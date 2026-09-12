from __future__ import annotations

import argparse
import math
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from protocol import PoseHand, PosePacket, pack_udp_pose  # noqa: E402


def build_landmarks(t: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    image = []
    world = []
    cx = 0.5 + 0.12 * math.sin(t * 2.0)
    cy = 0.55 + 0.06 * math.cos(t * 1.7)
    for index in range(21):
        angle = index * 0.43
        radius = 0.03 + (index % 5) * 0.003
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        z = 0.01 * math.sin(t * 3.0 + index * 0.2)
        image.extend((x, y, z))
        world.extend(((index % 5) * 0.015 - 0.03, -0.02 + (index // 5) * 0.025, z))
    return tuple(image), tuple(world)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a synthetic hand pose directly to Blender UDP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--hz", type=float, default=60.0)
    parser.add_argument("--side", choices=("left", "right", "both"), default="right")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / max(1.0, args.hz)
    start = time.monotonic()
    sequence = 0
    try:
        while time.monotonic() - start < args.seconds:
            elapsed = time.monotonic() - start
            image, world = build_landmarks(elapsed)
            hands = []
            if args.side in {"left", "both"}:
                hands.append(PoseHand(1, 1, 1.0, image, world))
            if args.side in {"right", "both"}:
                hands.append(PoseHand(2, 1, 1.0, image, world))
            packet = PosePacket(sequence, elapsed * 1000.0, tuple(hands))
            sock.sendto(pack_udp_pose(packet, time.monotonic_ns()), (args.host, args.port))
            sequence += 1
            time.sleep(interval)
    finally:
        sock.close()
    print(f"sent {sequence} packets to {args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
