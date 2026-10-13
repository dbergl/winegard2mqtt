#!/bin/sh
# Per-connection emitter: stream the latest GPS snapshot to one TCP client.
# socat runs one copy of this per accepted connection.
#
# Freshness guard: only emit when the snapshot is current. The reader in
# gpsnmea.sh rewrites /tmp/gps.nmea (via mv) once per second, bumping its
# mtime. We send a snapshot only when its mtime has ADVANCED (new epoch) and
# is no older than MAXAGE seconds. If the reader stalls, the mtime stops
# moving: the client goes silent instead of replaying a stale fix forever.
# (winegard2mqtt / gpsd / OpenCPN treat silence as "fix lost", which is
# correct; replaying an old epoch would look falsely current.)
OUT=/tmp/gps.nmea
MAXAGE="${MAXAGE:-5}"

last_mt=""
while :; do
	mt="$(date -r "$OUT" +%s 2>/dev/null)"
	now="$(date +%s)"
	if [ -n "$mt" ] && [ "$mt" != "$last_mt" ] && [ $((now - mt)) -le "$MAXAGE" ]; then
		cat "$OUT" 2>/dev/null
		last_mt="$mt"
	fi
	sleep 1
done
