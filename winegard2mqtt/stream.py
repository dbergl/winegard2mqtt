"""Read the router's NMEA stream over TCP and publish fixes as they change.

Runs on its own thread beside the ``sys_status`` poll, which keeps supplying the
modem telemetry. Nothing here outlives a bad connection: a refused connect, a
dropped socket or a silent one all mark the position unavailable and retry.

Publishing is throttled. The stream delivers a new epoch every second, which is
more than Home Assistant needs; ``min_interval`` caps the rate. Identical fixes
are also dropped, in case the router ever replays an epoch.
"""

import logging
import socket
import threading
import time
from typing import Callable, Optional

from .nmea import NmeaAccumulator
from .parser import GpsFix

logger = logging.getLogger(__name__)

# Sentinel distinguishing "nothing published yet" from "published no fix".
_UNSET = object()


class NmeaStream:
    def __init__(self, host: str, port: int, publisher, min_interval: float = 5.0,
                 idle_timeout: float = 30.0, retry_delay: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.port = port
        self.publisher = publisher
        self.min_interval = min_interval
        self.idle_timeout = idle_timeout
        self.retry_delay = retry_delay
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._published = _UNSET
        self._published_at = float("-inf")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="nmea", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_connection()
            except OSError as exc:
                logger.warning("NMEA stream %s:%s unavailable: %s",
                               self.host, self.port, exc)
            self.offer(None)
            self._stop.wait(self.retry_delay)

    def _read_connection(self) -> None:
        accumulator = NmeaAccumulator()
        with socket.create_connection((self.host, self.port),
                                      timeout=self.idle_timeout) as sock:
            logger.info("connected to NMEA stream %s:%s", self.host, self.port)
            with sock.makefile("r", encoding="ascii", errors="replace") as lines:
                for line in lines:
                    if self._stop.is_set():
                        return
                    if accumulator.feed(line) and accumulator.complete:
                        self.offer(accumulator.fix())
        logger.warning("NMEA stream %s:%s closed by router", self.host, self.port)

    # -- publishing ----------------------------------------------------------

    def offer(self, fix: Optional[GpsFix]) -> None:
        """Publish ``fix`` if it differs from the last one and is due.

        Gaining or losing the fix publishes at once; only updates to a fix
        that is already held wait out ``min_interval``. An update held back
        that way is superseded by the next epoch, a second later.
        """
        if fix == self._published:
            return
        now = self._clock()
        if fix is not None and self._published not in (None, _UNSET) \
                and now - self._published_at < self.min_interval:
            return
        self.publisher.publish_gps(fix)
        self._published = fix
        self._published_at = now
