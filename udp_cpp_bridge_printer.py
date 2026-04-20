#!/usr/bin/env python3
"""
UDP printer for binary packets produced by can_udp_bridge (C++).
"""

import argparse
import socket
import struct
import sys
import time
import zlib


MAGIC = 0xCAFE
VERSION = 1
FLAG_EXTENDED = 1 << 0
FLAG_RTR = 1 << 1
FLAG_ERROR = 1 << 2

# C++ packed layout (network byte order for multi-byte fields):
# uint16 magic
# uint8  version
# uint8  flags
# uint32 can_id
# uint8  dlc
# uint8  data[8]
# uint32 seq
# uint32 crc32
FRAME_STRUCT = struct.Struct("!HBBIB8sII")
FRAME_SIZE = FRAME_STRUCT.size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read and print binary CAN-over-UDP frames from can_udp_bridge."
    )
    parser.add_argument(
        "--listen-host",
        default="0.0.0.0",
        help="UDP host/IP to bind. Default: 0.0.0.0",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        required=True,
        help="UDP port to bind",
    )
    parser.add_argument(
        "--show-invalid",
        action="store_true",
        help="Print reason for invalid packets (bad size/header/crc)",
    )
    return parser.parse_args()


def verify_crc(payload: bytes, expected_crc: int) -> bool:
    # C++ sender computes CRC over frame bytes excluding trailing crc32 field.
    calc = zlib.crc32(payload[:-4]) & 0xFFFFFFFF
    return calc == expected_crc


def format_frame(
    can_id: int, flags: int, dlc: int, data: bytes, seq: int, src: tuple[str, int]
) -> str:
    is_extended = bool(flags & FLAG_EXTENDED)
    is_rtr = bool(flags & FLAG_RTR)
    is_error = bool(flags & FLAG_ERROR)

    can_type = "CAN2.0B" if is_extended else "CAN2.0A"
    can_id_hex = f"{can_id:08X}" if is_extended else f"{can_id:03X}"
    data_hex = data[:dlc].hex().upper()

    flags_parts = []
    if is_rtr:
        flags_parts.append("RTR")
    if is_error:
        flags_parts.append("ERR")
    flags_text = f" [{'|'.join(flags_parts)}]" if flags_parts else ""

    ts_local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return (
        f"{ts_local} {src[0]}:{src[1]} {can_type} ID=0x{can_id_hex} "
        f"DLC={dlc} DATA={data_hex} SEQ={seq}{flags_text}"
    )


def main() -> int:
    args = parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((args.listen_host, args.listen_port))
    except OSError as exc:
        print(f"[ERR] Failed to bind UDP socket: {exc}", file=sys.stderr)
        return 1

    print(
        f"[INFO] Listening binary bridge UDP on {args.listen_host}:{args.listen_port} "
        f"(frame_size={FRAME_SIZE})"
    )

    valid = 0
    invalid = 0

    while True:
        try:
            payload, src = sock.recvfrom(65535)
        except KeyboardInterrupt:
            print("\n[INFO] Stopped")
            print(f"[INFO] Valid frames: {valid}, invalid frames: {invalid}")
            return 0
        except OSError as exc:
            print(f"[ERR] UDP receive failed: {exc}", file=sys.stderr)
            return 1

        if len(payload) != FRAME_SIZE:
            invalid += 1
            if args.show_invalid:
                print(f"[WARN] Invalid frame size from {src[0]}:{src[1]}: {len(payload)}")
            continue

        magic, version, flags, can_id, dlc, data, seq, crc = FRAME_STRUCT.unpack(payload)

        if magic != MAGIC or version != VERSION:
            invalid += 1
            if args.show_invalid:
                print(
                    f"[WARN] Bad header from {src[0]}:{src[1]}: "
                    f"magic=0x{magic:04X}, version={version}"
                )
            continue

        if not verify_crc(payload, crc):
            invalid += 1
            if args.show_invalid:
                print(f"[WARN] Bad CRC from {src[0]}:{src[1]} (seq={seq})")
            continue

        if dlc > 8:
            invalid += 1
            if args.show_invalid:
                print(f"[WARN] Bad DLC from {src[0]}:{src[1]}: {dlc}")
            continue

        valid += 1
        print(format_frame(can_id, flags, dlc, data, seq, src))


if __name__ == "__main__":
    sys.exit(main())
