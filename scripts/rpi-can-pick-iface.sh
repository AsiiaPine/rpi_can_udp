#!/bin/bash
# Choose CAN_IFACE for the UDP bridge: SPI can vs USB SLCAN (see /etc/default/rpi-can-hardware).
set -euo pipefail

HW=/etc/default/rpi-can-hardware
OUT=/run/rpi-can-udp-bridge-can.env

mkdir -p "$(dirname "${OUT}")"

CAN_SOURCE=auto
SPI_CAN_IFACE=can0
SLCAN_IFACE=slcan0

if [[ -f "${HW}" ]]; then
  # shellcheck source=/dev/null
  set -a
  source "${HW}"
  set +a
fi

CAN_SOURCE="${CAN_SOURCE:-auto}"
SPI_CAN_IFACE="${SPI_CAN_IFACE:-can0}"
SLCAN_IFACE="${SLCAN_IFACE:-slcan0}"

iface_is_up() {
  local n="$1"
  ip link show "${n}" 2>/dev/null | grep -q 'state UP'
}

iface_exists() {
  ip link show "$1" &>/dev/null
}

pick_auto() {
  if iface_is_up "${SLCAN_IFACE}"; then
    echo "${SLCAN_IFACE}"
    return
  fi
  local i
  for i in $(seq 0 7); do
    local name="slcan${i}"
    if iface_is_up "${name}"; then
      echo "${name}"
      return
    fi
  done
  if iface_exists "${SPI_CAN_IFACE}"; then
    echo "${SPI_CAN_IFACE}"
    return
  fi
  echo "${SPI_CAN_IFACE}"
}

case "${CAN_SOURCE}" in
  spi)
    chosen="${SPI_CAN_IFACE}"
    ;;
  slcan)
    chosen="${SLCAN_IFACE}"
    ;;
  auto)
    chosen="$(pick_auto)"
    ;;
  *)
    echo "[rpi-can-pick-iface] Invalid CAN_SOURCE=${CAN_SOURCE}; use spi|slcan|auto" >&2
    exit 1
    ;;
esac

umask 022
printf 'CAN_IFACE=%s\n' "${chosen}" > "${OUT}"
echo "[rpi-can-pick-iface] CAN_SOURCE=${CAN_SOURCE} -> CAN_IFACE=${chosen}"
