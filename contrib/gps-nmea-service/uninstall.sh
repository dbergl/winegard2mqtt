#!/usr/bin/env bash
#
# uninstall.sh — remove the GPS NMEA service from a Winegard ConnecT router.
# Stops and disables the service, deletes its files, and removes the firewall
# rule. Requires ROOT SSH access (you supply your own credentials).
#
# Usage:
#   ./uninstall.sh [SSH_TARGET]          # default root@10.11.12.1
#
# Note: this does NOT revert the modem's nmeasrc setting. If you want the stock
# firmware GPS-pull behavior back, run on the router:
#   chatty 'AT+QGPSCFG="nmeasrc",1'
set -euo pipefail

SSH_TARGET="${1:-${SSH_TARGET:-root@10.11.12.1}}"
PORT="${PORT:-10110}"
DEV="${DEV:-/dev/ttyUSB1}"   # match a custom modem tty if install used one
SSH_OPTS=${SSH_OPTS:--o StrictHostKeyChecking=accept-new -o ConnectTimeout=10}

echo "[*] Target router : $SSH_TARGET"
echo "[*] You will be asked for the router's root SSH password."

ssh $SSH_OPTS "$SSH_TARGET" sh <<REMOTE
/etc/init.d/gpsnmea stop    >/dev/null 2>&1 || true
/etc/init.d/gpsnmea disable >/dev/null 2>&1 || true
rm -f /etc/init.d/gpsnmea /usr/bin/gpsnmea.sh /usr/bin/gps-emit.sh /etc/gpsnmea.conf /tmp/gps.nmea

# Remove the firewall rule by name (index-independent).
removed=0
while uci -q show firewall | grep -q "name='Block-GPS-NMEA-WAN'"; do
	idx=\$(uci show firewall | sed -n "s/^firewall\.@rule\[\([0-9]\+\)\].name='Block-GPS-NMEA-WAN'/\1/p" | head -1)
	[ -n "\$idx" ] || break
	uci delete firewall.@rule[\$idx]
	removed=1
done
[ "\$removed" = 1 ] && { uci commit firewall; /etc/init.d/firewall reload >/dev/null 2>&1 || true; echo '[device] firewall rule removed'; }

# Kill any still-running pieces.
for pid in \$(ps w | grep -E 'gpsnmea.sh|gps-emit.sh|socat TCP-LISTEN:$PORT|cat $DEV' | grep -v grep | awk '{print \$1}'); do
	kill \$pid 2>/dev/null || true
done
echo '[device] uninstalled'
REMOTE

echo "[*] Done."
