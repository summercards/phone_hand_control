"""Binary protocol shared by the phone server, tests and diagnostics.

Browser -> server (WebSocket, little endian):
    magic         4s  b"PHCW"
    version       B   1
    hand_count    B   0..2
    sequence      I
    capture_ms    d   performance.now() on the phone
    hands         N * WS_HAND

WS_HAND:
    hand_id       B   0 unknown, 1 left, 2 right
    flags         B   bit 0 tracked, bit 1 mirrored source/diagnostic
    score         f
    image_xyz     126f: 21 image landmarks followed by 21 world landmarks
    For each landmark: x, y, z. Image x/y are normalized [0, 1];
    image z is relative depth. World landmarks are metres.

server -> Blender (UDP, little endian):
    magic         4s  b"PHC1"
    version       B   1
    hand_count    B   0..2
    receive_ns    Q   server time.monotonic_ns()
    sequence      I
    capture_ms    d   phone performance.now()
    hands         N * UDP_HAND (same layout as WS_HAND)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Sequence

WS_MAGIC = b"PHCW"
UDP_MAGIC = b"PHC1"
PROTOCOL_VERSION = 1
MAX_HANDS = 2
LANDMARKS_PER_HAND = 21
COORDS_PER_HAND = LANDMARKS_PER_HAND * 3 * 2

WS_HEADER = struct.Struct("<4sBBI d")
UDP_HEADER = struct.Struct("<4sBBQI d")
HAND = struct.Struct("<BBf126f")

assert WS_HEADER.size == 18
assert UDP_HEADER.size == 26
assert HAND.size == 510


@dataclass(slots=True)
class PoseHand:
    hand_id: int
    flags: int
    score: float
    image_xyz: tuple[float, ...]
    world_xyz: tuple[float, ...]

    @property
    def tracked(self) -> bool:
        return bool(self.flags & 0x01)


@dataclass(slots=True)
class PosePacket:
    sequence: int
    capture_ms: float
    hands: tuple[PoseHand, ...]
    receive_ns: int | None = None


def parse_ws_pose(data: bytes) -> PosePacket:
    """Parse and validate one browser WebSocket binary message."""
    if len(data) < WS_HEADER.size:
        raise ValueError("packet shorter than header")
    magic, version, hand_count, sequence, capture_ms = WS_HEADER.unpack_from(data)
    if magic != WS_MAGIC:
        raise ValueError(f"bad WebSocket magic: {magic!r}")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {version}")
    if hand_count > MAX_HANDS:
        raise ValueError(f"too many hands: {hand_count}")
    expected = WS_HEADER.size + hand_count * HAND.size
    if len(data) != expected:
        raise ValueError(f"bad packet length: got {len(data)}, expected {expected}")

    offset = WS_HEADER.size
    hands: list[PoseHand] = []
    for _ in range(hand_count):
        fields = HAND.unpack_from(data, offset)
        offset += HAND.size
        hand_id, flags, score = fields[:3]
        coords = fields[3:]
        if hand_id > 2:
            raise ValueError(f"bad hand id: {hand_id}")
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"bad hand score: {score}")
        image = tuple(coords[:63])
        world = tuple(coords[63:126])
        hands.append(PoseHand(hand_id, flags, score, image, world))

    return PosePacket(sequence=sequence, capture_ms=capture_ms, hands=tuple(hands))


def pack_udp_pose(packet: PosePacket, receive_ns: int) -> bytes:
    """Pack a validated packet for the local Blender UDP receiver."""
    out = bytearray(
        UDP_HEADER.pack(
            UDP_MAGIC,
            PROTOCOL_VERSION,
            len(packet.hands),
            receive_ns,
            packet.sequence,
            packet.capture_ms,
        )
    )
    for hand in packet.hands:
        out.extend(
            HAND.pack(
                hand.hand_id,
                hand.flags,
                hand.score,
                *hand.image_xyz,
                *hand.world_xyz,
            )
        )
    return bytes(out)


def parse_udp_pose(data: bytes) -> PosePacket:
    """Parse server -> Blender packets. Used by tests and optional diagnostics."""
    if len(data) < UDP_HEADER.size:
        raise ValueError("packet shorter than UDP header")
    magic, version, hand_count, receive_ns, sequence, capture_ms = (
        UDP_HEADER.unpack_from(data)
    )
    if magic != UDP_MAGIC:
        raise ValueError(f"bad UDP magic: {magic!r}")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {version}")
    if hand_count > MAX_HANDS:
        raise ValueError(f"too many hands: {hand_count}")
    expected = UDP_HEADER.size + hand_count * HAND.size
    if len(data) != expected:
        raise ValueError(f"bad packet length: got {len(data)}, expected {expected}")

    offset = UDP_HEADER.size
    hands: list[PoseHand] = []
    for _ in range(hand_count):
        fields = HAND.unpack_from(data, offset)
        offset += HAND.size
        hand_id, flags, score = fields[:3]
        coords = fields[3:]
        hands.append(
            PoseHand(
                hand_id,
                flags,
                score,
                tuple(coords[:63]),
                tuple(coords[63:126]),
            )
        )
    return PosePacket(sequence, capture_ms, tuple(hands), receive_ns)


def landmarks_to_tuples(flat: Sequence[float]) -> Iterable[tuple[float, float, float]]:
    if len(flat) != 63:
        raise ValueError(f"expected 63 coordinates, got {len(flat)}")
    for i in range(0, 63, 3):
        yield float(flat[i]), float(flat[i + 1]), float(flat[i + 2])
