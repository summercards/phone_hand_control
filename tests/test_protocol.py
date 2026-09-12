from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from protocol import HAND, UDP_HEADER, WS_HEADER, PoseHand, PosePacket, pack_udp_pose, parse_udp_pose, parse_ws_pose


def make_packet(sequence: int = 7) -> PosePacket:
    image = tuple(float(i) / 100.0 for i in range(63))
    world = tuple((float(i) - 31.0) / 1000.0 for i in range(63))
    return PosePacket(sequence=sequence, capture_ms=1234.5, hands=(PoseHand(2, 3, 0.97, image, world),))


class ProtocolTests(unittest.TestCase):
    def test_browser_packet_round_trip_shape(self) -> None:
        packet = make_packet()
        hand = packet.hands[0]
        raw = WS_HEADER.pack(b"PHCW", 1, 1, packet.sequence, packet.capture_ms)
        raw += HAND.pack(hand.hand_id, hand.flags, hand.score, *hand.image_xyz, *hand.world_xyz)
        parsed = parse_ws_pose(raw)
        self.assertEqual(parsed.sequence, 7)
        self.assertAlmostEqual(parsed.capture_ms, 1234.5)
        self.assertEqual(len(parsed.hands), 1)
        self.assertEqual(parsed.hands[0].hand_id, 2)
        self.assertAlmostEqual(parsed.hands[0].world_xyz[-1], (62 - 31) / 1000.0, places=6)

    def test_udp_round_trip(self) -> None:
        packet = make_packet(11)
        raw = pack_udp_pose(packet, 987654321)
        self.assertEqual(len(raw), UDP_HEADER.size + HAND.size)
        parsed = parse_udp_pose(raw)
        self.assertEqual(parsed.receive_ns, 987654321)
        self.assertEqual(parsed.sequence, 11)
        self.assertEqual(parsed.hands[0].flags, 3)

    def test_rejects_invalid_magic(self) -> None:
        raw = WS_HEADER.pack(b"NOPE", 1, 0, 0, 0.0)
        with self.assertRaises(ValueError):
            parse_ws_pose(raw)

    def test_rejects_truncated_packet(self) -> None:
        raw = WS_HEADER.pack(b"PHCW", 1, 1, 0, 0.0)
        with self.assertRaises(ValueError):
            parse_ws_pose(raw)


if __name__ == "__main__":
    unittest.main()
