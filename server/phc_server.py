from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from protocol import parse_ws_pose, pack_udp_pose

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_CERT_DIR = PROJECT_ROOT / "certs"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class BrokerStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started_ns = time.monotonic_ns()
        self.ws_clients = 0
        self.pose_count = 0
        self.hand_count = 0
        self.udp_bytes = 0
        self.last_sequence: int | None = None
        self.last_receive_ns: int | None = None
        self.fps = 0.0
        self.rtt_ms = 0.0
        self.last_client_stats: dict[str, Any] = {}
        self._window_count = 0
        self._window_start_ns = time.monotonic_ns()

    def client_connected(self) -> None:
        with self.lock:
            self.ws_clients += 1

    def client_disconnected(self) -> None:
        with self.lock:
            self.ws_clients = max(0, self.ws_clients - 1)

    def record_pose(self, sequence: int, hand_count: int, udp_bytes: int) -> None:
        now = time.monotonic_ns()
        with self.lock:
            self.pose_count += 1
            self.hand_count = hand_count
            self.udp_bytes += udp_bytes
            self.last_sequence = sequence
            self.last_receive_ns = now
            self._window_count += 1
            elapsed = now - self._window_start_ns
            if elapsed >= 1_000_000_000:
                self.fps = self._window_count * 1_000_000_000 / elapsed
                self._window_count = 0
                self._window_start_ns = now

    def set_client_stats(self, stats: dict[str, Any]) -> None:
        with self.lock:
            self.last_client_stats = stats
            try:
                self.rtt_ms = float(stats.get("rtt_ms", self.rtt_ms))
            except (TypeError, ValueError):
                pass

    def public(self) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic_ns()
            age_ms = (
                (now - self.last_receive_ns) / 1_000_000
                if self.last_receive_ns is not None
                else None
            )
            return {
                "uptime_s": (now - self.started_ns) / 1_000_000_000,
                "ws_clients": self.ws_clients,
                "pose_count": self.pose_count,
                "hand_count": self.hand_count,
                "pose_hz": round(self.fps, 2),
                "udp_bytes": self.udp_bytes,
                "last_sequence": self.last_sequence,
                "last_pose_age_ms": round(age_ms, 2) if age_ms is not None else None,
                "phone_rtt_ms": round(self.rtt_ms, 2),
                "client": dict(self.last_client_stats),
            }


class WebSocketProtocolError(Exception):
    pass


class Broker:
    def __init__(self, web_root: Path, udp_host: str, udp_port: int) -> None:
        self.web_root = web_root.resolve()
        self.udp_target = (udp_host, udp_port)
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.setblocking(False)
        self.stats = BrokerStats()
        self.session_token = secrets.token_urlsafe(12)
        self._stop = threading.Event()

    def send_udp(self, data: bytes) -> None:
        try:
            self.udp_socket.sendto(data, self.udp_target)
        except OSError as exc:
            print(f"[PHC] UDP send failed: {exc}")

    def close(self) -> None:
        self._stop.set()
        self.udp_socket.close()

class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PhoneHandControl/1.0"

    @property
    def broker(self) -> Broker:
        return self.server.broker  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(send_body=True)

    def _handle(self, send_body: bool) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/ws":
            if not self._valid_ws_request():
                self._json(HTTPStatus.BAD_REQUEST, {"error": "WebSocket upgrade required"})
                return
            if not self._token_ok(parsed.query):
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid session token"})
                return
            self._handle_websocket()
            return

        if parsed.path == "/health":
            self._json(HTTPStatus.OK, self.broker.stats.public())
            return

        if parsed.path == "/ca.crt":
            self._serve_file(self.broker.web_root.parent / "certs" / "ca.crt", send_body)
            return

        self._serve_static(parsed.path, send_body)

    def _serve_static(self, raw_path: str, send_body: bool) -> None:
        path = urllib.parse.unquote(raw_path)
        relative = path.lstrip("/") or "index.html"
        candidate = (self.broker.web_root / relative).resolve()
        try:
            candidate.relative_to(self.broker.web_root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        self._serve_file(candidate, send_body)

    def _serve_file(self, path: Path, send_body: bool) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in {".mjs", ".js"}:
            content_type = "text/javascript; charset=utf-8"
        elif path.suffix.lower() == ".wasm":
            content_type = "application/wasm"
        elif path.suffix.lower() == ".task":
            content_type = "application/octet-stream"
        elif path.suffix.lower() == ".html":
            content_type = "text/html; charset=utf-8"
        elif path.suffix.lower() == ".json":
            content_type = "application/json; charset=utf-8"

        stat = path.stat()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header(
            "Cache-Control",
            "no-cache" if path.suffix == ".html" else "public, max-age=86400",
        )
        self.end_headers()
        if not send_body:
            return
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(256 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _token_ok(self, query: str) -> bool:
        supplied = urllib.parse.parse_qs(query).get("token", [""])[0]
        return secrets.compare_digest(supplied, self.broker.session_token)

    def _valid_ws_request(self) -> bool:
        return (
            self.headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in self.headers.get("Connection", "").lower()
            and bool(self.headers.get("Sec-WebSocket-Key"))
        )

    def _handle_websocket(self) -> None:
        key = self.headers["Sec-WebSocket-Key"].encode("ascii")
        accept = base64.b64encode(
            hashlib.sha1(key + GUID.encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        self.broker.stats.client_connected()
        print("[PHC] phone connected")
        try:
            self.connection.settimeout(30.0)
            while True:
                opcode, payload = self._recv_frame()
                if opcode == 0x8:
                    self._send_frame(0x8, payload[:125])
                    break
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x1:
                    self._handle_text(payload)
                    continue
                if opcode != 0x2:
                    continue

                try:
                    packet = parse_ws_pose(payload)
                except ValueError as exc:
                    print(f"[PHC] dropped malformed pose: {exc}")
                    continue
                receive_ns = time.monotonic_ns()
                udp_payload = pack_udp_pose(packet, receive_ns)
                self.broker.send_udp(udp_payload)
                self.broker.stats.record_pose(
                    packet.sequence, len(packet.hands), len(udp_payload)
                )
        except (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError):
            pass
        except WebSocketProtocolError as exc:
            if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
                print(f"[PHC] WebSocket closed: {exc}")
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
            self.broker.stats.client_disconnected()
            print("[PHC] phone disconnected")

    def _handle_text(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        kind = message.get("type")
        if kind == "ping":
            self._send_json_text(
                {
                    "type": "pong",
                    "client_t": message.get("t"),
                    "server_mono_ms": time.monotonic() * 1000.0,
                }
            )
        elif kind == "telemetry":
            stats = message.get("stats")
            if isinstance(stats, dict):
                self.broker.stats.set_client_stats(stats)

    def _read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.connection.recv(length - len(chunks))
            if not chunk:
                raise ConnectionError("socket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if not masked:
            raise WebSocketProtocolError("client frame is not masked")
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > 2 * 1024 * 1024:
            raise WebSocketProtocolError("frame exceeds 2 MiB limit")
        if not fin:
            raise WebSocketProtocolError("fragmented frames are not supported")
        mask = self._read_exact(4)
        payload = bytearray(self._read_exact(length))
        for i in range(length):
            payload[i] ^= mask[i & 3]
        return opcode, bytes(payload)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        self.connection.sendall(bytes(header) + payload)

    def _send_json_text(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send_frame(0x1, payload)

class BootstrapHandler(BaseHTTPRequestHandler):
    """Plain HTTP endpoint used only to install the local CA certificate."""

    protocol_version = "HTTP/1.1"
    server_version = "PhoneHandControlBootstrap/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/ca.crt":
            path = self.server.broker.web_root.parent / "certs" / "ca.crt"  # type: ignore[attr-defined]
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="phone-hand-control-ca.crt"',
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        host = self.headers.get("Host", "192.168.0.0")
        https_host = host.split(":")[0]
        token = self.server.broker.session_token  # type: ignore[attr-defined]
        html = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Phone Hand Control - 安装证书</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#10131a;color:#edf2ff;margin:0;padding:32px;line-height:1.6}}.card{{max-width:720px;margin:auto;background:#1a1f2c;border:1px solid #30394e;border-radius:18px;padding:24px}}a.button{{display:block;text-align:center;background:#4d7cfe;color:white;text-decoration:none;padding:16px;border-radius:12px;font-size:18px;font-weight:700;margin:22px 0}}code{{background:#0d1017;padding:3px 7px;border-radius:6px}}.warn{{color:#ffcc70}}</style>
<div class='card'><h1>Phone Hand Control</h1>
<p>手机摄像头必须运行在可信 HTTPS 环境中。请先安装本机生成的局域网 CA 证书。</p>
<a class='button' href='/ca.crt'>1. 下载 CA 证书</a>
<h2>Android</h2><p>设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书，选择刚下载的文件。</p>
<h2>iPhone / iPad</h2><p>用 Safari 下载后前往“设置 → 已下载描述文件”安装；再到“设置 → 通用 → 关于本机 → 证书信任设置”中启用完全信任。</p>
<p class='warn'>安装完成后点击：<br><a href='https://{https_host}:8443/?token={token}'>2. 打开手部控制页面</a></p>
<p>证书仅用于同一局域网内访问 <code>{https_host}</code>，不会上传摄像头图像。</p></div></html>"""
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_https_server(
    host: str,
    port: int,
    broker: Broker,
    cert_dir: Path,
    verbose: bool,
) -> ReusableThreadingHTTPServer:
    cert = cert_dir / "server.crt"
    key = cert_dir / "server.key"
    if not cert.exists() or not key.exists():
        raise FileNotFoundError(
            f"TLS certificate missing: run `python server/setup_assets.py` first ({cert_dir})"
        )
    server = ReusableThreadingHTTPServer((host, port), RequestHandler)
    server.broker = broker  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def build_bootstrap_server(
    host: str, port: int, broker: Broker, verbose: bool
) -> ReusableThreadingHTTPServer:
    server = ReusableThreadingHTTPServer((host, port), BootstrapHandler)
    server.broker = broker  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def make_qr(url: str) -> Path | None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        return None
    output = PROJECT_ROOT / "join_qr.png"
    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image(fill_color="#111827", back_color="white").save(output)
    return output


def stats_loop(broker: Broker, stop: threading.Event) -> None:
    while not stop.wait(2.0):
        value = broker.stats.public()
        age = value["last_pose_age_ms"]
        print(
            "[PHC] "
            f"pose={value['pose_hz']:.1f}Hz hands={value['hand_count']} "
            f"age={age if age is not None else '-'}ms "
            f"phone_rtt={value['phone_rtt_ms']:.1f}ms ws={value['ws_clients']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local HTTPS/WebSocket broker for Phone Hand Control"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--udp-host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=8766)
    parser.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT)
    parser.add_argument("--cert-dir", type=Path, default=DEFAULT_CERT_DIR)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-bootstrap", action="store_true")
    parser.add_argument("--no-qr", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    broker = Broker(args.web_root, args.udp_host, args.udp_port)
    https_server = build_https_server(
        args.host, args.https_port, broker, args.cert_dir, args.verbose
    )
    bootstrap_server: ReusableThreadingHTTPServer | None = None
    if not args.no_bootstrap:
        bootstrap_server = build_bootstrap_server(
            args.host, args.http_port, broker, args.verbose
        )

    ip = lan_ip()
    secure_url = f"https://{ip}:{args.https_port}/?token={broker.session_token}"
    bootstrap_url = f"http://{ip}:{args.http_port}/"
    qr_target = bootstrap_url if bootstrap_server is not None else secure_url
    qr_path = None if args.no_qr else make_qr(qr_target)
    stop = threading.Event()

    print("\nPhone Hand Control")
    print(f"  Blender UDP target : {args.udp_host}:{args.udp_port}")
    print(f"  Certificate setup  : {bootstrap_url}")
    print(f"  Control page       : {secure_url}")
    print(f"  Session token      : {broker.session_token}")
    if qr_path is not None:
        print(f"  Join QR image      : {qr_path}")
    print("  Press Ctrl+C to stop\n")

    threads = [
        threading.Thread(target=https_server.serve_forever, name="https", daemon=True),
        threading.Thread(target=stats_loop, args=(broker, stop), name="stats", daemon=True),
    ]
    if bootstrap_server is not None:
        threads.insert(
            1,
            threading.Thread(
                target=bootstrap_server.serve_forever,
                name="bootstrap",
                daemon=True,
            ),
        )
    for thread in threads:
        thread.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[PHC] stopping")
    finally:
        stop.set()
        https_server.shutdown()
        if bootstrap_server is not None:
            bootstrap_server.shutdown()
        https_server.server_close()
        if bootstrap_server is not None:
            bootstrap_server.server_close()
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


