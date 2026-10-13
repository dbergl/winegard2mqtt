"""Pure parsing of the router's NMEA-0183 stream. No I/O.

An alternative GPS source to ``sys_status``: the service in
``contrib/gps-nmea-service`` broadcasts GGA, VTG, RMC, GSA and GSV on TCP 10110,
one epoch a second. Each sentence carries only part of a fix, so
``NmeaAccumulator`` keeps the latest of each and assembles them into the same
``GpsFix`` the JSON path produces.

The fix's ``utc`` comes from the sentences, never from when they arrived: an
early build of the service replayed one epoch for minutes, and a stale epoch
must look stale.
"""

from datetime import datetime, timezone
from functools import reduce
from typing import Optional

from .parser import GpsFix, _number

_KNOTS_TO_KMH = 1.852

# GSA's navigation mode field: 1 is no fix.
_GSA_MODES = {"2": "2D", "3": "3D"}


def checksum(body: str) -> str:
    """XOR of every character between ``$`` and ``*``, as two hex digits."""
    return f"{reduce(lambda acc, ch: acc ^ ord(ch), body, 0):02X}"


def split_sentence(line: str) -> Optional[list]:
    """Validate one sentence and return its fields, or None if it is corrupt.

    The checksum is mandatory: a TCP stream can still be cut mid-sentence by a
    reconnect, and a truncated GGA would otherwise parse into a wrong position.
    """
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, _, given = line[1:].rpartition("*")
    if checksum(body) != given.strip().upper():
        return None
    return body.split(",")


def _coordinate(value: str, hemisphere: str, degree_digits: int) -> Optional[float]:
    """Convert NMEA ``(d)ddmm.mmmm`` plus a hemisphere letter to signed degrees."""
    if not value or hemisphere not in ("N", "S", "E", "W"):
        return None
    try:
        degrees = int(value[:degree_digits])
        minutes = float(value[degree_digits:])
    except ValueError:
        return None
    if not 0.0 <= minutes < 60.0:
        return None
    decimal = degrees + minutes / 60.0
    return -decimal if hemisphere in ("S", "W") else decimal


def _utc_timestamp(date: str, time: str) -> Optional[str]:
    """RMC's ``ddmmyy`` + ``hhmmss.ss`` as ISO 8601 with an explicit offset."""
    try:
        parsed = datetime.strptime(f"{date} {time.split('.')[0]}", "%d%m%y %H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _field(fields: list, index: int) -> str:
    return fields[index] if index < len(fields) else ""


class NmeaAccumulator:
    """Holds the latest GGA, RMC, VTG and GSA and builds a fix from them.

    Talker IDs are ignored (``GP``, ``GN`` and ``GL`` are treated alike), and so
    is GSV: per-satellite detail, like ``view`` in the JSON, is not published.
    """

    def __init__(self):
        self._latest = {}

    def feed(self, line: str) -> bool:
        """Ingest one line. Returns True if it was a sentence that was kept."""
        fields = split_sentence(line)
        if not fields or len(fields[0]) != 5:
            return False
        kind = fields[0][2:]
        if kind not in ("GGA", "RMC", "VTG", "GSA"):
            return False
        self._latest[kind] = fields
        return True

    @property
    def complete(self) -> bool:
        """Whether GGA and RMC are both held and describe the same epoch.

        Between a new epoch's GGA and its RMC the two disagree. That is a fix
        half-received, not a fix lost, so callers must not report it as one.
        """
        gga, rmc = self._latest.get("GGA"), self._latest.get("RMC")
        return gga is not None and rmc is not None and _field(gga, 1) == _field(rmc, 1)

    def fix(self) -> Optional[GpsFix]:
        """The current fix, or None when the receiver does not report one.

        GGA and RMC must agree on the epoch's time before they are combined:
        after a reconnect, one may be from an epoch the other hasn't reached.
        Validity takes both RMC's status (``A``) and GGA's quality (non-zero),
        mirroring the JSON path's insistence on a 2D/3D ``fix``.
        """
        if not self.complete:
            return None
        gga, rmc = self._latest["GGA"], self._latest["RMC"]
        if _field(rmc, 2) != "A" or _field(gga, 6) in ("", "0"):
            return None

        vtg = self._latest.get("VTG") or []
        gsa = self._latest.get("GSA") or []

        speed = _number(_field(vtg, 7))
        if speed is None:
            knots = _number(_field(rmc, 7))
            speed = None if knots is None else round(knots * _KNOTS_TO_KMH, 1)

        heading = _field(rmc, 8) or _field(vtg, 1)

        return GpsFix.build(
            latitude=_coordinate(_field(rmc, 3), _field(rmc, 4), 2),
            longitude=_coordinate(_field(rmc, 5), _field(rmc, 6), 3),
            altitude=_field(gga, 9),
            speed=speed,
            heading=heading,
            utc=_utc_timestamp(_field(rmc, 9), _field(rmc, 1)),
            fix_type=_GSA_MODES.get(_field(gsa, 2)),
            satellites=_field(gga, 7),
            hdop=_field(gga, 8),
        )
