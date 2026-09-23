"""Media state tracking and command validation."""

from __future__ import annotations

from fedoralink.plugins.media import MediaPlugin
from fedoralink.protocol import MEDIA, make_packet


class FakeDbus:
    def __init__(self) -> None:
        self.changes = 0

    def emit_changed(self):
        self.changes += 1


class FakeDaemon:
    def __init__(self) -> None:
        self.dbus = FakeDbus()
        self.sent: list[tuple[str, dict]] = []

    def send(self, packet_type, body=None):
        self.sent.append((packet_type, body or {}))
        return True


def deliver(plugin, **body):
    plugin.on_packet(make_packet(MEDIA, body))


class TestState:
    def test_session_metadata_is_recorded(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title="Song", artist="Band")

        assert plugin.has_session is True
        assert plugin.playing is True
        assert plugin.title == "Song"
        assert plugin.artist == "Band"

    def test_no_session_leaves_the_row_empty(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=False, playing=False)
        assert plugin.has_session is False
        assert plugin.title == ""

    def test_losing_a_session_clears_stale_metadata(self):
        # A track title with no phone playing it is worse than nothing.
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title="Song", artist="Band")
        deliver(plugin, hasSession=False)

        assert plugin.title == ""
        assert plugin.artist == ""
        assert plugin.playing is False

    def test_disconnect_clears_state(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title="Song", artist="Band")
        plugin.on_disconnected(None)
        assert plugin.has_session is False

    def test_non_string_metadata_is_ignored(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title={"nope": 1})
        assert plugin.title == ""


class TestChangeNotification:
    def test_new_session_emits_once(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title="Song")
        assert daemon.dbus.changes == 1

    def test_identical_updates_do_not_emit(self):
        # A session that reports in repeatedly must not redraw the menu
        # every time.
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        for _ in range(4):
            deliver(plugin, hasSession=True, playing=True, title="Song")
        assert daemon.dbus.changes == 1

    def test_pausing_emits(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title="Song")
        deliver(plugin, hasSession=True, playing=False, title="Song")
        assert daemon.dbus.changes == 2

    def test_track_change_emits(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        deliver(plugin, hasSession=True, playing=True, title="One")
        deliver(plugin, hasSession=True, playing=True, title="Two")
        assert daemon.dbus.changes == 2

    def test_repeated_empty_state_does_not_emit(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        for _ in range(3):
            deliver(plugin, hasSession=False)
        assert daemon.dbus.changes == 0


class TestCommands:
    def test_known_actions_are_sent(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        for action in ("play", "pause", "playpause", "next", "previous"):
            assert plugin.command(action) is True
        assert [b["action"] for _, b in daemon.sent] == [
            "play", "pause", "playpause", "next", "previous",
        ]

    def test_unknown_action_is_refused_not_forwarded(self):
        # Refusing here beats sending something the phone will silently
        # ignore at the far end.
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        assert plugin.command("selfdestruct") is False
        assert daemon.sent == []

    def test_empty_action_is_refused(self):
        daemon = FakeDaemon()
        plugin = MediaPlugin(daemon)
        assert plugin.command("") is False
        assert daemon.sent == []
