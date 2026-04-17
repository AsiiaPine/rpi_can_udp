#!/bin/bash
# Periodic watchdog for USB SLCAN. Restarts rpi-slcan.service if USB CAN exists
# but no matching SLCAN interface is present.
set -euo pipefail

DEFAULT_SLCAN=/etc/default/rpi-slcan

if [[ -f "${DEFAULT_SLCAN}" ]]; then
  # shellcheck source=/dev/null
  source "${DEFAULT_SLCAN}"
fi

ENABLE_SLCAN="${ENABLE_SLCAN:-1}"
if [[ "${ENABLE_SLCAN}" != "1" ]]; then
  exit 0
fi

BASENAME="${SLCAN_BASENAME:-slcan}"

collect_ttys() {
  if [[ -n "${SLCAN_DEVICES:-}" ]]; then
    read -r -a _arr <<< "${SLCAN_DEVICES}"
    printf '%s\n' "${_arr[@]}"
    return
  fi
  shopt -s nullglob
  local -a ttys=()
  ttys=(/dev/ttyUSB* /dev/ttyACM*)
  printf '%s\n' "${ttys[@]}"
}

mapfile -t ttys < <(collect_ttys)
usb_present=0
for tty in "${ttys[@]}"; do
  [[ -n "${tty}" ]] || continue
  [[ -e "${tty}" ]] || continue
  usb_present=1
  break
done

if [[ "${usb_present}" -ne 1 ]]; then
  exit 0
fi

slcan_ok=0
for idx in $(seq 0 31); do
  iface="${BASENAME}${idx}"
  if ip link show "${iface}" &>/dev/null; then
    slcan_ok=1
    break
  fi
done

if [[ "${slcan_ok}" -eq 1 ]]; then
  exit 0
fi

echo "[rpi-slcan-watchdog] USB CAN detected but no ${BASENAME}* interface found. Restarting rpi-slcan.service"
exec systemctl restart rpi-slcan.service
