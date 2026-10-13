# gps-nmea-service

Serves the GPS from a Winegard ConnecT's cellular modem (Quectel EC25 family) as
an NMEA-0183 stream on TCP port 10110. Works with any NMEA client: OpenCPN,
SignalK, iNavX, gpsd, or `winegard2mqtt` with `WINEGARD_GPS_SOURCE=nmea`.

The stream is LAN-only: it listens on the router's LAN address, and a firewall
rule drops the port on the WAN (cellular) side.

## Requirements

- Root SSH access to a router you own. The installer prompts for the password
  and stores nothing.
- A ConnecT whose modem exposes a GNSS NMEA tty (`/dev/ttyUSB1` on the EC25),
  with a GNSS antenna fitted. The stock firmware already includes `socat`,
  `chatty`, `uci` and `stty`.

## Install

```sh
./install.sh root@10.11.12.1
```

One SSH session: copies the files, adds the firewall rule, enables the service
at boot and starts it.

## Uninstall

```sh
./uninstall.sh root@10.11.12.1
```

This removes the service, its files and the firewall rule. It leaves the modem
in streaming mode; to restore the firmware's default, run this on the router:

```sh
chatty 'AT+QGPSCFG="nmeasrc",1'
```

## Connect

| Client | Setting |
|---|---|
| OpenCPN / SignalK / iNavX | TCP, host `10.11.12.1`, port `10110` |
| gpsd | `gpsd tcp://10.11.12.1:10110` |
| Quick check | `nc 10.11.12.1 10110` |

## Stream

| | |
|---|---|
| Rate | One epoch per second |
| Sentences | `GGA`, `VTG`, `RMC`, `GSA`, `GSV` ×1–4, in that order |
| Talker | `GP` |
| Clients | Any number |

If the reader stops getting data from the modem, clients receive nothing rather
than a repeat of the last fix: an epoch is sent only once, and only while it is
under `MAXAGE` seconds old.

## How it works

`gpsnmea.sh` switches the modem to continuous NMEA output (`AT+QGPS=1`,
`AT+QGPSCFG="nmeasrc",0`). It tails the tty, writing each complete epoch to
`/tmp/gps.nmea`, and reopens the tty if the read ever ends. socat accepts
connections and runs `gps-emit.sh` once per client to send new epochs. procd
supervises the whole thing and restarts it if it exits.

| File | Installed to |
|---|---|
| `files/gpsnmea.sh` | `/usr/bin/gpsnmea.sh` |
| `files/gps-emit.sh` | `/usr/bin/gps-emit.sh` |
| `files/gpsnmea.init` | `/etc/init.d/gpsnmea` |

## Configuration

Environment variables, read when the service starts:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `10110` | TCP port (also used by `install.sh` / `uninstall.sh`) |
| `DEV` | `/dev/ttyUSB1` | Modem NMEA tty (also used by `uninstall.sh`) |
| `BIND` | LAN address (`network.lan.ipaddr`) | Listen address |
| `MAXAGE` | `5` | Seconds before a stale epoch is withheld |

`install.sh` saves `PORT` and `DEV` to `/etc/gpsnmea.conf`. To change `DEV`,
`BIND` or `MAXAGE`, edit that file and run `/etc/init.d/gpsnmea restart`. To
change `PORT`, uninstall and reinstall with the new value: the firewall rule
is only created when none exists, so editing the file or reinstalling over it
would leave the rule on the old port.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Connection refused | Service down. `/etc/init.d/gpsnmea status`; `netstat -ltn \| grep 10110` |
| Times advance, RMC status `V`, GGA quality `0` | No fix. Check the antenna and sky view; `chatty 'AT+QGPSLOC=2'` returns a position once fixed |
| Connects but no data | Reader stalled. `/etc/init.d/gpsnmea restart` |
| Still no data after a restart | Wrong tty. `ls /dev/ttyUSB*` and set `DEV` |
