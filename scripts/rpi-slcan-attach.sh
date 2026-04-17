#!/bin/bash
# Attach USB serial devices as SLCAN interfaces using setup_slcan (Pavel Kirienko).
set -euo pipefail

DEFAULT_SLCAN=/etc/default/rpi-slcan
SETUP_SLCAN=/usr/local/bin/setup_slcan
LOCK_FILE=/run/rpi-slcan-attach.lock

if [[ -f "${DEFAULT_SLCAN}" ]]; then
  # shellcheck source=/dev/null
  set -a
  source "${DEFAULT_SLCAN}"
  set +a
fi

ENABLE_SLCAN="${ENABLE_SLCAN:-1}"
if [[ "${ENABLE_SLCAN}" != "1" ]]; then
  exit 0
fi

SPEED_CODE="${SLCAN_SPEED_CODE:-8}"
BAUDRATE="${SLCAN_SERIAL_BAUD:-921600}"
BASENAME="${SLCAN_BASENAME:-slcan}"
SILENT="${SLCAN_SILENT:-0}"

collect_ttys() {
  if [[ -n "${SLCAN_DEVICES:-}" ]]; then
    read -r -a _arr <<< "${SLCAN_DEVICES}"
    printf '%s\n' "${_arr[@]}"
    return
  fi
  shopt -s nullglob
  local -a ttys=()
  ttys=(/dev/ttyUSB* /dev/ttyACM*)
  IFS=$'\n'
  printf '%s\n' "${ttys[@]}" | sort -u
}

mapfile -t ttys < <(collect_ttys)
if [[ ${#ttys[@]} -eq 0 ]]; then
  echo "[rpi-slcan-attach] No USB serial devices (ttyUSB*/ttyACM*); nothing to attach."
  exit 0
fi

# /run is tmpfs and is cleared on reboot, but we also prune stale lock files.
if [[ -e "${LOCK_FILE}" ]]; then
  exec 8>"${LOCK_FILE}"
  if flock -n 8; then
    rm -f "${LOCK_FILE}"
  fi
  exec 8>&-
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[rpi-slcan-attach] Another attach run is in progress; skipping."
  exit 0
fi
sleep 0.35

if [[ ! -x "${SETUP_SLCAN}" ]]; then
  echo "[rpi-slcan-attach] Missing ${SETUP_SLCAN}" >&2
  exit 1
fi

args=(--remove-all "-b${BASENAME}" "-S${BAUDRATE}" "-s${SPEED_CODE}")
if [[ "${SILENT}" == "1" ]]; then
  args+=(--silent)
fi

valid=0
for tty in "${ttys[@]}"; do
  [[ -n "${tty}" ]] || continue
  [[ -e "${tty}" ]] || continue
  args+=("${tty}")
  valid=1
done

if [[ "${valid}" -eq 0 ]]; then
  echo "[rpi-slcan-attach] No existing device nodes to attach."
  exit 0
fi

"${SETUP_SLCAN}" "${args[@]}"
rm /run/rpi-slcan-attach.lock
