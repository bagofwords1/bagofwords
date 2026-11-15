"""Tiny offline Ethernet capture fixtures with distinct schemas and repeated DNS answers."""

import struct


def write_capture(path, frames):
    if path.suffix == ".pcapng":

        def block(kind, body):
            size = len(body) + 12
            return struct.pack("<II", kind, size) + body + struct.pack("<I", size)

        data = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
        data += block(1, struct.pack("<HHI", 1, 0, 65535))
        for i, frame in enumerate(frames):
            data += block(6, struct.pack("<IIIII", 0, 0, i, len(frame), len(frame)) + frame + bytes(-len(frame) % 4))
    else:
        data = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        for i, frame in enumerate(frames):
            data += struct.pack("<IIII", 1700000000, i, len(frame), len(frame)) + frame
    path.write_bytes(data)


ETH = bytes.fromhex("00112233445566778899aabb")
ARP = ETH + bytes.fromhex("0806000108000604000166778899aabbc0000201000000000000c0000202")
# DNS response with two A records. Repeated values must survive in order.
DNS = bytes.fromhex("000181800000000200000000" + "00000100010000003c0004c0000201" + "00000100010000003c0004c0000202")
UDP = struct.pack("!HHHH", 53, 12345, 8 + len(DNS), 0) + DNS
IP = (
    ETH
    + bytes.fromhex("08004500")
    + struct.pack("!H", 20 + len(UDP))
    + bytes.fromhex("0001000040110000c0000201c0000202")
    + UDP
)
