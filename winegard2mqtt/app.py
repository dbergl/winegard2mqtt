"""Poll the Winegard ConnecT and publish to MQTT."""

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

from .client import AuthError, WinegardClient
from .parser import parse_gps, parse_modem
from .publisher import Publisher
from .stream import NmeaStream

logger = logging.getLogger("winegard2mqtt")

GPS_SOURCES = ("json", "nmea")


@dataclass(frozen=True)
class Config:
    host: str
    username: str
    password: str
    poll_interval: int
    mqtt_host: str
    mqtt_port: int
    mqtt_username: Optional[str]
    mqtt_password: Optional[str]
    topic_base: str
    client_id: str
    gps_source: str = "json"
    nmea_port: int = 10110
    nmea_interval: float = 5.0

    @classmethod
    def from_env(cls) -> "Config":
        password = os.environ.get("WINEGARD_PASSWORD")
        if not password:
            raise ValueError("WINEGARD_PASSWORD is required")

        host = os.environ.get("WINEGARD_HOST", "10.11.12.1")
        if not host.startswith(("http://", "https://")):
            host = "http://" + host

        gps_source = os.environ.get("WINEGARD_GPS_SOURCE", "json").strip().lower()
        if gps_source not in GPS_SOURCES:
            raise ValueError(
                f"WINEGARD_GPS_SOURCE must be one of {', '.join(GPS_SOURCES)}, "
                f"not {gps_source!r}"
            )

        return cls(
            host=host,
            username=os.environ.get("WINEGARD_USERNAME", "admin"),
            password=password,
            poll_interval=int(os.environ.get("WINEGARD_POLL_INTERVAL", "60")),
            mqtt_host=os.environ.get("MQTT_HOST", "mosquitto"),
            mqtt_port=int(os.environ.get("MQTT_PORT", "1883")),
            mqtt_username=os.environ.get("MQTT_USERNAME"),
            mqtt_password=os.environ.get("MQTT_PASSWORD"),
            topic_base=os.environ.get("MQTT_TOPIC_BASE", "winegard"),
            client_id=os.environ.get("MQTT_CLIENT_ID", "connect"),
            gps_source=gps_source,
            nmea_port=int(os.environ.get("WINEGARD_NMEA_PORT", "10110")),
            nmea_interval=float(os.environ.get("WINEGARD_NMEA_INTERVAL", "5")),
        )

    @property
    def router_hostname(self) -> str:
        """The bare router address, for the NMEA socket rather than HTTP."""
        return urlparse(self.host).hostname


def poll_once(client: WinegardClient, publisher: Publisher,
              include_gps: bool = True) -> None:
    """One fetch/parse/publish cycle.

    ``include_gps`` is False when the NMEA stream owns the position, so the
    two sources never overwrite each other's payload.

    Router-side failures are logged and swallowed: a router that is briefly
    unreachable, or a cable being reseated, must not take the bridge down. The
    bridge's own state topic is left untouched so it keeps reflecting the
    process, not the router.
    """
    try:
        status = client.status()
        publisher.publish_modem(parse_modem(status))
        if include_gps:
            publisher.publish_gps(parse_gps(status))
    except AuthError:
        logger.exception("authentication failed — check WINEGARD_PASSWORD")
    except Exception:
        logger.exception("poll failed; will retry next interval")


def build_mqtt_client(config: Config, publisher_state_topic: str) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"winegard2mqtt-{config.client_id}",
    )
    if config.mqtt_username:
        client.username_pw_set(config.mqtt_username, config.mqtt_password)
    client.will_set(publisher_state_topic, "offline", qos=0, retain=True)
    return client


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()

    router = WinegardClient(config.host, config.username, config.password)

    state_topic = f"{config.topic_base}/{config.client_id}/state"
    mqtt_client = build_mqtt_client(config, state_topic)
    publisher = Publisher(mqtt_client, config.topic_base, config.client_id)

    mqtt_client.connect(config.mqtt_host, config.mqtt_port)
    mqtt_client.loop_start()

    publisher.publish_bridge_state(online=True)
    publisher.publish_discovery()
    logger.info("polling %s every %ss", config.host, config.poll_interval)

    nmea = None
    if config.gps_source == "nmea":
        nmea = NmeaStream(config.router_hostname, config.nmea_port, publisher,
                          min_interval=config.nmea_interval)
        nmea.start()
        logger.info("GPS from NMEA stream %s:%s", config.router_hostname,
                    config.nmea_port)

    running = True

    def stop(signum, _frame):
        nonlocal running
        logger.info("signal %s received, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while running:
            poll_once(router, publisher, include_gps=nmea is None)
            for _ in range(config.poll_interval):
                if not running:
                    break
                time.sleep(1)
    finally:
        if nmea is not None:
            nmea.stop()
        publisher.publish_bridge_state(online=False)
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
