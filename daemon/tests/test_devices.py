"""Trust store persistence, permissions and failure modes."""

from __future__ import annotations

import json
import os
import stat

from fedoralink.auth import new_secret
from fedoralink.devices import DeviceStore, store_path


class TestEmpty:
    def test_missing_file_is_an_empty_store(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        assert store.known_devices() == {}
        assert store.is_trusted("anything") is False

    def test_secret_for_unknown_device_is_none(self, tmp_path):
        assert DeviceStore(tmp_path / "devices.json").secret_for("nope") is None

    def test_store_path_honours_xdg_data_home(self, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", "/tmp/data")
        assert str(store_path()) == "/tmp/data/fedoralink/devices.json"


class TestTrust:
    def test_trusted_device_is_readable_back(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        secret = new_secret()
        store.trust("dev-1", "Pixel", secret)

        assert store.is_trusted("dev-1")
        assert store.secret_for("dev-1") == secret
        assert store.name_for("dev-1") == "Pixel"

    def test_survives_a_reload(self, tmp_path):
        path = tmp_path / "devices.json"
        secret = new_secret()
        DeviceStore(path).trust("dev-1", "Pixel", secret)

        # A daemon restart must not force re-enrollment.
        assert DeviceStore(path).secret_for("dev-1") == secret

    def test_creates_the_parent_directory(self, tmp_path):
        store = DeviceStore(tmp_path / "nested" / "deeper" / "devices.json")
        store.trust("dev-1", "Pixel", new_secret())
        assert store.path.exists()

    def test_re_trusting_replaces_the_secret(self, tmp_path):
        path = tmp_path / "devices.json"
        store = DeviceStore(path)
        store.trust("dev-1", "Pixel", new_secret())
        second = new_secret()
        store.trust("dev-1", "Pixel", second)

        assert DeviceStore(path).secret_for("dev-1") == second

    def test_several_devices_coexist(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.trust("a", "Pixel", new_secret())
        store.trust("b", "Tablet", new_secret())
        assert store.known_devices() == {"a": "Pixel", "b": "Tablet"}


class TestRevoke:
    def test_revoke_removes_the_device(self, tmp_path):
        path = tmp_path / "devices.json"
        store = DeviceStore(path)
        store.trust("dev-1", "Pixel", new_secret())

        assert store.revoke("dev-1") is True
        assert store.is_trusted("dev-1") is False
        assert DeviceStore(path).is_trusted("dev-1") is False

    def test_revoking_an_unknown_device_reports_false(self, tmp_path):
        assert DeviceStore(tmp_path / "devices.json").revoke("nope") is False

    def test_revoke_leaves_other_devices_alone(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.trust("a", "Pixel", new_secret())
        store.trust("b", "Tablet", new_secret())
        store.revoke("a")
        assert list(store.known_devices()) == ["b"]


class TestPermissions:
    def test_file_is_owner_only(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.trust("dev-1", "Pixel", new_secret())

        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert mode == 0o600, f"secrets readable beyond the owner: {oct(mode)}"

    def test_permissions_are_repaired_on_write(self, tmp_path):
        path = tmp_path / "devices.json"
        store = DeviceStore(path)
        store.trust("dev-1", "Pixel", new_secret())

        # A store copied in by hand, or predating the chmod.
        os.chmod(path, 0o644)
        store.trust("dev-2", "Tablet", new_secret())

        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_no_temp_files_are_left_behind(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.trust("dev-1", "Pixel", new_secret())
        assert [p.name for p in tmp_path.iterdir()] == ["devices.json"]


class TestCorruptStore:
    def test_malformed_json_yields_an_empty_store(self, tmp_path):
        path = tmp_path / "devices.json"
        path.write_text("{ not json")
        # Empty means every device must re-enroll — inconvenient, and far
        # better than accepting one without a secret.
        assert DeviceStore(path).known_devices() == {}

    def test_non_object_json_yields_an_empty_store(self, tmp_path):
        path = tmp_path / "devices.json"
        path.write_text("[1, 2, 3]")
        assert DeviceStore(path).known_devices() == {}

    def test_entries_without_a_secret_are_dropped(self, tmp_path):
        path = tmp_path / "devices.json"
        path.write_text(json.dumps({"a": {"name": "No secret"}}))
        assert DeviceStore(path).is_trusted("a") is False

    def test_entries_with_a_non_string_secret_are_dropped(self, tmp_path):
        path = tmp_path / "devices.json"
        path.write_text(json.dumps({"a": {"name": "x", "secret": 1234}}))
        assert DeviceStore(path).is_trusted("a") is False

    def test_good_entries_survive_a_bad_neighbour(self, tmp_path):
        path = tmp_path / "devices.json"
        secret = new_secret()
        path.write_text(
            json.dumps({"good": {"name": "Pixel", "secret": secret}, "bad": "oops"})
        )
        store = DeviceStore(path)
        assert store.secret_for("good") == secret
        assert store.is_trusted("bad") is False
