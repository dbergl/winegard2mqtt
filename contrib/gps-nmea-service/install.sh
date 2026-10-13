#!/usr/bin/env bash
#
# install.sh — deploy the GPS NMEA-0183 TCP service to a Winegard ConnecT
# (OpenWrt/LEDE) router over SSH, enable it at boot, and lock it to the LAN.
#
# Requirements:
#   * ROOT SSH access to the router (user must supply their own credentials).
#     The router's dropbear SSH must be reachable and root login enabled.
#   * The router uses a Quectel EC25 (or compatible) cellular modem that
#     exposes a GNSS NMEA tty (default /dev/ttyUSB1).
#
# Usage:
#   ./install.sh [SSH_TARGET]
#   ./install.sh root@10.11.12.1        # explicit target
#   SSH_TARGET=root@10.0.0.1 ./install.sh # via env
#
# SSH_TARGET defaults to root@10.11.12.1; set it to YOUR router's LAN IP.
# You will be prompted for the root password by ssh (nothing is stored here).
set -euo pipefail

SSH_TARGET="${1:-${SSH_TARGET:-root@10.11.12.1}}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-10110}"
DEV="${DEV:-/dev/ttyUSB1}"

# ssh options tolerant of the router's old host key / first-connect prompt.
SSH_OPTS=${SSH_OPTS:--o StrictHostKeyChecking=accept-new -o ConnectTimeout=10}

echo "[*] Target router : $SSH_TARGET"
echo "[*] TCP port       : $PORT (LAN-only)"
echo "[*] Modem NMEA tty : $DEV"
echo "[*] You will be asked for the router's root SSH password."
echo

# Embed a payload file into the remote bootstrap stream as a heredoc install.
emit_file() {
	local dest="$1" src="$2" mode="$3" delim="$4"
	printf 'cat > %s <<'\''%s'\''\n' "$dest" "$delim"
	cat "$HERE/$src"
	printf '%s\n' "$delim"
	printf 'chmod %s %s\n' "$mode" "$dest"
}

# Build one remote script and run it in a single SSH session (one password).
{
	echo 'set -e'
	emit_file /usr/bin/gpsnmea.sh  files/gpsnmea.sh  755 __GPSNMEA_SH_EOF__
	emit_file /usr/bin/gps-emit.sh files/gps-emit.sh 755 __GPSEMIT_SH_EOF__
	emit_file /etc/init.d/gpsnmea  files/gpsnmea.init 755 __GPSNMEA_INIT_EOF__

	cat <<REMOTE
# --- Service config: PORT/DEV the init script sources at start (same PORT as the
#     firewall rule below). Edit /etc/gpsnmea.conf + restart to change later. ---
cat > /etc/gpsnmea.conf <<'CONF'
PORT=$PORT
DEV=$DEV
CONF
chmod 644 /etc/gpsnmea.conf

# --- Firewall: drop the GPS port on WAN (defense in depth; socket is LAN-bound) ---
if ! uci -q show firewall | grep -q "name='Block-GPS-NMEA-WAN'"; then
	uci add firewall rule >/dev/null
	uci set firewall.@rule[-1].name='Block-GPS-NMEA-WAN'
	uci set firewall.@rule[-1].src='wan'
	uci set firewall.@rule[-1].proto='tcp'
	uci set firewall.@rule[-1].dest_port='$PORT'
	uci set firewall.@rule[-1].target='DROP'
	uci commit firewall
	/etc/init.d/firewall reload >/dev/null 2>&1 || true
	echo '[device] firewall rule added'
else
	echo '[device] firewall rule already present'
fi

# --- Enable at boot and (re)start now ---
/etc/init.d/gpsnmea enable
/etc/init.d/gpsnmea stop  >/dev/null 2>&1 || true
/etc/init.d/gpsnmea start
sleep 3

echo '[device] boot symlinks:'; ls /etc/rc.d/ 2>/dev/null | grep gpsnmea || echo '  (none!)'
echo '[device] listening socket:'; netstat -ltn 2>/dev/null | grep ":$PORT " || echo '  (not listening yet)'
echo "[device] LAN IP: \$(uci -q get network.lan.ipaddr)"
REMOTE
} | ssh $SSH_OPTS "$SSH_TARGET" sh

echo
echo "[*] Done. Point your NMEA client at the router's LAN IP, port $PORT:"
echo "      OpenCPN / SignalK / iNavX : TCP, host <router-LAN-IP>, port $PORT"
echo "      gpsd                      : gpsd tcp://<router-LAN-IP>:$PORT"
echo "      quick test                : nc <router-LAN-IP> $PORT"
