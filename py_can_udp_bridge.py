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
import logging
import os
from collections import deque
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


LOGGER = logging.getLogger("can_udp_bridge")


def _is_ipv4_multicast(host: str) -> bool:
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return False
    first_octet = int(ip.split(".", 1)[0])
    return 224 <= first_octet <= 239


def _resolve_iface_ipv4(iface: str) -> str:
    try:
        import fcntl
    except ImportError as exc:
        raise OSError("fcntl is required to resolve interface IPv4 address") from exc
    ifreq = struct.pack("256s", iface.encode("utf-8")[:15])
    res = fcntl.ioctl(sock := socket.socket(socket.AF_INET, socket.SOCK_DGRAM), 0x8915, ifreq)
    sock.close()
    return socket.inet_ntoa(res[20:24])


def _multicast_ifaddr(iface: str, ifaddr: str) -> str:
    if ifaddr:
        return ifaddr
    if iface:
        return _resolve_iface_ipv4(iface)
    return "0.0.0.0"


def _configure_multicast_rx(sock: socket.socket, group_host: str, ifaddr: str) -> None:
    group_ip = socket.gethostbyname(group_host)
    membership = socket.inet_aton(group_ip) + socket.inet_aton(ifaddr)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)


def _configure_multicast_tx(sock: socket.socket, ifaddr: str, ttl: int = 1) -> None:
    if ifaddr != "0.0.0.0":
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ifaddr))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Python CAN <-> UDP bridge")
    p.add_argument("--mode", choices=["can2udp", "udp2can", "bridge"], default="bridge")
    p.add_argument("--can-iface", default="can0", help="CAN interface name")
    p.add_argument(
        "--udp-transport",
        choices=["unicast", "multicast"],
        default="unicast",
        help="UDP transport mode. Unicast supports fixed remote and auto-peers; multicast sends to "
        "and receives from the multicast group in --udp-remote-host.",
    )
    p.add_argument("--udp-remote-host", default="127.0.0.1", help="Remote UDP host")
    p.add_argument("--udp-remote-port", type=int, default=5000, help="Remote UDP port")
    p.add_argument(
        "--udp-multicast-iface",
        default="",
        nargs="?",
        const="",
        help="Interface used for IPv4 multicast TX/RX (e.g. wg0). Optional; overrides routing ambiguity.",
    )
    p.add_argument(
        "--udp-multicast-ifaddr",
        default="",
        nargs="?",
        const="",
        help="Local IPv4 address used for IPv4 multicast TX/RX (e.g. VPN address). "
        "Takes precedence over --udp-multicast-iface.",
    )
    p.add_argument(
        "--udp-auto-peers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-register UDP peers from inbound datagrams and fan out CAN->UDP to all active peers "
        "(default: on). Use --no-udp-auto-peers for single remote peer mode.",
    )
    p.add_argument(
        "--udp-peer-ttl-sec",
        type=float,
        default=30.0,
        metavar="SEC",
        help="Peer TTL for auto-registered UDP peers (default: 30). Inactive peers are evicted.",
    )
    p.add_argument(
        "--udp-max-peers",
        type=int,
        default=64,
        metavar="N",
        help="Max number of active UDP peers in auto-peer mode (default: 64).",
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
    p.add_argument(
        "--bridge-order",
        choices=["can_first", "udp_first", "interleaved"],
        default="can_first",
        help="Bridge inner loop: can_first (RPi / sniff-heavy) drains CAN->UDP then UDP->CAN; "
        "udp_first drains UDP->CAN first (good when PC only listens, bad if the same iface also "
        "hosts an active DroneCAN node — local TX waits behind up to drain-burst remote frames); "
        "interleaved alternates one UDP frame and one CAN frame up to drain-burst cycles (safest "
        "when one host both injects and tunnels).",
    )
    p.add_argument(
        "--udp-pending-max",
        type=int,
        default=65536,
        metavar="N",
        help="Max queued CAN->UDP datagrams if UDP send would block (non-blocking TX); "
        "avoids CAN RX overflow on bursts. Very full queue logs once.",
    )
    p.add_argument(
        "--can-pending-max",
        type=int,
        default=65536,
        metavar="N",
        help="Max queued UDP->CAN frames if CAN send would block (e.g. slcan/USB serial); "
        "avoids stalling UDP recv and gaps in multi-frame transfers. Default matches udp-pending-max.",
    )
    p.add_argument(
        "--udp-drop-out-of-order",
        action="store_true",
        help="Drop late/out-of-order UDP frames on UDP->CAN path using per-packet seq number. "
        "Can reduce corrupted multi-frame transfers on jittery links at cost of possible frame drops.",
    )
    if argv is None:
        argv = sys.argv[1:]
    # systemd EnvironmentFile expansions may occasionally yield empty/whitespace-only args
    # (e.g. optional flag variables). Drop them to avoid argparse "unrecognized arguments:".
    clean_argv = [a for a in argv if a and a.strip()]
    return p.parse_args(clean_argv)


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


def parse_udp_frame(payload: bytes) -> tuple[bool, int, int, bytes, int]:
    if len(payload) != UDP_FRAME_STRUCT.size:
        return False, 0, 0, b"", 0
    magic, version, flags, can_id, dlc, data8, seq, crc = UDP_FRAME_STRUCT.unpack(payload)
    if magic != UDP_MAGIC or version != UDP_VERSION or dlc > 8:
        return False, 0, 0, b"", 0
    calc_crc = zlib.crc32(payload[:-4]) & 0xFFFFFFFF
    if calc_crc != crc:
        return False, 0, 0, b"", 0

    can_id_raw = (can_id & CAN_EFF_MASK) if (flags & FLAG_EXTENDED) else (can_id & CAN_SFF_MASK)
    if flags & FLAG_EXTENDED:
        can_id_raw |= CAN_EFF_FLAG
    if flags & FLAG_RTR:
        can_id_raw |= CAN_RTR_FLAG
    if flags & FLAG_ERROR:
        can_id_raw |= CAN_ERR_FLAG

    return True, can_id_raw, dlc, data8, seq


def _send_would_block(exc: BaseException) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
    return False


def _is_seq_ahead(expected: int, got: int) -> bool:
    """
    Compare uint32 sequence numbers with wrap-around semantics.
    Returns True when got is ahead of expected in modular space.
    """
    return ((got - expected) & 0xFFFFFFFF) < 0x80000000


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.name != "posix":
        LOGGER.error("This script requires Linux SocketCAN (posix)")
        return 1

    args = parse_args(sys.argv[1:])
    if args.udp_transport == "multicast" and not _is_ipv4_multicast(args.udp_remote_host):
        LOGGER.error("Multicast transport requires --udp-remote-host to be an IPv4 multicast group")
        return 2
    multicast_ifaddr = "0.0.0.0"
    if args.udp_transport == "multicast":
        try:
            multicast_ifaddr = _multicast_ifaddr(args.udp_multicast_iface, args.udp_multicast_ifaddr)
        except OSError as exc:
            LOGGER.error("Multicast interface setup failed: %s", exc)
            return 2
        args.udp_auto_peers = False
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
                LOGGER.warning("CAN_RAW_LOOPBACK=0 not applied: %s", exc)
        if args.can_rx_buf > 0:
            try:
                can_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.can_rx_buf)
            except OSError as exc:
                LOGGER.error("CAN RX buffer set failed: %s", exc)
        if args.can_tx_buf > 0:
            try:
                can_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, args.can_tx_buf)
            except OSError as exc:
                LOGGER.error("CAN TX buffer set failed: %s", exc)
    except OSError as exc:
        LOGGER.error("CAN init failed: %s", exc)
        return 1

    udp_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_tx.setblocking(False)
    udp_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_rx.setblocking(False)
    if args.udp_tx_buf > 0:
        try:
            udp_tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, args.udp_tx_buf)
        except OSError as exc:
            LOGGER.error("UDP TX buffer set failed: %s", exc)
    if args.udp_transport == "multicast":
        try:
            _configure_multicast_tx(udp_tx, multicast_ifaddr)
        except OSError as exc:
            LOGGER.error("UDP multicast TX setup failed: %s", exc)

    if args.mode in ("udp2can", "bridge"):
        try:
            if args.udp_transport == "multicast":
                _configure_multicast_rx(udp_rx, args.udp_remote_host, multicast_ifaddr)
            udp_rx.bind(("0.0.0.0", args.udp_listen_port))
            if args.udp_rx_buf > 0:
                try:
                    udp_rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.udp_rx_buf)
                except OSError as exc:
                    LOGGER.error("UDP RX buffer set failed: %s", exc)
        except OSError as exc:
            LOGGER.error("UDP bind/setup failed: %s", exc)
            can_sock.close()
            udp_tx.close()
            udp_rx.close()
            return 1

    # In bridge mode use the bound RX socket for TX as well, so outbound packets
    # originate from udp-listen-port (important for peer replies/NAT mapping).
    udp_send_sock = udp_rx if args.mode == "bridge" else udp_tx
    if args.udp_tx_buf > 0 and args.mode == "bridge":
        try:
            udp_send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, args.udp_tx_buf)
        except OSError as exc:
            LOGGER.error("UDP TX buffer (bridge send sock) set failed: %s", exc)
    if args.udp_transport == "multicast" and args.mode == "bridge":
        try:
            _configure_multicast_tx(udp_send_sock, multicast_ifaddr)
        except OSError as exc:
            LOGGER.error("UDP multicast TX setup (bridge send sock) failed: %s", exc)

    remote = (args.udp_remote_host, args.udp_remote_port)
    peers_last_seen: dict[tuple[str, int], float] = {}
    peer_ttl = max(1.0, args.udp_peer_ttl_sec)
    peer_max = max(1, args.udp_max_peers)

    def _touch_peer(peer: tuple[str, int], *, now: float | None = None) -> None:
        if args.udp_transport != "unicast" or not args.udp_auto_peers:
            return
        ts = time.monotonic() if now is None else now
        if peer in peers_last_seen:
            peers_last_seen[peer] = ts
            return
        if len(peers_last_seen) >= peer_max:
            # Evict oldest peer to keep registration zero-config.
            oldest = min(peers_last_seen.items(), key=lambda kv: kv[1])[0]
            oldest_idle = ts - peers_last_seen[oldest]
            peers_last_seen.pop(oldest, None)
            LOGGER.info(
                "PEER evicted oldest %s:%s idle=%.1fs (max_peers=%s)",
                oldest[0],
                oldest[1],
                oldest_idle,
                peer_max,
            )
        peers_last_seen[peer] = ts
        LOGGER.info("PEER connected %s:%s active_peers=%s", peer[0], peer[1], len(peers_last_seen))

    def _gc_peers(*, now: float | None = None) -> None:
        if args.udp_transport != "unicast" or not args.udp_auto_peers:
            return
        ts = time.monotonic() if now is None else now
        stale = [(p, ts - last) for p, last in peers_last_seen.items() if (ts - last) > peer_ttl]
        for p, idle in stale:
            peers_last_seen.pop(p, None)
            udp_seq_expected_by_peer.pop(p, None)
            LOGGER.info(
                "PEER disconnected %s:%s idle=%.1fs (ttl=%.1fs) active_peers=%s",
                p[0],
                p[1],
                idle,
                peer_ttl,
                len(peers_last_seen),
            )

    def _build_udp_targets() -> list[tuple[str, int]]:
        if args.udp_transport == "multicast" or not args.udp_auto_peers:
            return [remote]
        targets = [remote]
        targets.extend(peer for peer in peers_last_seen.keys() if peer != remote)
        return targets
    if args.mode == "bridge" and args.udp_transport == "unicast" and not args.allow_self_loop:
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
            LOGGER.error(
                "Self-loop detected: udp-remote-host points to local host and udp-remote-port "
                "equals udp-listen-port. Use different host/port or pass --allow-self-loop intentionally."
            )
            can_sock.close()
            udp_tx.close()
            udp_rx.close()
            return 2

    can_w = max(1, args.bridge_can_weight)
    LOGGER.info(
        "start mode=%s can=%s udp=%s remote=%s:%s listen=%s mcast_if=%s peers=%s order=%s burst=%s",
        args.mode,
        args.can_iface,
        args.udp_transport,
        remote[0],
        remote[1],
        args.udp_listen_port,
        multicast_ifaddr if args.udp_transport == "multicast" else "-",
        args.udp_auto_peers,
        args.bridge_order,
        args.drain_burst,
    )

    seq = 0
    tx_can2udp = 0
    udp_rx_frames = 0
    rx_udp2can = 0
    can_tx_fail = 0
    dropped_bad_crc = 0
    dropped_can_wrong_len = 0
    udp_seq_gap_frames = 0
    udp_seq_out_of_order = 0
    udp_seq_expected_by_peer: dict[tuple[str, int], int] = {}
    udp_seq_dropped_ooo = 0
    udp_pending_peak = 0
    udp_pending_full = 0  # times CAN->UDP stopped reading CAN because pending queue was full
    last_stats = time.monotonic()

    pending_udp: deque[tuple[bytes, tuple[str, int]]] = deque()
    udp_pending_max = max(32, args.udp_pending_max)
    pending_can: deque[bytes] = deque()
    can_pending_max = max(32, args.can_pending_max)
    can_pending_peak = 0
    can_pending_full = 0

    def flush_udp_pending() -> None:
        nonlocal tx_can2udp, udp_pending_peak
        while pending_udp:
            try:
                payload0, peer0 = pending_udp[0]
                udp_send_sock.sendto(payload0, peer0)
                pending_udp.popleft()
                tx_can2udp += 1
            except (BlockingIOError, OSError) as exc:
                if _send_would_block(exc):
                    break
                LOGGER.error("UDP send failed: %s", exc)
                pending_udp.popleft()
                break
        udp_pending_peak = max(udp_pending_peak, len(pending_udp))

    def flush_can_pending() -> None:
        nonlocal rx_udp2can, can_pending_peak
        while pending_can:
            try:
                can_sock.send(pending_can[0])
                pending_can.popleft()
                rx_udp2can += 1
            except (BlockingIOError, OSError) as exc:
                if _send_would_block(exc):
                    break
                LOGGER.error("CAN send failed: %s", exc)
                pending_can.popleft()
                break
        can_pending_peak = max(can_pending_peak, len(pending_can))

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
        write_list = []
        if pending_udp:
            write_list.append(udp_send_sock)
        if pending_can:
            write_list.append(can_sock)
        try:
            select.select(read_list, write_list, [], sel_to)
        except InterruptedError:
            continue

        flush_udp_pending()
        flush_can_pending()
        _gc_peers()

        if args.mode in ("can2udp", "bridge") and len(pending_udp) >= udp_pending_max:
            udp_pending_full += 1
        if args.mode in ("udp2can", "bridge") and len(pending_can) >= can_pending_max:
            can_pending_full += 1

        if args.mode == "bridge":
            while not stop:
                flush_udp_pending()
                flush_can_pending()
                can_progress = 0
                udp_progress = 0

                def try_one_udp_to_can() -> bool:
                    nonlocal udp_progress, udp_rx_frames, rx_udp2can, can_tx_fail, dropped_bad_crc, can_pending_peak
                    nonlocal udp_seq_gap_frames, udp_seq_out_of_order, udp_seq_dropped_ooo
                    if len(pending_can) >= can_pending_max:
                        return False
                    try:
                        payload, src = udp_rx.recvfrom(65535)
                    except BlockingIOError:
                        return False
                    except OSError as exc:
                        LOGGER.error("UDP recv failed: %s", exc)
                        return False
                    udp_rx_frames += 1
                    ok, can_id_raw, dlc, data8, seq_rx = parse_udp_frame(payload)
                    if not ok:
                        dropped_bad_crc += 1
                        return True
                    peer = (src[0], int(src[1]))
                    _touch_peer(peer)
                    expected = udp_seq_expected_by_peer.get(peer)
                    if expected is None:
                        udp_seq_expected_by_peer[peer] = (seq_rx + 1) & 0xFFFFFFFF
                    elif seq_rx != expected:
                        if _is_seq_ahead(expected, seq_rx):
                            udp_seq_gap_frames += (seq_rx - expected) & 0xFFFFFFFF
                            udp_seq_expected_by_peer[peer] = (seq_rx + 1) & 0xFFFFFFFF
                        else:
                            udp_seq_out_of_order += 1
                            if args.udp_drop_out_of_order:
                                udp_seq_dropped_ooo += 1
                                return True
                    else:
                        udp_seq_expected_by_peer[peer] = (expected + 1) & 0xFFFFFFFF
                    can_frame = CAN_FRAME_STRUCT.pack(can_id_raw, dlc, data8)
                    try:
                        can_sock.send(can_frame)
                        rx_udp2can += 1
                        udp_progress += 1
                    except (BlockingIOError, OSError) as exc:
                        if _send_would_block(exc):
                            pending_can.append(can_frame)
                            udp_progress += 1
                            can_pending_peak = max(can_pending_peak, len(pending_can))
                        else:
                            LOGGER.error("CAN send failed: %s", exc)
                            can_tx_fail += 1
                    return True

                def try_one_can_to_udp() -> bool:
                    nonlocal can_progress, seq, tx_can2udp, udp_pending_peak, dropped_can_wrong_len
                    if len(pending_udp) >= udp_pending_max:
                        return False
                    try:
                        frame = can_sock.recv(CAN_FRAME_STRUCT.size)
                    except BlockingIOError:
                        return False
                    except OSError as exc:
                        LOGGER.error("CAN recv failed: %s", exc)
                        return False
                    if len(frame) != CAN_FRAME_STRUCT.size:
                        dropped_can_wrong_len += 1
                        return True
                    can_id_raw, dlc, data8 = CAN_FRAME_STRUCT.unpack(frame)
                    payload = build_udp_frame(can_id_raw, dlc, data8, seq)
                    seq += 1
                    now_send = time.monotonic()
                    _gc_peers(now=now_send)
                    for peer in _build_udp_targets():
                        try:
                            udp_send_sock.sendto(payload, peer)
                            tx_can2udp += 1
                            _touch_peer(peer, now=now_send)
                        except (BlockingIOError, OSError) as exc:
                            if _send_would_block(exc):
                                pending_udp.append((payload, peer))
                                udp_pending_peak = max(udp_pending_peak, len(pending_udp))
                                continue
                            LOGGER.error("UDP send failed to %s:%s: %s", peer[0], peer[1], exc)
                    can_progress += 1
                    return True

                def bridge_leg_can_to_udp() -> None:
                    nonlocal can_progress, seq, tx_can2udp, udp_pending_peak, dropped_can_wrong_len
                    for _ in range(can_leg):
                        if len(pending_udp) >= udp_pending_max:
                            break
                        try:
                            frame = can_sock.recv(CAN_FRAME_STRUCT.size)
                        except BlockingIOError:
                            break
                        except OSError as exc:
                            LOGGER.error("CAN recv failed: %s", exc)
                            break
                        if len(frame) != CAN_FRAME_STRUCT.size:
                            dropped_can_wrong_len += 1
                            continue
                        can_id_raw, dlc, data8 = CAN_FRAME_STRUCT.unpack(frame)
                        payload = build_udp_frame(can_id_raw, dlc, data8, seq)
                        seq += 1
                        now_send = time.monotonic()
                        _gc_peers(now=now_send)
                        for peer in _build_udp_targets():
                            try:
                                udp_send_sock.sendto(payload, peer)
                                tx_can2udp += 1
                                _touch_peer(peer, now=now_send)
                            except (BlockingIOError, OSError) as exc:
                                if _send_would_block(exc):
                                    pending_udp.append((payload, peer))
                                    udp_pending_peak = max(udp_pending_peak, len(pending_udp))
                                    continue
                                LOGGER.error("UDP send failed to %s:%s: %s", peer[0], peer[1], exc)
                        can_progress += 1

                def bridge_leg_udp_to_can() -> None:
                    nonlocal udp_progress, udp_rx_frames, rx_udp2can, can_tx_fail, dropped_bad_crc, can_pending_peak
                    nonlocal udp_seq_gap_frames, udp_seq_out_of_order, udp_seq_dropped_ooo
                    for _ in range(burst):
                        if len(pending_can) >= can_pending_max:
                            break
                        try:
                            payload, src = udp_rx.recvfrom(65535)
                        except BlockingIOError:
                            break
                        except OSError as exc:
                            LOGGER.error("UDP recv failed: %s", exc)
                            break
                        udp_rx_frames += 1
                        ok, can_id_raw, dlc, data8, seq_rx = parse_udp_frame(payload)
                        if not ok:
                            dropped_bad_crc += 1
                            continue
                        peer = (src[0], int(src[1]))
                        _touch_peer(peer)
                        expected = udp_seq_expected_by_peer.get(peer)
                        if expected is None:
                            udp_seq_expected_by_peer[peer] = (seq_rx + 1) & 0xFFFFFFFF
                        elif seq_rx != expected:
                            if _is_seq_ahead(expected, seq_rx):
                                udp_seq_gap_frames += (seq_rx - expected) & 0xFFFFFFFF
                                udp_seq_expected_by_peer[peer] = (seq_rx + 1) & 0xFFFFFFFF
                            else:
                                udp_seq_out_of_order += 1
                                if args.udp_drop_out_of_order:
                                    udp_seq_dropped_ooo += 1
                                    continue
                        else:
                            udp_seq_expected_by_peer[peer] = (expected + 1) & 0xFFFFFFFF
                        can_frame = CAN_FRAME_STRUCT.pack(can_id_raw, dlc, data8)
                        try:
                            can_sock.send(can_frame)
                            rx_udp2can += 1
                            udp_progress += 1
                        except (BlockingIOError, OSError) as exc:
                            if _send_would_block(exc):
                                pending_can.append(can_frame)
                                udp_progress += 1
                                can_pending_peak = max(can_pending_peak, len(pending_can))
                                break
                            LOGGER.error("CAN send failed: %s", exc)
                            can_tx_fail += 1

                if args.bridge_order == "interleaved":
                    for _ in range(burst):
                        u = try_one_udp_to_can()
                        c = try_one_can_to_udp()
                        if not u and not c:
                            break
                elif args.bridge_order == "udp_first":
                    bridge_leg_udp_to_can()
                    bridge_leg_can_to_udp()
                else:
                    bridge_leg_can_to_udp()
                    bridge_leg_udp_to_can()

                if can_progress == 0 and udp_progress == 0:
                    break

        elif args.mode == "can2udp":
            while True:
                if len(pending_udp) >= udp_pending_max:
                    break
                try:
                    frame = can_sock.recv(CAN_FRAME_STRUCT.size)
                except BlockingIOError:
                    break
                except OSError as exc:
                    LOGGER.error("CAN recv failed: %s", exc)
                    break
                if len(frame) != CAN_FRAME_STRUCT.size:
                    dropped_can_wrong_len += 1
                    continue
                can_id_raw, dlc, data8 = CAN_FRAME_STRUCT.unpack(frame)
                payload = build_udp_frame(can_id_raw, dlc, data8, seq)
                seq += 1
                now_send = time.monotonic()
                _gc_peers(now=now_send)
                for peer in _build_udp_targets():
                    try:
                        udp_send_sock.sendto(payload, peer)
                        tx_can2udp += 1
                        _touch_peer(peer, now=now_send)
                    except (BlockingIOError, OSError) as exc:
                        if _send_would_block(exc):
                            pending_udp.append((payload, peer))
                            udp_pending_peak = max(udp_pending_peak, len(pending_udp))
                            continue
                        LOGGER.error("UDP send failed to %s:%s: %s", peer[0], peer[1], exc)

        elif args.mode == "udp2can":
            while True:
                if len(pending_can) >= can_pending_max:
                    break
                try:
                    payload, src = udp_rx.recvfrom(65535)
                except BlockingIOError:
                    break
                except OSError as exc:
                    LOGGER.error("UDP recv failed: %s", exc)
                    break
                udp_rx_frames += 1
                ok, can_id_raw, dlc, data8, seq_rx = parse_udp_frame(payload)
                if not ok:
                    dropped_bad_crc += 1
                    continue
                peer = (src[0], int(src[1]))
                _touch_peer(peer)
                expected = udp_seq_expected_by_peer.get(peer)
                if expected is None:
                    udp_seq_expected_by_peer[peer] = (seq_rx + 1) & 0xFFFFFFFF
                elif seq_rx != expected:
                    if _is_seq_ahead(expected, seq_rx):
                        udp_seq_gap_frames += (seq_rx - expected) & 0xFFFFFFFF
                        udp_seq_expected_by_peer[peer] = (seq_rx + 1) & 0xFFFFFFFF
                    else:
                        udp_seq_out_of_order += 1
                        if args.udp_drop_out_of_order:
                            udp_seq_dropped_ooo += 1
                            continue
                else:
                    udp_seq_expected_by_peer[peer] = (expected + 1) & 0xFFFFFFFF
                can_frame = CAN_FRAME_STRUCT.pack(can_id_raw, dlc, data8)
                try:
                    can_sock.send(can_frame)
                    rx_udp2can += 1
                except (BlockingIOError, OSError) as exc:
                    if _send_would_block(exc):
                        pending_can.append(can_frame)
                        can_pending_peak = max(can_pending_peak, len(pending_can))
                        break
                    LOGGER.error("CAN send failed: %s", exc)
                    can_tx_fail += 1

        now = time.monotonic()
        if now - last_stats >= args.stats_interval:
            LOGGER.info(
                "stats can2udp=%s udp2can=%s udp_rx=%s peers=%s q_udp=%s/%s q_can=%s/%s "
                "drop_crc=%s drop_len=%s seq_gap=%s seq_ooo=%s can_fail=%s",
                tx_can2udp,
                rx_udp2can,
                udp_rx_frames,
                len(peers_last_seen),
                len(pending_udp),
                udp_pending_peak,
                len(pending_can),
                can_pending_peak,
                dropped_bad_crc,
                dropped_can_wrong_len,
                udp_seq_gap_frames,
                udp_seq_out_of_order,
                can_tx_fail,
            )
            last_stats = now

    LOGGER.info("Stopping...")
    can_sock.close()
    if udp_send_sock is udp_rx:
        udp_rx.close()
        udp_tx.close()
    else:
        udp_tx.close()
        udp_rx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
