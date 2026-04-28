#!/usr/bin/env bash
set -euo pipefail

# Install and configure:
# - WireGuard (wg-quick@<config>.service)
# - SPI/native CAN (rpi-can-spi.service) when CAN_SOURCE is spi or auto
# - USB SLCAN via setup_slcan (rpi-slcan.service + udev hotplug) when slcan or auto
# - Bridge CAN_IFACE chosen at start (rpi-can-pick-iface.sh) unless forced by CAN_SOURCE
# - Python CAN<->UDP bridge
#
# Usage examples:
#   sudo ./install_rpi_can_vpn_bridge.sh
#   sudo ./install_rpi_can_vpn_bridge.sh --config ./my-install.conf
#   sudo ./install_rpi_can_vpn_bridge.sh --config ./my-install.conf --wg-config wg0
#
# If --config is passed more than once, the last file wins. CLI options override
# values from the config file.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE=""
BRIDGE_SCRIPT="${SCRIPT_DIR}/py_can_udp_bridge.py"
SETUP_SLCAN_SRC="${SCRIPT_DIR}/scripts/setup_slcan"
SLCAN_ATTACH_SRC="${SCRIPT_DIR}/scripts/rpi-slcan-attach.sh"
SLCAN_WATCHDOG_SRC="${SCRIPT_DIR}/scripts/rpi-slcan-watchdog.sh"
BRIDGE_WATCHDOG_SRC="${SCRIPT_DIR}/scripts/rpi-can-udp-bridge-watchdog.sh"
PICK_IFACE_SRC="${SCRIPT_DIR}/scripts/rpi-can-pick-iface.sh"

WG_CONFIG_INPUT="wg0"
CAN_SOURCE="auto"
SPI_CAN_IFACE="can0"
SLCAN_IFACE="slcan0"
CAN_BITRATE="1000000"
SKIP_UPGRADE="0"
SLCAN_WATCHDOG_INTERVAL="15s"
BRIDGE_WATCHDOG_WAIT_MAX_SEC="120"

UDEV_RULE_FILE="/etc/udev/rules.d/99-rpi-slcan.rules"

print_help() {
  cat <<'EOF'
Installs WireGuard, CAN tooling, optional MCP2515/SPI boot config, USB SLCAN
(setup_slcan by Pavel Kirienko), and py_can_udp_bridge.py as systemd services.

CAN_SOURCE:
  spi   — SPI/native CAN only (MCP2515 boot overlay + ip link); USB SLCAN off
  slcan — USB SLCAN only; no SPI overlay changes
  auto  — configure SPI if needed, enable SLCAN; bridge uses slcan0 if UP else SPI can

Options:
  --config|-c <file>       Shell-style KEY=value file (sourced before CLI; CLI wins).
                           May appear anywhere; if repeated, the last file wins.
  --wg-config <name|path>  WireGuard config name (wg0) or .conf path
  --can-source <spi|slcan|auto>
  --can-iface <name>       SPI/native SocketCAN name (default: can0); maps to SPI_CAN_IFACE
  --can-bitrate <rate>     SPI/native CAN bitrate (default: 1000000)
  --slcan-iface <name>     SLCAN netdev name (default: slcan0); maps to SLCAN_IFACE
  --slcan-watchdog-interval <sec|systemd-time>
                           Watchdog period (default: 15s, e.g. 5s, 30s, 1min)
  --skip-upgrade           Skip apt full-upgrade step
  -h, --help               Show help

Recognized keys in --config (optional unless noted):
  WG_CONFIG_INPUT   WireGuard: name or path to .conf (default: wg0)
  CAN_SOURCE        spi | slcan | auto
  SPI_CAN_IFACE     SPI/native CAN netdev (default: can0)
  SLCAN_IFACE       Preferred SLCAN netdev for pick-iface (default: slcan0)
  CAN_BITRATE       SPI/native bitrate (default: 1000000)
  SKIP_UPGRADE      1 to skip apt full-upgrade
  ENABLE_SLCAN      0/1 for /etc/default/rpi-slcan (default: from CAN_SOURCE)
  SLCAN_SPEED_CODE  SLCAN speed code 0..8 (default: 8)
  SLCAN_SERIAL_BAUD Serial baud for slcand (default: 921600)
  SLCAN_BASENAME    Interface basename (default: slcan)
  SLCAN_SILENT      1 for listen-only SLCAN
  SLCAN_DEVICES     Space-separated TTY list, or empty for auto ttyUSB*/ttyACM*
  SLCAN_WATCHDOG_INTERVAL  Timer period to auto-recover SLCAN (default: 15s)
  BRIDGE_WATCHDOG_WAIT_MAX_SEC Max wait for SLCAN iface before bridge restart (default: 120)
  MODE              Bridge mode: bridge | can2udp | udp2can
  UDP_REMOTE_HOST   Remote UDP host
  UDP_REMOTE_PORT   Remote UDP port
  UDP_LISTEN_PORT   Local UDP listen port
  STATS_INTERVAL    Stats print interval (seconds)
  UDP_BROADCAST     1 to enable --udp-broadcast
  UDP_PENDING_MAX   Max queued CAN->UDP datagrams if UDP send would block (default: 65536)
  CAN_PENDING_MAX   Max queued UDP->CAN frames if CAN send would block (default: 65536; matters for slcan)
  UDP_AUTO_PEERS    1 enables auto peer registration from inbound UDP (default: 1)
  UDP_PEER_TTL_SEC  Auto peer TTL seconds (default: 30)
  UDP_MAX_PEERS     Max active auto peers (default: 64)
  BRIDGE_ORDER      can_first (RPi default) | udp_first (PC listen-only) | interleaved (PC + active node on same CAN)
  DRAIN_BURST       Frames per bridge leg before switching direction (default: 4 for low latency)
  BRIDGE_CAN_WEIGHT CAN->UDP leg multiplier in bridge mode (default: 1)
  SELECT_TIMEOUT    select() timeout seconds when idle (default: 0.001 for low latency)
  UDP_DROP_OUT_OF_ORDER 1 to drop late/out-of-order UDP frames before UDP->CAN inject

Configs after install:
  /etc/default/rpi-can-hardware   — CAN_SOURCE, SPI_CAN_IFACE, SLCAN_IFACE, CAN_BITRATE
  /etc/default/rpi-slcan        — ENABLE_SLCAN, SLCAN_SPEED_CODE, SLCAN_DEVICES, ...
  rpi-slcan-watchdog.timer      — periodic SLCAN recovery checks
  rpi-can-udp-bridge-watchdog.timer — poll SLCAN; restart bridge only if iface was missing then returned
  /etc/default/rpi-can-udp-bridge — UDP bridge settings (CAN_IFACE set at runtime)

Example install config: install_rpi_can_vpn_bridge.conf.example
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

err() {
  printf '[ERR] %s\n' "$*" >&2
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    err "Please run as root: sudo $0 ..."
    exit 1
  fi
}

require_bridge_script() {
  if [[ ! -f "${BRIDGE_SCRIPT}" ]]; then
    err "Bridge script not found: ${BRIDGE_SCRIPT}"
    exit 1
  fi
}

require_bundled_scripts() {
  if [[ ! -f "${SETUP_SLCAN_SRC}" || ! -f "${SLCAN_ATTACH_SRC}" || ! -f "${SLCAN_WATCHDOG_SRC}" || ! -f "${BRIDGE_WATCHDOG_SRC}" || ! -f "${PICK_IFACE_SRC}" ]]; then
    err "Missing bundled scripts under ${SCRIPT_DIR}/scripts/"
    exit 1
  fi
}

load_install_config() {
  local f="$1"
  [[ -f "${f}" ]] || { err "Config file not found: ${f}"; exit 1; }
  log "Loading config: ${f}"
  # shellcheck disable=SC1090
  set -a
  # shellcheck source=/dev/null
  source "${f}"
  set +a
}

parse_args() {
  local -a all=("$@")
  local -a rest=()
  local i=0
  CONFIG_FILE=""
  while [[ "${i}" -lt ${#all[@]} ]]; do
    case "${all[i]}" in
      --config|-c)
        (( i + 1 < ${#all[@]} )) || { err "--config requires a path"; exit 1; }
        CONFIG_FILE="${all[i + 1]}"
        i=$((i + 2))
        ;;
      -h|--help)
        print_help
        exit 0
        ;;
      *)
        rest+=("${all[i]}")
        i=$((i + 1))
        ;;
    esac
  done

  if [[ -n "${CONFIG_FILE}" ]]; then
    load_install_config "${CONFIG_FILE}"
  fi

  set -- "${rest[@]}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --wg-config)
        [[ $# -ge 2 ]] || { err "--wg-config requires a value"; exit 1; }
        WG_CONFIG_INPUT="$2"
        shift 2
        ;;
      --can-source)
        [[ $# -ge 2 ]] || { err "--can-source requires a value"; exit 1; }
        CAN_SOURCE="$2"
        shift 2
        ;;
      --slcan-iface)
        [[ $# -ge 2 ]] || { err "--slcan-iface requires a value"; exit 1; }
        SLCAN_IFACE="$2"
        shift 2
        ;;
      --can-iface)
        [[ $# -ge 2 ]] || { err "--can-iface requires a value"; exit 1; }
        SPI_CAN_IFACE="$2"
        shift 2
        ;;
      --can-bitrate)
        [[ $# -ge 2 ]] || { err "--can-bitrate requires a value"; exit 1; }
        CAN_BITRATE="$2"
        shift 2
        ;;
      --slcan-watchdog-interval)
        [[ $# -ge 2 ]] || { err "--slcan-watchdog-interval requires a value"; exit 1; }
        SLCAN_WATCHDOG_INTERVAL="$2"
        shift 2
        ;;
      --skip-upgrade)
        SKIP_UPGRADE="1"
        shift
        ;;
      -h|--help)
        print_help
        exit 0
        ;;
      *)
        err "Unknown argument: $1"
        print_help
        exit 1
        ;;
    esac
  done

  case "${CAN_SOURCE}" in
    spi|slcan|auto) ;;
    *)
      err "Invalid CAN_SOURCE (use spi, slcan, or auto): ${CAN_SOURCE}"
      exit 1
      ;;
  esac
}

resolve_wg_config() {
  local input="$1"
  local cfg_name cfg_file

  mkdir -p /etc/wireguard
  chmod 700 /etc/wireguard

  if [[ "${input}" == */* || "${input}" == *.conf ]]; then
    cfg_file="${input}"
    [[ -f "${cfg_file}" ]] || { err "WireGuard config file not found: ${cfg_file}"; exit 1; }
    cfg_name="$(basename "${cfg_file}" .conf)"
    install -m 600 "${cfg_file}" "/etc/wireguard/${cfg_name}.conf"
    log "Installed WireGuard config: /etc/wireguard/${cfg_name}.conf"
  else
    cfg_name="${input%.conf}"
    cfg_file="/etc/wireguard/${cfg_name}.conf"
    [[ -f "${cfg_file}" ]] || {
      err "Expected WireGuard config not found: ${cfg_file}"
      err "Provide --wg-config /path/to/<name>.conf or place it in /etc/wireguard"
      exit 1
    }
  fi

  WG_CONFIG_NAME="${cfg_name}"
}

install_packages() {
  log "Installing required packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  if [[ "${SKIP_UPGRADE}" != "1" ]]; then
    apt-get -y full-upgrade
  fi
  apt-get install -y \
    wireguard \
    wireguard-tools \
    resolvconf \
    can-utils \
    iproute2 \
    net-tools \
    python3 \
    python3-venv
}

boot_has_line() {
  local f="$1"
  local pat="$2"
  grep -qxF "${pat}" "${f}" 2>/dev/null
}

configure_boot_for_spi_can() {
  # From can_setup.md — interface name from overlay is typically can0 (mcp2515-can0).
  local cfg_file=""
  if [[ -f /boot/firmware/config.txt ]]; then
    cfg_file="/boot/firmware/config.txt"
  elif [[ -f /boot/config.txt ]]; then
    cfg_file="/boot/config.txt"
  else
    log "Boot config not found at /boot/config.txt or /boot/firmware/config.txt, skipping SPI overlay config"
    return 0
  fi

  log "Configuring SPI and MCP2515 in ${cfg_file}"
  if ! grep -q '^\[all\]$' "${cfg_file}" 2>/dev/null; then
    printf '\n[all]\n' >> "${cfg_file}"
  fi
  if ! boot_has_line "${cfg_file}" "dtparam=spi=on"; then
    printf 'dtparam=spi=on\n' >> "${cfg_file}"
  fi
  if ! boot_has_line "${cfg_file}" "dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=12"; then
    printf 'dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=12\n' >> "${cfg_file}"
  fi
}

write_can_hardware_defaults() {
  local f="/etc/default/rpi-can-hardware"
  cat > "${f}" <<EOF
# CAN_SOURCE: spi | slcan | auto
# auto: prefer USB SLCAN (SLCAN_IFACE) if link is UP, else SPI/native (SPI_CAN_IFACE)
CAN_SOURCE=${CAN_SOURCE}
SPI_CAN_IFACE=${SPI_CAN_IFACE}
SLCAN_IFACE=${SLCAN_IFACE}
CAN_BITRATE=${CAN_BITRATE}
EOF
  chmod 644 "${f}"
}

write_slcan_defaults() {
  local f="/etc/default/rpi-slcan"
  local enable
  if [ "${ENABLE_SLCAN+set}" = set ]; then
    enable="${ENABLE_SLCAN}"
  elif [[ "${CAN_SOURCE}" == "spi" ]]; then
    enable=0
  else
    enable=1
  fi
  : "${SLCAN_SPEED_CODE:=8}"
  : "${SLCAN_SERIAL_BAUD:=921600}"
  : "${SLCAN_BASENAME:=slcan}"
  : "${SLCAN_SILENT:=0}"
  : "${SLCAN_DEVICES:=}"
  : "${SLCAN_WATCHDOG_INTERVAL:=15s}"
  : "${BRIDGE_WATCHDOG_WAIT_MAX_SEC:=120}"
  {
    cat <<EOF
# Set to 0 to disable USB SLCAN attach (see setup_slcan gist)
ENABLE_SLCAN=${enable}
# SLCAN speed code 0..8 (8 = 1 Mbps); see setup_slcan --help
SLCAN_SPEED_CODE=${SLCAN_SPEED_CODE}
SLCAN_SERIAL_BAUD=${SLCAN_SERIAL_BAUD}
SLCAN_BASENAME=${SLCAN_BASENAME}
SLCAN_SILENT=${SLCAN_SILENT}
# Space-separated TTY list, or empty for all /dev/ttyUSB* /dev/ttyACM*
SLCAN_WATCHDOG_INTERVAL=${SLCAN_WATCHDOG_INTERVAL}
BRIDGE_WATCHDOG_WAIT_MAX_SEC=${BRIDGE_WATCHDOG_WAIT_MAX_SEC}
EOF
    printf 'SLCAN_DEVICES=%q\n' "${SLCAN_DEVICES}"
  } > "${f}"
  chmod 644 "${f}"
}

install_helpers() {
  install -m 755 "${SETUP_SLCAN_SRC}" /usr/local/bin/setup_slcan
  install -m 755 "${SLCAN_ATTACH_SRC}" /usr/local/bin/rpi-slcan-attach.sh
  install -m 755 "${SLCAN_WATCHDOG_SRC}" /usr/local/bin/rpi-slcan-watchdog.sh
  install -m 755 "${BRIDGE_WATCHDOG_SRC}" /usr/local/bin/rpi-can-udp-bridge-watchdog.sh
  install -m 755 "${PICK_IFACE_SRC}" /usr/local/bin/rpi-can-pick-iface.sh
}

write_can_spi_service() {
  local svc="/etc/systemd/system/rpi-can-spi.service"
  cat > "${svc}" <<'EOF'
[Unit]
Description=Bring up SPI/native SocketCAN interface (if present)
After=network-pre.target
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=/etc/default/rpi-can-hardware
ExecStart=/bin/sh -c 'if [ -d "/sys/class/net/${SPI_CAN_IFACE}" ]; then ip link set "${SPI_CAN_IFACE}" down 2>/dev/null || true; exec ip link set "${SPI_CAN_IFACE}" up type can bitrate "${CAN_BITRATE}"; else echo "[rpi-can-spi] ${SPI_CAN_IFACE} not present, skipping"; exit 0; fi'
ExecStop=/bin/sh -c 'ip link set "${SPI_CAN_IFACE}" down 2>/dev/null || true'

[Install]
WantedBy=multi-user.target
EOF
}

write_slcan_service() {
  local svc="/etc/systemd/system/rpi-slcan.service"
  cat > "${svc}" <<'EOF'
[Unit]
Description=Attach USB SLCAN adapters (setup_slcan)
DefaultDependencies=no
After=local-fs.target
Before=rpi-can-udp-bridge.service

[Service]
Type=oneshot
RemainAfterExit=no
# slcand daemonizes; without this, systemd tears down the cgroup when this unit exits and kills slcand.
KillMode=none
ExecStart=/usr/local/bin/rpi-slcan-attach.sh
# No ExecStop: with Type=oneshot RemainAfterExit=no, systemd runs ExecStop when the unit
# deactivates after ExecStart, which would immediately tear down slcand. To detach USB CAN:
#   sudo /usr/local/bin/setup_slcan --remove-all

[Install]
WantedBy=multi-user.target
EOF
}

write_slcan_watchdog_service() {
  local svc="/etc/systemd/system/rpi-slcan-watchdog.service"
  cat > "${svc}" <<'EOF'
[Unit]
Description=Watchdog for USB SLCAN auto-recovery
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rpi-slcan-watchdog.sh
EOF
}

write_slcan_watchdog_timer() {
  local tmr="/etc/systemd/system/rpi-slcan-watchdog.timer"
  cat > "${tmr}" <<EOF
[Unit]
Description=Periodic watchdog for USB SLCAN auto-recovery

[Timer]
OnBootSec=15s
OnUnitActiveSec=${SLCAN_WATCHDOG_INTERVAL}
AccuracySec=2s
Unit=rpi-slcan-watchdog.service

[Install]
WantedBy=timers.target
EOF
}

write_bridge_watchdog_service() {
  local svc="/etc/systemd/system/rpi-can-udp-bridge-watchdog.service"
  cat > "${svc}" <<'EOF'
[Unit]
Description=Watchdog for rpi-can-udp-bridge (restart only after SLCAN recovery)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rpi-can-udp-bridge-watchdog.sh
EOF
}

write_bridge_watchdog_timer() {
  local tmr="/etc/systemd/system/rpi-can-udp-bridge-watchdog.timer"
  cat > "${tmr}" <<EOF
[Unit]
Description=Periodic check for SLCAN loss; conditional bridge restart

[Timer]
OnBootSec=20s
OnUnitActiveSec=${SLCAN_WATCHDOG_INTERVAL}
AccuracySec=2s
Unit=rpi-can-udp-bridge-watchdog.service

[Install]
WantedBy=timers.target
EOF
}

write_slcan_udev_rules() {
  cat > "${UDEV_RULE_FILE}" <<'EOF'
# Re-run SLCAN attach when a USB serial device appears (ttyUSB / ttyACM).
ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyUSB[0-9]*|ttyACM[0-9]*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="rpi-slcan.service"
EOF
  chmod 644 "${UDEV_RULE_FILE}"
}

remove_slcan_udev_rules() {
  rm -f "${UDEV_RULE_FILE}"
}

migrate_old_can_iface_service() {
  if systemctl is-enabled rpi-can-iface.service &>/dev/null; then
    systemctl disable --now rpi-can-iface.service 2>/dev/null || true
  fi
  rm -f /etc/systemd/system/rpi-can-iface.service
}

write_bridge_env() {
  local env_file="/etc/default/rpi-can-udp-bridge"
  : "${MODE:=bridge}"
  : "${UDP_REMOTE_HOST:=127.0.0.1}"
  : "${UDP_REMOTE_PORT:=5000}"
  : "${UDP_LISTEN_PORT:=5000}"
  : "${STATS_INTERVAL:=2.0}"
  : "${UDP_BROADCAST:=0}"
  : "${UDP_PENDING_MAX:=65536}"
  : "${CAN_PENDING_MAX:=65536}"
  : "${UDP_AUTO_PEERS:=1}"
  : "${UDP_PEER_TTL_SEC:=30}"
  : "${UDP_MAX_PEERS:=64}"
  : "${BRIDGE_ORDER:=can_first}"
  : "${DRAIN_BURST:=4}"
  : "${BRIDGE_CAN_WEIGHT:=1}"
  : "${SELECT_TIMEOUT:=0.001}"
  : "${UDP_DROP_OUT_OF_ORDER:=0}"
  local udp_broadcast_arg=""
  local udp_drop_ooo_arg=""
  local udp_auto_peers_arg="--udp-auto-peers"
  if [[ "${UDP_BROADCAST}" == "1" ]]; then
    udp_broadcast_arg="--udp-broadcast"
  fi
  if [[ "${UDP_DROP_OUT_OF_ORDER}" == "1" ]]; then
    udp_drop_ooo_arg="--udp-drop-out-of-order"
  fi
  if [[ "${UDP_AUTO_PEERS}" == "0" ]]; then
    udp_auto_peers_arg="--no-udp-auto-peers"
  fi
  cat > "${env_file}" <<EOF
# py_can_udp_bridge.py — CAN_IFACE is written by rpi-can-pick-iface.sh to:
#   /run/rpi-can-udp-bridge-can.env
MODE=${MODE}
UDP_REMOTE_HOST=${UDP_REMOTE_HOST}
UDP_REMOTE_PORT=${UDP_REMOTE_PORT}
UDP_LISTEN_PORT=${UDP_LISTEN_PORT}
STATS_INTERVAL=${STATS_INTERVAL}
UDP_BROADCAST=${UDP_BROADCAST}
UDP_PENDING_MAX=${UDP_PENDING_MAX}
CAN_PENDING_MAX=${CAN_PENDING_MAX}
UDP_AUTO_PEERS=${UDP_AUTO_PEERS}
UDP_PEER_TTL_SEC=${UDP_PEER_TTL_SEC}
UDP_MAX_PEERS=${UDP_MAX_PEERS}
BRIDGE_ORDER=${BRIDGE_ORDER}
DRAIN_BURST=${DRAIN_BURST}
BRIDGE_CAN_WEIGHT=${BRIDGE_CAN_WEIGHT}
SELECT_TIMEOUT=${SELECT_TIMEOUT}
UDP_DROP_OUT_OF_ORDER=${UDP_DROP_OUT_OF_ORDER}
UDP_BROADCAST_ARG=${udp_broadcast_arg}
UDP_DROP_OUT_OF_ORDER_ARG=${udp_drop_ooo_arg}
UDP_AUTO_PEERS_ARG=${udp_auto_peers_arg}
EOF
  chmod 644 "${env_file}"
}

write_bridge_service() {
  local svc="/etc/systemd/system/rpi-can-udp-bridge.service"
  cat > "${svc}" <<EOF
[Unit]
Description=Python CAN <-> UDP bridge
After=network-online.target rpi-can-spi.service rpi-slcan.service wg-quick@${WG_CONFIG_NAME}.service
Wants=network-online.target wg-quick@${WG_CONFIG_NAME}.service

[Service]
Type=simple
# Line-buffer stdout/stderr to journald (otherwise Python may buffer for a long time).
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/default/rpi-can-udp-bridge
EnvironmentFile=-/run/rpi-can-udp-bridge-can.env
WorkingDirectory=${SCRIPT_DIR}
ExecStartPre=/usr/local/bin/rpi-can-pick-iface.sh
ExecStart=/usr/bin/python3 -u ${BRIDGE_SCRIPT} \
  --mode \${MODE} \
  --can-iface \${CAN_IFACE} \
  --udp-remote-host \${UDP_REMOTE_HOST} \
  --udp-remote-port \${UDP_REMOTE_PORT} \
  --udp-listen-port \${UDP_LISTEN_PORT} \
  --stats-interval \${STATS_INTERVAL} \
  \${UDP_BROADCAST_ARG} \
  \${UDP_AUTO_PEERS_ARG} \
  --udp-peer-ttl-sec \${UDP_PEER_TTL_SEC} \
  --udp-max-peers \${UDP_MAX_PEERS} \
  --bridge-order \${BRIDGE_ORDER} \
  --drain-burst \${DRAIN_BURST} \
  --bridge-can-weight \${BRIDGE_CAN_WEIGHT} \
  --select-timeout \${SELECT_TIMEOUT} \
  --udp-pending-max \${UDP_PENDING_MAX} \
  --can-pending-max \${CAN_PENDING_MAX} \
  \${UDP_DROP_OUT_OF_ORDER_ARG}
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
EOF
}

enable_disable_can_stack() {
  systemctl daemon-reload
  migrate_old_can_iface_service

  systemctl enable rpi-can-spi.service
  if [[ "${CAN_SOURCE}" == "spi" ]]; then
    systemctl disable rpi-slcan.service 2>/dev/null || true
    systemctl disable rpi-slcan-watchdog.timer 2>/dev/null || true
    systemctl enable rpi-can-udp-bridge-watchdog.timer
    systemctl stop rpi-slcan-watchdog.timer 2>/dev/null || true
    systemctl stop rpi-slcan.service 2>/dev/null || true
    remove_slcan_udev_rules
  else
    systemctl enable rpi-slcan.service
    systemctl enable rpi-slcan-watchdog.timer
    systemctl enable rpi-can-udp-bridge-watchdog.timer
    write_slcan_udev_rules
  fi

  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=tty 2>/dev/null || true
}

enable_services() {
  log "Enabling services"
  systemctl daemon-reload
  systemctl enable "wg-quick@${WG_CONFIG_NAME}.service"
  systemctl enable rpi-can-udp-bridge.service
}

start_services() {
  log "Starting services"
  systemctl restart "wg-quick@${WG_CONFIG_NAME}.service" || true
  systemctl restart rpi-can-spi.service || true
  systemctl restart rpi-slcan.service || true
  systemctl restart rpi-slcan-watchdog.timer || true
  systemctl restart rpi-can-udp-bridge-watchdog.timer || true
  systemctl restart rpi-can-udp-bridge.service || true
}

show_status() {
  log "Current service status (short):"
  systemctl --no-pager --full status "wg-quick@${WG_CONFIG_NAME}.service" || true
  systemctl --no-pager --full status rpi-can-spi.service || true
  systemctl --no-pager --full status rpi-slcan.service || true
  systemctl --no-pager --full status rpi-slcan-watchdog.timer || true
  systemctl --no-pager --full status rpi-can-udp-bridge-watchdog.timer || true
  systemctl --no-pager --full status rpi-can-udp-bridge.service || true
}

main() {
  parse_args "$@"
  require_root
  require_bridge_script
  require_bundled_scripts
  resolve_wg_config "${WG_CONFIG_INPUT}"
  install_packages

  write_can_hardware_defaults
  write_slcan_defaults

  if [[ "${CAN_SOURCE}" == "slcan" ]]; then
    log "CAN_SOURCE=slcan: skipping SPI boot overlay (MCP2515) changes"
  else
    configure_boot_for_spi_can
  fi

  install_helpers
  write_can_spi_service
  write_slcan_service
  write_slcan_watchdog_service
  write_slcan_watchdog_timer
  write_bridge_watchdog_service
  write_bridge_watchdog_timer
  write_bridge_env
  write_bridge_service
  enable_disable_can_stack
  enable_services
  start_services
  show_status

  log "Done."
  if [[ "${CAN_SOURCE}" != "slcan" ]]; then
    log "If SPI/CAN overlay was added to boot config, reboot once so can0 appears."
  fi
  log "Edit /etc/default/rpi-can-hardware and /etc/default/rpi-slcan, then: systemctl restart rpi-can-spi rpi-slcan rpi-can-udp-bridge"
}

main "$@"
