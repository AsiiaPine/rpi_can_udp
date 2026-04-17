# rpi_can_udp

A set of scripts to run a CAN<->UDP bridge on Raspberry Pi with support for:

- WireGuard (`wg-quick@<config>.service`)
- SPI/MCP2515 (`can0`) and/or USB SLCAN (`slcan0`)
- systemd/udev auto-start and SLCAN watchdog recovery

## Scripts in project root

### `install_rpi_can_vpn_bridge.sh`

Main installer/provisioning script. It:

- installs required packages (`wireguard`, `can-utils`, `resolvconf`, `iproute2`, `python3`, etc.)
- copies WireGuard `*.conf` files into `/etc/wireguard` (or uses an existing one)
- writes runtime config files into `/etc/default/*`
- installs helper scripts into `/usr/local/bin`
- creates/enables systemd units:
  - `rpi-can-spi.service`
  - `rpi-slcan.service`
  - `rpi-slcan-watchdog.service` + `rpi-slcan-watchdog.timer`
  - `rpi-can-udp-bridge.service`
  - `wg-quick@<name>.service`
- installs a udev rule for USB-CAN hotplug (`ttyUSB*`, `ttyACM*`)

Examples:

```bash
sudo ./install_rpi_can_vpn_bridge.sh
sudo ./install_rpi_can_vpn_bridge.sh --config ./install_rpi_can_vpn_bridge.conf.example
sudo ./install_rpi_can_vpn_bridge.sh --wg-config wg0 --can-source auto
```

Main flags:

- `--config|-c <file>`: `KEY=value` config source; CLI flags override config values
- `--wg-config <name|path>`
- `--can-source <spi|slcan|auto>`
- `--can-iface <name>`: SPI interface (usually `can0`)
- `--slcan-iface <name>`: preferred SLCAN interface (usually `slcan0`)
- `--can-bitrate <rate>`
- `--slcan-watchdog-interval <time>`: for example `5s`, `30s`, `1min`
- `--skip-upgrade`

### `install_rpi_can_vpn_bridge.conf.example`

Example installer configuration file.

- Contains all key variables (`CAN_SOURCE`, `SLCAN_*`, `UDP_*`, `MODE`, etc.)
- Can be used as a template:

```bash
cp install_rpi_can_vpn_bridge.conf.example my-install.conf
sudo ./install_rpi_can_vpn_bridge.sh --config ./my-install.conf
```
> [!NOTE] 
> Don't forget to change UDP_REMOTE_HOST and ports for your connection.

> [!NOTE] 
> For broadcast the ip address should be x.x.x.255!

### `py_can_udp_bridge.py`

Runtime bridge between SocketCAN and UDP.

Modes:

- `bridge`: bidirectional (CAN->UDP and UDP->CAN)
- `can2udp`: CAN->UDP only
- `udp2can`: UDP->CAN only

Parameters:

- `--can-iface` (e.g. `can0`, `slcan0`)
- `--udp-remote-host`, `--udp-remote-port`
- `--udp-listen-port`
- `--udp-broadcast`
- `--stats-interval`
- `--allow-self-loop` (dangerous; disables local loop protection)

Important:

- UDP payload must match the expected bridge format (`UDP_FRAME_STRUCT`), otherwise frames are dropped (`dropped_bad_crc` grows).
- For broadcast mode, use your subnet broadcast in `--udp-remote-host` (for example `192.168.1.255`), not `127.0.0.1`.

## Scripts in `scripts/`

### `scripts/setup_slcan`

Vendored helper (Pavel Kirienko gist) for registering serial-CAN adapters via `slcand`.

Typical capabilities:

- `--remove-all`
- set interface basename (`slcan*` / `can*`)
- set CAN bit rate (`-s0..8`)
- set UART baud (`-S`, e.g. `921600`)

### `scripts/rpi-slcan-attach.sh`

Wrapper around `setup_slcan`. It:

- reads `/etc/default/rpi-slcan`
- discovers devices (`SLCAN_DEVICES` or auto `ttyUSB*/ttyACM*`)
- prevents races using `/run/rpi-slcan-attach.lock`
- executes `--remove-all` then attaches fresh `slcan*` interfaces

Note:

- `/run` is cleared on reboot.

### `scripts/rpi-slcan-watchdog.sh`

Checks that a matching `slcan*` interface exists when USB-CAN is connected.

- if USB is present but no `slcan*` exists, it restarts `rpi-slcan.service`
- run period is controlled by `rpi-slcan-watchdog.timer`

### `scripts/rpi-can-pick-iface.sh`

Selects CAN interface for the bridge before `rpi-can-udp-bridge.service` starts.

Logic:

- `CAN_SOURCE=spi` -> `SPI_CAN_IFACE`
- `CAN_SOURCE=slcan` -> `SLCAN_IFACE`
- `CAN_SOURCE=auto` -> active `slcan*` first, otherwise SPI

Writes the result to `/run/rpi-can-udp-bridge-can.env` as `CAN_IFACE=<...>`.

## System config files

After installation, these files are used:

- `/etc/default/rpi-can-hardware`
- `/etc/default/rpi-slcan`
- `/etc/default/rpi-can-udp-bridge`
- `/etc/wireguard/<name>.conf`
- `/etc/udev/rules.d/99-rpi-slcan.rules`

## Quick diagnostics

Check services:

```bash
systemctl status rpi-can-spi.service
systemctl status rpi-slcan.service
systemctl status rpi-slcan-watchdog.timer
systemctl status rpi-can-udp-bridge.service
systemctl status wg-quick@wg0.service
```

Check CAN interfaces:

```bash
ip -br link show type can
ip -details link show slcan0
```

Bridge/watchdog logs:

```bash
journalctl -u rpi-can-udp-bridge.service -f
journalctl -u rpi-slcan-watchdog.service -n 50 --no-pager
```
