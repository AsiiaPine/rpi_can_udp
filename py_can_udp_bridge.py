#!/usr/bin/env python3
"""
Python CAN <-> UDP bridge (SocketCAN on Linux).

Compatible with can_udp_bridge.cpp binary UDP frame format:
- magic: 0xCAFE
- version: 1
- CRC32 over frame bytes excluding crc32 field
"""

import argparse
import os
import select
import signal
import socket
import struct
import sys
import time
import zlib


CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x000007FF
CAN_EFF_MASK = 0x1FFFFFFF

UDP_MAGIC = 0xCAFE
UDP_VERSION = 1
FLAG_EXTENDED = 1 << 0
FLAG_RTR = 1 << 1
FLAG_ERROR = 1 << 2

CAN_FRAME_STRUCT = struct.Struct("=IB3x8s")   # linux struct can_frame
UDP_FRAME_STRUCT = struct.Struct("!HBBIB8sII")  # network byte order


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Python CAN <-> UDP bridge")
    p.add_argument("--mode", choices=["can2udp", "udp2can", "bridge"], default="bridge")
    p.add_argument("--can-iface", default="can0", help="CAN interface name")
    p.add_argument("--udp-remote-host", default="127.0.0.1", help="Remote UDP host")
    p.add_argument("--udp-remote-port", type=int, default=5000, help="Remote UDP port")
    p.add_argument(
        "--udp-broadcast",
        action="store_true",
        help="Enable UDP broadcast sending (uses --udp-remote-host as broadcast address)",
    )
    p.add_argument("--udp-listen-port", type=int, default=5000, help="Local UDP listen port")
    p.add_argument(
        "--allow-self-loop",
        action="store_true",
        help="Allow sending UDP to local host on the same listen port (danger: CAN<->UDP loop)",
    )
    p.add_argument("--stats-interval", type=float, default=2.0, help="Stats print interval seconds")
    return p.parse_args()


def build_udp_frame(can_id_raw: int, dlc: int, data8: bytes, seq: int) -> bytes:
    flags = 0
    if can_id_raw & CAN_EFF_FLAG:
        flags |= FLAG_EXTENDED
        can_id = can_id_raw & CAN_EFF_MASK
    else:
        can_id = can_id_raw & CAN_SFF_MASK
    if can_id_raw & CAN_RTR_FLAG:
        flags |= FLAG_RTR
    if can_id_raw & CAN_ERR_FLAG:
        flags |= FLAG_ERROR

    dlc = min(max(dlc, 0), 8)
    frame_wo_crc = UDP_FRAME_STRUCT.pack(
        UDP_MAGIC, UDP_VERSION, flags, can_id, dlc, data8[:8].ljust(8, b"\x00"), seq, 0
    )
    crc = zlib.crc32(frame_wo_crc[:-4]) & 0xFFFFFFFF
    return UDP_FRAME_STRUCT.pack(
        UDP_MAGIC, UDP_VERSION, flags, can_id, dlc, data8[:8].ljust(8, b"\x00"), seq, crc
    )


def parse_udp_frame(payload: bytes) -> tuple[bool, int, int, bytes]:
    if len(payload) != UDP_FRAME_STRUCT.size:
        return False, 0, 0, b""
    magic, version, flags, can_id, dlc, data8, _seq, crc = UDP_FRAME_STRUCT.unpack(payload)
    if magic != UDP_MAGIC or version != UDP_VERSION or dlc > 8:
        return False, 0, 0, b""
    calc_crc = zlib.crc32(payload[:-4]) & 0xFFFFFFFF
    if calc_crc != crc:
        return False, 0, 0, b""

    can_id_raw = (can_id & CAN_EFF_MASK) if (flags & FLAG_EXTENDED) else (can_id & CAN_SFF_MASK)
    if flags & FLAG_EXTENDED:
        can_id_raw |= CAN_EFF_FLAG
    if flags & FLAG_RTR:
        can_id_raw |= CAN_RTR_FLAG
    if flags & FLAG_ERROR:
        can_id_raw |= CAN_ERR_FLAG

    return True, can_id_raw, dlc, data8


def main() -> int:
    if os.name != "posix":
        print("[ERR] This script requires Linux SocketCAN (posix)", file=sys.stderr)
        return 1

    args = parse_args()
    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        can_sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        can_sock.bind((args.can_iface,))
        can_sock.setblocking(False)
    except OSError as exc:
        print(f"[ERR] CAN init failed: {exc}", file=sys.stderr)
        return 1

    udp_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_rx.setblocking(False)
    if args.udp_broadcast:
        udp_tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    if args.mode in ("udp2can", "bridge"):
        try:
            udp_rx.bind(("0.0.0.0", args.udp_listen_port))
        except OSError as exc:
            print(f"[ERR] UDP bind failed: {exc}", file=sys.stderr)
            can_sock.close()
            udp_tx.close()
            udp_rx.close()
            return 1

    remote = (args.udp_remote_host, args.udp_remote_port)
    if args.mode == "bridge" and not args.allow_self_loop and not args.udp_broadcast:
        local_hosts = {"127.0.0.1", "localhost", "0.0.0.0"}
        try:
            local_hosts.add(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass
        try:
            remote_ip = socket.gethostbyname(args.udp_remote_host)
        except OSError:
            remote_ip = args.udp_remote_host
        if remote_ip in local_hosts and args.udp_remote_port == args.udp_listen_port:
            print(
                "[ERR] Self-loop detected: udp-remote-host points to local host and "
                "udp-remote-port equals udp-listen-port. "
                "Use different host/port or pass --allow-self-loop intentionally.",
                file=sys.stderr,
            )
            can_sock.close()
            udp_tx.close()
            udp_rx.close()
            return 2

    print(
        f"[INFO] Started mode={args.mode} can={args.can_iface} "
        f"udp_remote={remote[0]}:{remote[1]} udp_listen={args.udp_listen_port} "
        f"udp_broadcast={args.udp_broadcast}"
    )

    seq = 0
    tx_can2udp = 0
    udp_rx_frames = 0
    rx_udp2can = 0
    can_tx_fail = 0
    dropped_bad_crc = 0
    last_stats = time.monotonic()

    while not stop:
        read_list = []
        if args.mode in ("can2udp", "bridge"):
            read_list.append(can_sock)
        if args.mode in ("udp2can", "bridge"):
            read_list.append(udp_rx)

        if not read_list:
            time.sleep(0.05)
            continue

        ready, _, _ = select.select(read_list, [], [], 0.1)
        for rs in ready:
            if rs is can_sock:
                try:
                    frame = can_sock.recv(CAN_FRAME_STRUCT.size)
                    if len(frame) != CAN_FRAME_STRUCT.size:
                        continue
                    can_id_raw, dlc, data8 = CAN_FRAME_STRUCT.unpack(frame)
                    payload = build_udp_frame(can_id_raw, dlc, data8, seq)
                    seq += 1
                    udp_tx.sendto(payload, remote)
                    tx_can2udp += 1
                except OSError:
                    pass

            elif rs is udp_rx:
                try:
                    payload, _src = udp_rx.recvfrom(65535)
                except OSError:
                    continue

                udp_rx_frames += 1
                ok, can_id_raw, dlc, data8 = parse_udp_frame(payload)
                if not ok:
                    dropped_bad_crc += 1
                    continue

                can_frame = CAN_FRAME_STRUCT.pack(can_id_raw, dlc, data8)
                try:
                    can_sock.send(can_frame)
                    rx_udp2can += 1
                except OSError:
                    can_tx_fail += 1

        now = time.monotonic()
        if now - last_stats >= args.stats_interval:
            print(
                f"tx_can2udp={tx_can2udp} udp_rx_frames={udp_rx_frames} "
                f"rx_udp2can={rx_udp2can} can_tx_fail={can_tx_fail} dropped_bad_crc={dropped_bad_crc}"
            )
            last_stats = now

    print("[INFO] Stopping...")
    can_sock.close()
    udp_tx.close()
    udp_rx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
