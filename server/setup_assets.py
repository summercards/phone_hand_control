from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_VENDOR = PROJECT_ROOT / "web" / "vendor" / "mediapipe"
CERT_DIR = PROJECT_ROOT / "certs"

MEDIAPIPE_VERSION = "1.0.1"
MEDIAPIPE_TARBALLS = (
    "https://registry.npmjs.org/@mediapipe/tasks-vision/-/tasks-vision-1.0.1.tgz",
    "https://registry.npmmirror.com/@mediapipe/tasks-vision/-/tasks-vision-1.0.1.tgz",
)
HAND_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)


def log(message: str) -> None:
    print(f"[setup] {message}", flush=True)


def local_ips() -> list[str]:
    values = {"127.0.0.1"}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        values.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        values.update(ipaddress.ip_address(x[4][0]).compressed for x in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(values)


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "PhoneHandControl/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length", "0"))
        received = 0
        last_report = -1
        while chunk := response.read(256 * 1024):
            output.write(chunk)
            received += len(chunk)
            if total:
                percent = int(received * 100 / total)
                if percent >= last_report + 10:
                    log(f"  {percent:3d}% ({received // 1024} KiB)")
                    last_report = percent
    log(f"  saved {destination} ({destination.stat().st_size // 1024} KiB)")


def install_mediapipe_browser_assets(force: bool = False) -> None:
    bundle = WEB_VENDOR / "vision_bundle.mjs"
    wasm_dir = WEB_VENDOR / "wasm"
    model = WEB_VENDOR / "hand_landmarker.task"
    if not force and bundle.exists() and model.exists() and wasm_dir.exists():
        if list(wasm_dir.glob("*.wasm")) and list(wasm_dir.glob("*.js")):
            log("MediaPipe browser assets already installed")
            return

    WEB_VENDOR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="phc-mediapipe-") as temp_name:
        temp = Path(temp_name)
        tarball = temp / "tasks-vision.tgz"
        last_error: Exception | None = None
        for url in MEDIAPIPE_TARBALLS:
            try:
                download(url, tarball)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                log(f"download failed, trying fallback: {exc}")
        if last_error is not None:
            raise RuntimeError(f"could not download MediaPipe package: {last_error}")

        with tarfile.open(tarball, "r:gz") as archive:
            wanted_bundle = "package/vision_bundle.mjs"
            bundle_member = archive.getmember(wanted_bundle)
            bundle_member.name = "vision_bundle.mjs"
            archive.extract(bundle_member, WEB_VENDOR, filter="data")

            wasm_members = []
            for member in archive.getmembers():
                prefix = "package/wasm/"
                if member.isfile() and member.name.startswith(prefix):
                    member.name = member.name[len(prefix):]
                    wasm_members.append(member)
            if not wasm_members:
                raise RuntimeError("MediaPipe package did not contain package/wasm assets")
            wasm_dir.mkdir(parents=True, exist_ok=True)
            for member in wasm_members:
                archive.extract(member, wasm_dir, filter="data")

    if force or not model.exists():
        download(HAND_MODEL_URL, model)
    else:
        log("hand_landmarker.task already installed")


def ensure_certificates(force: bool = False) -> None:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:
        raise RuntimeError(
            "cryptography is required for HTTPS certificate generation; run pip install -r requirements.txt"
        ) from exc

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    ca_cert_path = CERT_DIR / "ca.crt"
    ca_key_path = CERT_DIR / "ca.key"
    server_cert_path = CERT_DIR / "server.crt"
    server_key_path = CERT_DIR / "server.key"

    if force or not (ca_cert_path.exists() and ca_key_path.exists()):
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        now = datetime.now(timezone.utc)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Phone Hand Control"),
                x509.NameAttribute(NameOID.COMMON_NAME, "Phone Hand Control Local CA"),
            ]
        )
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        ca_key_path.write_bytes(
            ca_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        log(f"created local CA: {ca_cert_path}")
    else:
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)

    ips = local_ips()
    need_server_cert = force or not (server_cert_path.exists() and server_key_path.exists())
    if not need_server_cert:
        existing = x509.load_pem_x509_certificate(server_cert_path.read_bytes())
        try:
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            existing_ips = {str(value) for value in san.get_values_for_type(x509.IPAddress)}
            need_server_cert = not set(ips).issubset(existing_ips)
        except x509.ExtensionNotFound:
            need_server_cert = True
    if not need_server_cert:
        log("HTTPS certificate is valid for current LAN address")
        return

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Phone Hand Control"),
            x509.NameAttribute(NameOID.COMMON_NAME, ips[0] if ips else "localhost"),
        ]
    )
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost")]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    )
    now = datetime.now(timezone.utc)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    log(f"created HTTPS certificate for: {', '.join(ips)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download local assets and create HTTPS certificates")
    parser.add_argument("--force", action="store_true", help="redownload all assets and regenerate CA")
    parser.add_argument("--certs-only", action="store_true", help="only create or refresh certificates")
    parser.add_argument("--assets-only", action="store_true", help="only download browser assets")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.certs_only:
        install_mediapipe_browser_assets(force=args.force)
    if not args.assets_only:
        ensure_certificates(force=args.force)
    log("setup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

