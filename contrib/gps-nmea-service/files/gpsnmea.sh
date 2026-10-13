#!/bin/sh
# Serve the Quectel cellular-modem GPS as NMEA-0183 over TCP, multi-client.
#
# Reads the EC25 modem's live NMEA port (default /dev/ttyUSB1), keeps the most
# recent 1 Hz epoch in /tmp/gps.nmea, and fans it out over TCP via socat.
#
# LAN-only: binds to the router's LAN IP, so it never listens on the WAN
# (cellular) interface. Pair with the firewall rule the installer adds.
#
# Overridable via environment:
#   PORT  TCP listen port                 (default 10110, IANA "NMEA-0183")
#   DEV   modem NMEA tty                   (default /dev/ttyUSB1)
#   BIND  listen address                   (default = network.lan.ipaddr)

PORT="${PORT:-10110}"
DEV="${DEV:-/dev/ttyUSB1}"
BIND="${BIND:-$(uci -q get network.lan.ipaddr)}"; BIND="${BIND:-127.0.0.1}"
OUT=/tmp/gps.nmea

# Clear out strays from a prior run: a reader still owning the tty, or a socat
# still holding the port (procd stop can leave one orphaned). Without this a
# restart cannot re-bind the port and would keep serving a stale snapshot.
for pid in $(ps w | grep -E "cat $DEV|socat TCP-LISTEN:$PORT" | grep -v grep | awk '{print $1}'); do
	kill "$pid" 2>/dev/null
done

# Enable modem GNSS and continuous NMEA streaming on the dedicated port.
# nmeasrc=0 => the modem streams NMEA to $DEV (what we tail here).
chatty 'AT+QGPSCFG="nmeasrc",0' >/dev/null 2>&1
chatty 'AT+QGPS=1'              >/dev/null 2>&1

# Reader: maintain a fresh 1 Hz snapshot (one full GGA/VTG/RMC/GSA/GSV set) in
# $OUT. Wrapped in a loop so that if cat ever returns (EOF / transient tty
# hiccup) the port is reopened and the feed cannot freeze permanently.
reader() {
	while :; do
		stty -F "$DEV" raw -echo 2>/dev/null
		snap=""
		cat "$DEV" 2>/dev/null | while IFS= read -r line; do
			case "$line" in \$G*) ;; *) continue ;; esac
			case "$line" in
				*GGA,*)
					[ -n "$snap" ] && { printf '%s' "$snap" > "$OUT.tmp" && mv -f "$OUT.tmp" "$OUT"; }
					snap="$line
" ;;
				*)
					snap="$snap$line
" ;;
			esac
		done
		sleep 1
	done
}
reader & RPID=$!

# Run socat in the FOREGROUND (not exec) so this shell remains the supervised
# process under procd. Start both, then trap: on TERM/INT (procd stop) tear
# down BOTH the reader and socat so neither is left orphaned holding the tty or
# the port — otherwise the next start can't re-bind.
socat TCP-LISTEN:"$PORT",reuseaddr,fork,bind="$BIND" EXEC:/usr/bin/gps-emit.sh &
SPID=$!
trap 'kill $RPID $SPID 2>/dev/null; exit 0' INT TERM
wait $SPID
kill $RPID 2>/dev/null
