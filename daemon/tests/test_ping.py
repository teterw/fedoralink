"""Ping semantics in both directions.

Three distinct meanings share the fedoralink.ping type, and conflating
them is the easy mistake: no `ring` key is the link test, `ring: true`
rings this PC, `ring: false` silences it.
"""

from __future__ import annotations

from fedoralink.config import DEFAULTS
from fedoralink.plugins.ping import PingPlugin
from fedoralink.protocol import PING, make_packet


class FakeNotifications:
    def __init__(self) -> None:
        self.shown: list[dict] = []

    def show_local(self, summary, body="", *, urgent=False, sound_name=None):
        self.shown.append(
            {
                "summary": summary,
                "body": body,
                "urgent": urgent,
                "sound_name": sound_name,
            }
        )


class FakeAlerter:
    def __init__(self, can_play=True) -> None:
        self.can_play = can_play
        self.fallback_sound_name = None if can_play else "alarm-clock-elapsed"
        self.started: list[int] = []
        self.stopped = 0

    def start(self, seconds):
        self.started.append(seconds)

    def stop(self):
        self.stopped += 1


class FakeDaemon:
    def __init__(self, can_play=True, **overrides) -> None:
        self.config = dict(DEFAULTS) | overrides
        self.notifications = FakeNotifications()
        self.alerter = FakeAlerter(can_play)
        self.device_name = "Pixel"
        self.sent: list[tuple[str, dict]] = []

    def send(self, packet_type, body=None):
        self.sent.append((packet_type, body or {}))
        return True


def deliver(plugin, body):
    plugin.on_packet(make_packet(PING, body))


class TestOutbound:
    def test_ring_phone_sends_ring_true(self):
        daemon = FakeDaemon()
        PingPlugin(daemon).ring_phone()
        assert daemon.sent == [(PING, {"ring": True})]

    def test_stop_ringing_sends_ring_false(self):
        daemon = FakeDaemon()
        PingPlugin(daemon).stop_ringing()
        assert daemon.sent == [(PING, {"ring": False})]


class TestPlainPing:
    def test_no_ring_key_shows_a_quiet_notification(self):
        daemon = FakeDaemon()
        deliver(PingPlugin(daemon), {})

        assert len(daemon.notifications.shown) == 1
        assert daemon.notifications.shown[0]["urgent"] is False
        assert daemon.alerter.started == []

    def test_custom_message_is_used(self):
        daemon = FakeDaemon()
        deliver(PingPlugin(daemon), {"message": "hello from the bus"})
        assert daemon.notifications.shown[0]["body"] == "hello from the bus"


class TestRingThePc:
    def test_ring_true_starts_the_alerter(self):
        daemon = FakeDaemon()
        deliver(PingPlugin(daemon), {"ring": True})
        assert daemon.alerter.started == [DEFAULTS["pc_ring_seconds"]]

    def test_ring_true_posts_an_urgent_notification(self):
        daemon = FakeDaemon()
        deliver(PingPlugin(daemon), {"ring": True})
        assert daemon.notifications.shown[0]["urgent"] is True

    def test_duration_is_configurable(self):
        daemon = FakeDaemon(pc_ring_seconds=30)
        deliver(PingPlugin(daemon), {"ring": True})
        assert daemon.alerter.started == [30]

    def test_no_sound_hint_when_canberra_can_play(self):
        # Both would double up, so the hint is only set as a fallback.
        daemon = FakeDaemon(can_play=True)
        deliver(PingPlugin(daemon), {"ring": True})
        assert daemon.notifications.shown[0]["sound_name"] is None

    def test_sound_hint_is_the_fallback_without_canberra(self):
        daemon = FakeDaemon(can_play=False)
        deliver(PingPlugin(daemon), {"ring": True})
        assert daemon.notifications.shown[0]["sound_name"] == "alarm-clock-elapsed"


class TestStopRingingThePc:
    def test_ring_false_stops_the_alerter(self):
        daemon = FakeDaemon()
        deliver(PingPlugin(daemon), {"ring": False})
        assert daemon.alerter.stopped == 1

    def test_ring_false_shows_no_notification(self):
        # The bug this pins: treating ring:false as a plain ping would pop
        # a "Ping from your phone" notification while silencing the alert.
        daemon = FakeDaemon()
        deliver(PingPlugin(daemon), {"ring": False})
        assert daemon.notifications.shown == []
