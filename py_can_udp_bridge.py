#!/usr/bin/env python3
"""
Python CAN <-> UDP bridge (SocketCAN on Linux).

Compatible with can_udp_bridge.cpp binary UDP frame format:
- magic: 0xCAFE
- version: 1
- CRC32 over frame bytes excluding crc32 field
"""

import argparse
import errno
import ipaddress
import os
import select
import signal
import socket
import struct
import sys
import time
import zlib
from typing import Callable


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


def _resolved_ipv4_needs_so_broadcast(host: str) -> bool:
    """
    Linux requires SO_BROADCAST for sendto() to IPv4 directed broadcast (e.g. 192.168.1.255);
    otherwise errno is EACCES (Permission denied).
    """
    try:
        ip = socket.gethostbyname(host)
        addr = ipaddress.ip_address(ip)
    except (OSError, ValueError):
        return False
    if not isinstance(addr, ipaddress.IPv4Address):
        return False
    if addr == ipaddress.IPv4Address("255.255.255.255"):
        return True
    return (int(addr) & 0xFF) == 255


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Python CAN <-> UDP bridge")
    p.add_argument("--mode", choices=["can2udp", "udp2can", "bridge"], default="bridge")
    p.add_argument("--can-iface", default="can0", help="CAN interface name")
    p.add_argument("--udp-remote-host", default="127.0.0.1", help="Remote UDP host")
    p.add_argument("--udp-remote-port", type=int, default=5000, help="Remote UDP port")
    p.add_argument(
        "--udp-broadcast",
        action="store_true",
        help="Force SO_BROADCAST on the UDP TX socket. Also set automatically when "
        "--udp-remote-host resolves to a typical IPv4 broadcast (x.x.x.255 or 255.255.255.255).",
    )
    p.add_argument("--udp-listen-port", type=int, default=5000, help="Local UDP listen port")
    p.add_argument(
        "--allow-self-loop",
        action="store_true",
        help="Allow sending UDP to local host on the same listen port (danger: CAN<->UDP loop)",
    )
    p.add_argument("--stats-interval", type=float, default=2.0, help="Stats print interval seconds")
    p.add_argument(
        "--select-timeout",
        type=float,
        default=0.05,
        help="select() timeout when idle (seconds); lower = slightly more CPU, snappier wakeups",
    )
    p.add_argument(
        "--can-rx-buf",
        type=int,
        default=1 << 20,
        metavar="BYTES",
        help="SO_RCVBUF for CAN socket (best-effort; kernel may cap)",
    )
    p.add_argument(
        "--can-tx-buf",
        type=int,
        default=1 << 20,
        metavar="BYTES",
        help="SO_SNDBUF for CAN socket (best-effort; reduces EAGAIN on UDP->CAN bursts)",
    )
    p.add_argument(
        "--can-send-wait",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Max total time to wait for CAN TX queue (UDP->CAN) before counting a hard failure",
    )
    p.add_argument(
        "--udp-rx-buf",
        type=int,
        default=1 << 20,
        metavar="BYTES",
        help="SO_RCVBUF for UDP receive socket (bridge / udp2can)",
    )
    p.add_argument(
        "--udp-tx-buf",
        type=int,
        default=1 << 20,
        metavar="BYTES",
        help="SO_SNDBUF for UDP send socket",
    )
    p.add_argument(
        "--can-disable-local-loopback",
        action="store_true",
        help="Set CAN_RAW_LOOPBACK=0 on this socket (kernel default is ON). "
        "Use on vcan or to reduce echo traffic; candump on the same host may miss local TX.",
    )
    p.add_argument(
        "--drain-burst",
        type=int,
        default=64,
        metavar="N",
        help="Max UDP frames per inner drain step in bridge mode; CAN side uses N * bridge-can-weight.",
    )
    p.add_argument(
        "--bridge-can-weight",
        type=int,
        default=2,
        metavar="K",
        help="In bridge mode, read up to (drain-burst * K) CAN frames per leg before a UDP leg "
        "(K>=1). Higher K favors CAN->UDP and reduces tail drops under duplex load.",
    )
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
    pad8 = data8[:8].ljust(8, b"\x00")
    frame_wo_crc = UDP_FRAME_STRUCT.pack(
        UDP_MAGIC, UDP_VERSION, flags, can_id, dlc, pad8, seq, 0
    )
    crc = zlib.crc32(frame_wo_crc[:-4]) & 0xFFFFFFFF
    return UDP_FRAME_STRUCT.pack(
        UDP_MAGIC, UDP_VERSION, flags, can_id, dlc, pad8, seq, crc
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


def _can_send_would_block(exc: BaseException) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
    return False


def can_send_with_backpressure(
    can_sock: socket.socket,
    can_frame: bytes,
    *,
    should_stop: Callable[[], bool],
    max_wait_sec: float,
) -> bool:
    deadline = time.monotonic() + max(max_wait_sec, 0.01)
    while not should_stop():
        try:
            can_sock.send(can_frame)
            return True
        except (BlockingIOError, OSError) as exc:
            if not _can_send_would_block(exc):
                print(f"[ERR] CAN send failed: {exc}", file=sys.stderr)
                return False
        if time.monotonic() >= deadline:
            print("[ERR] CAN send: TX queue stayed full (timeout)", file=sys.stderr)
            return False
        remaining = deadline - time.monotonic()
        try:
            select.select([], [can_sock], [], max(0.0, min(0.25, remaining)))
        except InterruptedError:
            continue
    return False


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
        if args.can_disable_local_loopback:
            _can_raw_lb = getattr(socket, "CAN_RAW_LOOPBACK", 3)
            try:
                can_sock.setsockopt(socket.SOL_CAN_RAW, _can_raw_lb, 0)
            except OSError as exc:
                print(f"[WARN] CAN_RAW_LOOPBACK=0 not applied: {exc}", file=sys.stderr)
        if args.can_rx_buf > 0:
            try:
                can_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.can_rx_buf)
            except OSError as exc:
                print(f"[ERR] CAN RX buffer set failed: {exc}", file=sys.stderr)
        if args.can_tx_buf > 0:
            try:
                can_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, args.can_tx_buf)
            except OSError as exc:
                print(f"[ERR] CAN TX buffer set failed: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"[ERR] CAN init failed: {exc}", file=sys.stderr)
        return 1

    udp_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_rx.setblocking(False)
    if args.udp_tx_buf > 0:
        try:
            udp_tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, args.udp_tx_buf)
        except OSError as exc:
            print(f"[ERR] UDP TX buffer set failed: {exc}", file=sys.stderr)

    udp_broadcast_tx = args.udp_broadcast or _resolved_ipv4_needs_so_broadcast(args.udp_remote_host)
    if udp_broadcast_tx:
        try:
            udp_tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as exc:
            print(f"[ERR] UDP SO_BROADCAST failed: {exc}", file=sys.stderr)

    if args.mode in ("udp2can", "bridge"):
        try:
            udp_rx.bind(("0.0.0.0", args.udp_listen_port))
            if args.udp_rx_buf > 0:
                try:
                    udp_rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.udp_rx_buf)
                except OSError as exc:
                    print(f"[ERR] UDP RX buffer set failed: {exc}", file=sys.stderr)
        except OSError as exc:
            print(f"[ERR] UDP bind failed: {exc}", file=sys.stderr)
            can_sock.close()
            udp_tx.close()
            udp_rx.close()
            return 1

    remote = (args.udp_remote_host, args.udp_remote_port)
    if args.mode == "bridge" and not args.allow_self_loop and not udp_broadcast_tx:
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

    can_w = max(1, args.bridge_can_weight)
    print(
        f"[INFO] Started mode={args.mode} can={args.can_iface} "
        f"udp_remote={remote[0]}:{remote[1]} udp_listen={args.udp_listen_port} "
        f"udp_broadcast={udp_broadcast_tx} bridge_can_weight={can_w} "
        f"drain_burst={args.drain_burst} select_timeout={args.select_timeout}s"
    )

    seq = 0
    tx_can2udp = 0
    udp_rx_frames = 0
    rx_udp2can = 0
    can_tx_fail = 0
    dropped_bad_crc = 0
    dropped_can_wrong_len = 0
    last_stats = time.monotonic()

    burst = max(1, args.drain_burst)
    can_leg = burst * can_w

    while not stop:
        read_list = []
        if args.mode in ("can2udp", "bridge"):
            read_list.append(can_sock)
        if args.mode in ("udp2can", "bridge"):
            read_list.append(udp_rx)

        if not read_list:
            time.sleep(0.05)
            continue

        sel_to = args.select_timeout if args.select_timeout > 0 else 0.05
        select.select(read_list, [], [], sel_to)

        if args.mode == "bridge":
            while not stop:
                can_progress = 0
                udp_progress = 0
                for _ in range(can_leg):
                    try:
                        frame = can_sock.recv(CAN_FRAME_STRUCT.size)
                    except BlockingIOError:
                        break
                    except OSError as exc:
                        print(f"[ERR] CAN recv failed: {exc}", file=sys.stderr)
                        break
                    if len(frame) != CAN_FRAME_STRUCT.size:
                        dropped_can_wrong_len += 1
                        continue
                    can_id_raw, dlc, data8 = CAN_FRAME_STRUCT.unpack(frame)
                    payload = build_udp_frame(can_id_raw, dlc, data8, seq)
                    seq += 1
                    try:
                        udp_tx.sendto(payload, remote)
                        tx_can2udp += 1
                        can_progress += 1
                    except OSError as exc:
                        print(f"[ERR] UDP send failed: {exc}", file=sys.stderr)

                for _ in range(burst):
                    try:
                        payload, _src = udp_rx.recvfrom(65535)
                    except BlockingIOError:
                        break
                    except OSError as exc:
                        print(f"[ERR] UDP recv failed: {exc}", file=sys.stderr)
                        break
                    udp_rx_frames += 1
                    ok, can_id_raw, dlc, data8 = parse_udp_frame(payload)
                    if not ok:
                        dropped_bad_crc += 1
                        continue
                    can_frame = CAN_FRAME_STRUCT.pack(can_id_raw, dlc, data8)
                    if can_send_with_backpressure(
                        can_sock,
                        can_frame,
                        should_stop=lambda: stop,
                        max_wait_sec=args.can_send_wait,
                    ):
                        rx_udp2can += 1
                        udp_progress += 1
                    else:
                        can_tx_fail += 1

                if can_progress == 0 and udp_progress == 0:
                    break

        elif args.mode == "can2udp":
            while True:
                try:
                    frame = can_sock.recv(CAN_FRAME_STRUCT.size)
                except BlockingIOError:
                    break
                except OSError as exc:
                    print(f"[ERR] CAN recv failed: {exc}", file=sys.stderr)
                    break
                if len(frame) != CAN_FRAME_STRUCT.size:
                    dropped_can_wrong_len += 1
                    continue
                can_id_raw, dlc, data8 = CAN_FRAME_STRUCT.unpack(frame)
                payload = build_udp_frame(can_id_raw, dlc, data8, seq)
                seq += 1
                try:
                    udp_tx.sendto(payload, remote)
                    tx_can2udp += 1
                except OSError as exc:
                    print(f"[ERR] UDP send failed: {exc}", file=sys.stderr)

        elif args.mode == "udp2can":
            while True:
                try:
                    payload, _src = udp_rx.recvfrom(65535)
                except BlockingIOError:
                    break
                except OSError as exc:
                    print(f"[ERR] UDP recv failed: {exc}", file=sys.stderr)
                    break
                udp_rx_frames += 1
                ok, can_id_raw, dlc, data8 = parse_udp_frame(payload)
                if not ok:
                    dropped_bad_crc += 1
                    continue
                can_frame = CAN_FRAME_STRUCT.pack(can_id_raw, dlc, data8)
                if can_send_with_backpressure(
                    can_sock,
                    can_frame,
                    should_stop=lambda: stop,
                    max_wait_sec=args.can_send_wait,
                ):
                    rx_udp2can += 1
                else:
                    can_tx_fail += 1

        now = time.monotonic()
        if now - last_stats >= args.stats_interval:
            print(
                f"tx_can2udp={tx_can2udp} udp_rx_frames={udp_rx_frames} "
                f"rx_udp2can={rx_udp2can} can_tx_fail={can_tx_fail} "
                f"dropped_bad_crc={dropped_bad_crc} dropped_can_wrong_len={dropped_can_wrong_len}"
            )
            last_stats = now

    print("[INFO] Stopping...")
    can_sock.close()
    udp_tx.close()
    udp_rx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
