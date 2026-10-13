import json

import pytest

from winegard2mqtt.app import Config, poll_once
from winegard2mqtt.publisher import Publisher


class StubClient:
    def __init__(self, status=None, error=None):
        self._status = status or {}
        self._error = error
        self.calls = 0

    def status(self):
        self.calls += 1
        if self._error:
            raise self._error
        return self._status


class RecordingClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.messages.append({"topic": topic, "payload": payload, "retain": retain})

    def last(self, topic):
        for message in reversed(self.messages):
            if message["topic"] == topic:
                return message
        return None


@pytest.fixture
def mqtt():
    return RecordingClient()


@pytest.fixture
def publisher(mqtt):
    return Publisher(mqtt, topic_base="winegard", client_id="connect")


# --- config -----------------------------------------------------------------


def test_password_is_required(monkeypatch):
    monkeypatch.delenv("WINEGARD_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="WINEGARD_PASSWORD"):
        Config.from_env()


def test_defaults_match_the_device(monkeypatch):
    monkeypatch.setenv("WINEGARD_PASSWORD", "secret")
    config = Config.from_env()
    assert config.host == "http://10.11.12.1"
    assert config.username == "admin"
    assert config.poll_interval == 60


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("WINEGARD_PASSWORD", "secret")
    monkeypatch.setenv("WINEGARD_HOST", "10.0.0.5")
    monkeypatch.setenv("WINEGARD_POLL_INTERVAL", "30")
    config = Config.from_env()
    assert config.host == "http://10.0.0.5"
    assert config.poll_interval == 30


def test_gps_source_defaults_to_json(monkeypatch):
    monkeypatch.setenv("WINEGARD_PASSWORD", "secret")
    config = Config.from_env()
    assert config.gps_source == "json"
    assert config.nmea_port == 10110


def test_nmea_source_is_configurable(monkeypatch):
    monkeypatch.setenv("WINEGARD_PASSWORD", "secret")
    monkeypatch.setenv("WINEGARD_GPS_SOURCE", "NMEA")
    monkeypatch.setenv("WINEGARD_NMEA_PORT", "2947")
    monkeypatch.setenv("WINEGARD_NMEA_INTERVAL", "1")
    config = Config.from_env()
    assert config.gps_source == "nmea"
    assert config.nmea_port == 2947
    assert config.nmea_interval == 1.0


def test_an_unknown_gps_source_is_rejected(monkeypatch):
    monkeypatch.setenv("WINEGARD_PASSWORD", "secret")
    monkeypatch.setenv("WINEGARD_GPS_SOURCE", "serial")
    with pytest.raises(ValueError, match="WINEGARD_GPS_SOURCE"):
        Config.from_env()


def test_the_nmea_socket_uses_the_bare_router_address(monkeypatch):
    monkeypatch.setenv("WINEGARD_PASSWORD", "secret")
    assert Config.from_env().router_hostname == "10.11.12.1"


# --- poll cycle -------------------------------------------------------------


def test_poll_publishes_modem_telemetry(publisher, mqtt):
    poll_once(StubClient({"modem_signal": "68%", "modem_state": "connected"}), publisher)

    payload = json.loads(mqtt.last("winegard/connect/modem")["payload"])
    assert payload["signal_percent"] == 68


def test_poll_without_fix_marks_position_unavailable(publisher, mqtt):
    poll_once(StubClient({"gps": {"error": "Not fixed now"}}), publisher)

    assert mqtt.last("winegard/connect/gps/available")["payload"] == "offline"


def test_router_failure_does_not_kill_the_loop(publisher, mqtt):
    """A poll that raises must be swallowed so the next interval still runs."""
    poll_once(StubClient(error=OSError("connection refused")), publisher)

    assert mqtt.last("winegard/connect/modem") is None


def test_router_failure_leaves_bridge_state_alone(publisher, mqtt):
    """Transient unreachability is not the same as the bridge going down."""
    poll_once(StubClient(error=OSError("connection refused")), publisher)

    assert mqtt.last("winegard/connect/state") is None


def test_malformed_status_does_not_kill_the_loop(publisher, mqtt):
    """A status payload that isn't a dict (bad firmware JSON) must not raise,
    and is treated as "nothing usable" rather than crashing the poller."""
    poll_once(StubClient(status=["unexpected"]), publisher)

    assert json.loads(mqtt.last("winegard/connect/modem")["payload"]) == {}
    assert mqtt.last("winegard/connect/gps/available")["payload"] == "offline"


def test_poll_leaves_gps_alone_when_nmea_owns_it(publisher, mqtt):
    """The JSON's position must not overwrite the stream's."""
    poll_once(StubClient({"gps": {"error": "Not fixed now"}, "modem_signal": "68%"}),
              publisher, include_gps=False)

    assert mqtt.last("winegard/connect/gps/available") is None
    assert mqtt.last("winegard/connect/modem") is not None
