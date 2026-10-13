import dataclasses
import json
import pathlib
import socket
import threading
import time

import pytest

from winegard2mqtt.nmea import NmeaAccumulator
from winegard2mqtt.publisher import Publisher
from winegard2mqtt.stream import NmeaStream

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EPOCH = (FIXTURES / "nmea_fix.txt").read_bytes()


class RecordingClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.messages.append({"topic": topic, "payload": payload})

    def on(self, topic):
        return [m["payload"] for m in self.messages if m["topic"] == topic]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def mqtt():
    return RecordingClient()


@pytest.fixture
def publisher(mqtt):
    return Publisher(mqtt, topic_base="winegard", client_id="connect")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fix():
    accumulator = NmeaAccumulator()
    for line in EPOCH.decode().splitlines():
        accumulator.feed(line)
    return accumulator.fix()


def make_stream(publisher, clock, **kwargs):
    return NmeaStream("127.0.0.1", 0, publisher, min_interval=5.0,
                      clock=clock, **kwargs)


# --- dedupe and throttle ----------------------------------------------------


def test_a_repeated_epoch_is_published_once(publisher, mqtt, clock, fix):
    """The router repeats one epoch ~7 times a second; the broker needs it once."""
    stream = make_stream(publisher, clock)
    for _ in range(50):
        stream.offer(fix)
    assert len(mqtt.on("winegard/connect/gps")) == 1


def test_a_changed_fix_waits_for_the_interval(publisher, mqtt, clock, fix):
    stream = make_stream(publisher, clock)
    stream.offer(fix)
    moved = dataclasses.replace(fix, latitude=fix.latitude + 0.001)

    clock.now += 2
    stream.offer(moved)
    assert len(mqtt.on("winegard/connect/gps")) == 1

    clock.now += 3
    stream.offer(moved)
    assert len(mqtt.on("winegard/connect/gps")) == 2


def test_losing_the_fix_is_published_immediately(publisher, mqtt, clock, fix):
    stream = make_stream(publisher, clock)
    stream.offer(fix)
    stream.offer(None)
    assert mqtt.on("winegard/connect/gps/available")[-1] == "offline"


def test_regaining_the_fix_is_published_immediately(publisher, mqtt, clock, fix):
    stream = make_stream(publisher, clock)
    stream.offer(fix)
    stream.offer(None)
    stream.offer(fix)
    assert mqtt.on("winegard/connect/gps/available")[-1] == "online"


def test_no_fix_is_published_once(publisher, mqtt, clock):
    stream = make_stream(publisher, clock)
    stream.offer(None)
    stream.offer(None)
    assert mqtt.on("winegard/connect/gps/available") == ["offline"]


# --- over a real socket -----------------------------------------------------


class NmeaServer:
    """A local TCP server that sends one canned payload per connection."""

    def __init__(self, payload: bytes, hold_open: bool = True):
        self.payload = payload
        self.hold_open = hold_open
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.release = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.sendall(self.payload)
                if self.hold_open:
                    self.release.wait(5)

    def close(self):
        self.release.set()
        self.sock.close()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a publish"
        time.sleep(0.01)


def test_a_streamed_epoch_publishes_the_position(publisher, mqtt):
    server = NmeaServer(EPOCH * 3)
    stream = NmeaStream("127.0.0.1", server.port, publisher)
    stream.start()
    try:
        wait_for(lambda: mqtt.on("winegard/connect/gps"))
        payload = json.loads(mqtt.on("winegard/connect/gps")[0])
        assert payload["latitude"] == pytest.approx(12.34567)
        assert payload["utc"] == "2026-10-12T20:36:28+00:00"
        assert mqtt.on("winegard/connect/gps/available") == ["online"]
    finally:
        stream.stop()
        server.close()


def test_a_dropped_connection_marks_the_position_unavailable(publisher, mqtt):
    server = NmeaServer(EPOCH, hold_open=False)
    stream = NmeaStream("127.0.0.1", server.port, publisher, retry_delay=60)
    stream.start()
    try:
        wait_for(lambda: mqtt.on("winegard/connect/gps/available")[-1:] == ["offline"])
        assert mqtt.on("winegard/connect/gps/available") == ["online", "offline"]
    finally:
        stream.stop()
        server.close()


def test_a_silent_connection_marks_the_position_unavailable(publisher, mqtt):
    """The router accepting the socket but sending nothing is not a fix."""
    server = NmeaServer(b"")
    stream = NmeaStream("127.0.0.1", server.port, publisher,
                        idle_timeout=0.2, retry_delay=60)
    stream.start()
    try:
        wait_for(lambda: mqtt.on("winegard/connect/gps/available"))
        assert mqtt.on("winegard/connect/gps/available") == ["offline"]
    finally:
        stream.stop()
        server.close()


def test_a_refused_connection_marks_the_position_unavailable(publisher, mqtt):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    stream = NmeaStream("127.0.0.1", port, publisher, retry_delay=60)
    stream.start()
    try:
        wait_for(lambda: mqtt.on("winegard/connect/gps/available"))
        assert mqtt.on("winegard/connect/gps/available") == ["offline"]
    finally:
        stream.stop()
