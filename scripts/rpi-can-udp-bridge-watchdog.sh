#!/bin/bash
# Restart rpi-can-udp-bridge only when SLCAN netdev was missing and came back.
set -euo pipefail

HW=/etc/default/rpi-can-hardware
SLCAN=/etc/default/rpi-slcan

CAN_SOURCE=auto
SLCAN_IFACE=slcan0
ENABLE_SLCAN=1
BRIDGE_WATCHDOG_WAIT_MAX_SEC=120

if [[ -f "${HW}" ]]; then
  # shellcheck source=/dev/null
  source "${HW}"
fi

if [[ -f "${SLCAN}" ]]; then
  # shellcheck source=/dev/null
  source "${SLCAN}"
fi

CAN_SOURCE="${CAN_SOURCE:-auto}"
SLCAN_IFACE="${SLCAN_IFACE:-slcan0}"
ENABLE_SLCAN="${ENABLE_SLCAN:-1}"
BRIDGE_WATCHDOG_WAIT_MAX_SEC="${BRIDGE_WATCHDOG_WAIT_MAX_SEC:-120}"

needs_slcan=0
if [[ "${CAN_SOURCE}" == "slcan" ]]; then
  needs_slcan=1
elif [[ "${CAN_SOURCE}" == "auto" && "${ENABLE_SLCAN}" == "1" ]]; then
  needs_slcan=1
fi

if [[ "${needs_slcan}" -eq 0 ]]; then
  exit 0
fi

if ip link show "${SLCAN_IFACE}" &>/dev/null; then
  exit 0
fi

echo "[rpi-can-udp-bridge-watchdog] ${SLCAN_IFACE} missing; waiting up to ${BRIDGE_WATCHDOG_WAIT_MAX_SEC}s"
start_ts="$(date +%s)"
while true; do
  if ip link show "${SLCAN_IFACE}" &>/dev/null; then
    echo "[rpi-can-udp-bridge-watchdog] ${SLCAN_IFACE} is back; restarting bridge"
    exec systemctl try-restart rpi-can-udp-bridge.service
  fi
  now_ts="$(date +%s)"
  elapsed="$((now_ts - start_ts))"
  if [[ "${elapsed}" -ge "${BRIDGE_WATCHDOG_WAIT_MAX_SEC}" ]]; then
    echo "[rpi-can-udp-bridge-watchdog] ${SLCAN_IFACE} did not appear in time; skip restart"
    exit 0
  fi
  sleep 1
done
