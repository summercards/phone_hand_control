from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def send(code: str, host: str = "127.0.0.1", port: int = 9876, timeout: float = 60.0):
    message = {"type": "execute_code", "params": {"code": code}}
    with socket.create_connection((host, port), timeout=5.0) as sock:
        sock.settimeout(timeout)
        sock.sendall(json.dumps(message).encode("utf-8"))
        chunks = bytearray()
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.extend(data)
            try:
                return json.loads(chunks.decode("utf-8"))
            except json.JSONDecodeError:
                continue
    raise RuntimeError("MCP closed without a complete JSON response")


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a Python file in the active Blender MCP session")
    parser.add_argument("file", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    code = f"PROJECT_ROOT = {str(PROJECT_ROOT)!r}\n" + args.file.read_text(encoding="utf-8")
    result = send(code, args.host, args.port, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())



