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

HAND_TEMPLATE = (
    (0.50, 0.78), (0.42, 0.68), (0.36, 0.60), (0.31, 0.53), (0.27, 0.47),
    (0.48, 0.59), (0.48, 0.48), (0.48, 0.38), (0.48, 0.29),
    (0.54, 0.57), (0.54, 0.45), (0.54, 0.34), (0.54, 0.24),
    (0.60, 0.59), (0.60, 0.48), (0.60, 0.38), (0.60, 0.30),
    (0.66, 0.63), (0.66, 0.53), (0.66, 0.44), (0.66, 0.37),
)


def build_landmarks(t: float, hand_offset: float = 0.0) -> tuple[tuple[float, ...], tuple[float, ...]]:
    image = []
    world = []
    shift_x = hand_offset + 0.14 * math.sin(t * 1.8)
    shift_y = 0.035 * math.cos(t * 1.3)
    for index, (x, y) in enumerate(HAND_TEMPLATE):
        image_x = x + shift_x
        image_y = y + shift_y
        image_z = 0.008 * math.sin(t * 2.2 + index * 0.25)
        image.extend((image_x, image_y, image_z))
        world.extend((
            (index % 5 - 2) * 0.018,
            -(index // 5) * 0.035,
            image_z,
        ))
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
            hands = []
            if args.side in {"left", "both"}:
                image, world = build_landmarks(elapsed, -0.18)
                hands.append(PoseHand(1, 1, 1.0, image, world))
            if args.side in {"right", "both"}:
                image, world = build_landmarks(elapsed, 0.18)
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
