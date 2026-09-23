"""Low-battery warning behaviour.

battery.py touches nothing in gi.repository, so the plugin runs against a
fake daemon with no main loop.
"""

from __future__ import annotations

from fedoralink.config import DEFAULTS
from fedoralink.plugins.battery import BatteryPlugin
from fedoralink.protocol import BATTERY, make_packet


class FakeNotifications:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def show_local(self, summary, body="", **_kwargs):
        self.shown.append((summary, body))


class FakeDaemon:
    def __init__(self, **overrides) -> None:
        self.config = dict(DEFAULTS) | overrides
        self.notifications = FakeNotifications()
        self.device_name = "Pixel"
        self.connected = True
        self.battery = None

    def set_battery(self, level, charging):
        self.battery = (level, charging)


def report(plugin, level, charging=False):
    plugin.on_packet(make_packet(BATTERY, {"level": level, "charging": charging}))


class TestLevelReporting:
    def test_level_reaches_the_shell(self):
        daemon = FakeDaemon()
        report(BatteryPlugin(daemon), 80)
        assert daemon.battery == (80, False)

    def test_out_of_range_level_is_ignored(self):
        daemon = FakeDaemon()
        report(BatteryPlugin(daemon), 150)
        assert daemon.battery is None

    def test_non_integer_level_is_ignored(self):
        daemon = FakeDaemon()
        plugin = BatteryPlugin(daemon)
        plugin.on_packet(make_packet(BATTERY, {"level": "low"}))
        assert daemon.battery is None

    def test_disconnect_clears_the_level(self):
        # The plugin passes None; turning that into -1 for the shell is
        # Daemon.set_battery's job, not the plugin's.
        daemon = FakeDaemon()
        plugin = BatteryPlugin(daemon)
        report(plugin, 80)
        plugin.on_disconnected(None)
        assert daemon.battery == (None, False)


class TestLowBatteryWarning:
    def test_warns_below_the_threshold(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        report(BatteryPlugin(daemon), 10)

        assert len(daemon.notifications.shown) == 1
        summary, body = daemon.notifications.shown[0]
        assert "Pixel" in summary
        assert "10%" in body

    def test_warns_exactly_at_the_threshold(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        report(BatteryPlugin(daemon), 15)
        assert len(daemon.notifications.shown) == 1

    def test_does_not_warn_above_the_threshold(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        report(BatteryPlugin(daemon), 16)
        assert daemon.notifications.shown == []

    def test_warns_only_once_while_it_stays_low(self):
        # A phone sitting at 12% reports in repeatedly; one warning is
        # information, six is a nuisance.
        daemon = FakeDaemon(battery_low_threshold=15)
        plugin = BatteryPlugin(daemon)

        for _ in range(5):
            report(plugin, 12)

        assert len(daemon.notifications.shown) == 1

    def test_charging_suppresses_the_warning(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        report(BatteryPlugin(daemon), 5, charging=True)
        assert daemon.notifications.shown == []

    def test_rearms_after_charging(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        plugin = BatteryPlugin(daemon)

        report(plugin, 10)
        report(plugin, 10, charging=True)
        report(plugin, 50)
        report(plugin, 10)

        assert len(daemon.notifications.shown) == 2

    def test_rearms_after_rising_above_the_threshold(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        plugin = BatteryPlugin(daemon)

        report(plugin, 10)
        report(plugin, 40)
        report(plugin, 10)

        assert len(daemon.notifications.shown) == 2

    def test_rearms_after_a_reconnect(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        plugin = BatteryPlugin(daemon)

        report(plugin, 10)
        plugin.on_disconnected(None)
        report(plugin, 10)

        assert len(daemon.notifications.shown) == 2

    def test_disabled_by_config(self):
        daemon = FakeDaemon(battery_low_warning=False)
        report(BatteryPlugin(daemon), 2)
        assert daemon.notifications.shown == []

    def test_threshold_is_configurable(self):
        daemon = FakeDaemon(battery_low_threshold=50)
        report(BatteryPlugin(daemon), 40)
        assert len(daemon.notifications.shown) == 1

    def test_falls_back_to_phone_when_no_device_name(self):
        daemon = FakeDaemon(battery_low_threshold=15)
        daemon.device_name = ""
        report(BatteryPlugin(daemon), 5)
        assert "Phone" in daemon.notifications.shown[0][0]
