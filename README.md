# winegard2mqtt

Publishes GPS position and cellular modem telemetry from a Winegard ConnecT
router to MQTT, with Home Assistant discovery.

Tested on the ConnecT 2.0 4G and the ConnecT 5G. The 5G ships with a GPS
antenna; the 2.0 4G does not, and needs a passive GNSS antenna fitted to the
modem's empty `ANT_GNSS` u.fl port (I used the
[Adafruit passive GPS antenna](https://www.adafruit.com/product/2461)).

Background and design decisions: [`docs/design.md`](docs/design.md).

## Configuration

All settings are environment variables. Put `WINEGARD_PASSWORD` in `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `WINEGARD_HOST` | `10.11.12.1` | Router address |
| `WINEGARD_USERNAME` | `admin` | Fixed by the router UI |
| `WINEGARD_PASSWORD` | — | **Required** |
| `WINEGARD_POLL_INTERVAL` | `60` | Seconds between `sys_status` polls |
| `WINEGARD_GPS_SOURCE` | `json` | Where position comes from: `json` or `nmea` (see below) |
| `WINEGARD_NMEA_PORT` | `10110` | NMEA TCP port on the router (`nmea` only) |
| `WINEGARD_NMEA_INTERVAL` | `5` | Minimum seconds between position updates (`nmea` only) |
| `MQTT_HOST` / `MQTT_PORT` | `mosquitto` / `1883` | Broker |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | Broker credentials |
| `MQTT_TOPIC_BASE` | `winegard` | Root topic |
| `MQTT_CLIENT_ID` | `connect` | Client ID and topic segment |

Modem telemetry always comes from the router's `sys_status` JSON. Position
comes from one of two sources.

## Position sources

### `json` (default)

Reads the `gps` object in `sys_status`, the same data the router's GPS Services
page shows, plus fix quality the page omits. Works on stock firmware.

The router only refreshes this every `gpsrate` seconds (300 by default, minimum
30, set on its GPS Services page), so polling faster than that re-reads the
same fix.

| Key | Example | Notes |
|---|---|---|
| `latitude` / `longitude` | `"12.34567"` / `"-65.43210"` | Signed decimal degrees |
| `altitude` | `"83.6"` | Metres |
| `speed` | `"0.0"` | km/h |
| `heading` | `"0.00"` | Degrees |
| `date` + `time` | `"2026/08/15"` + `"22:15:30.0"` | Combined into ISO 8601 UTC |
| `fix` | `"3D"` | `2D` / `3D`; absent without a fix |
| `satellites` | `"06"` | Used in the fix, not in view |
| `hdop` | `"0.8"` | |

Checked against firmware `LEDE-BOTH-20240315`.

### `nmea` (live, 1 Hz)

Reads a NMEA-0183 stream from TCP port 10110 on the router. Stock firmware has
no such stream: install the companion service in
[`contrib/gps-nmea-service/`](contrib/gps-nmea-service/) first (needs root SSH
to the router). Check that it's running with `nc 10.11.12.1 10110`.

| Field | Sentence |
|---|---|
| `latitude` / `longitude` | RMC |
| `altitude`, `satellites`, `hdop` | GGA |
| `speed` | VTG km/h, falling back to RMC knots |
| `heading` | RMC course, falling back to VTG |
| `utc` | RMC date + time |
| `fix_type` | GSA (`2D` / `3D`) |

- Lines with a bad checksum are dropped.
- Position updates go out at most every `WINEGARD_NMEA_INTERVAL` seconds.
  Gaining or losing the fix is published immediately.
- If the connection is refused or closed, or sends nothing for 30 s, the
  position is marked unavailable. It reconnects every 5 s.
- `utc` is the time in the sentences, not when they arrived, so a stale fix
  shows as an old *GPS Time* rather than looking current.

### Validity

Both sources publish a position only with a real fix: `fix` of `2D`/`3D` in
the JSON, or RMC status `A` and a non-zero GGA quality in NMEA. Otherwise
`gps/available` goes `offline`, and the last position is left in place rather
than republished as current.

`gps_accuracy` is `hdop × 5` m, a rough estimate that gives Home Assistant an
accuracy circle.

## Privacy

With *GPS Location Service* enabled on the router's GPS Services page
(`tracking: true` in `sys_status`), the router sends its position to Winegard's
MQTT server. This project doesn't need that. Both position sources keep
working with it off, so you can turn it off; the button label shows the action,
not the current state, so confirm `sys_status` reads `tracking: false`.

## Topics

| Topic | Payload |
|---|---|
| `winegard/connect/state` | `online` / `offline`: bridge running (LWT) |
| `winegard/connect/gps/available` | `online` / `offline`: valid fix |
| `winegard/connect/gps` | JSON: `latitude`, `longitude`, `altitude`, `speed`, `heading`, `utc`, `fix_type`, `satellites`, `hdop`, `gps_accuracy` |
| `winegard/connect/modem` | JSON: `signal_percent`, `rssi_dbm`, `band`, `mode`, `rx_bytes`, `tx_bytes`, `carrier`, `apn`, `state`, `internet_source`, `internet_status` |

All are retained. GPS sensors follow `gps/available` and modem sensors follow
`state`, so losing the fix doesn't take the modem sensors down.

`rx_bytes` / `tx_bytes` reset whenever the cellular connection drops. They are
published as `total_increasing`, so Home Assistant treats a drop as a reset.
For billing-cycle totals, use a `utility_meter` helper.

## Development

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements.dev.txt
./venv/bin/python -m pytest test/ -v
```

Tests run without mocks: a local HTTP server mimics the router's login, a local
TCP server plays the NMEA stream, and fixtures are captured router output with
positions substituted.
