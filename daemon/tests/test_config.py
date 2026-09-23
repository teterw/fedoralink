"""Config loading and validation.

The daemon must start with a usable config no matter what is in the file,
so every failure path here has to end in defaults rather than an
exception.
"""

from __future__ import annotations

import json

from fedoralink.config import DEFAULTS, config_path, load, merge


class TestDefaults:
    def test_missing_file_gives_defaults(self, tmp_path):
        assert load(tmp_path / "nope.json") == DEFAULTS

    def test_lock_on_disconnect_is_off_by_default(self):
        # A screen that locks on every Bluetooth hiccup is worse than no
        # feature, so this default is load-bearing.
        assert DEFAULTS["lock_on_disconnect"] is False

    def test_load_returns_a_copy(self, tmp_path):
        loaded = load(tmp_path / "nope.json")
        loaded["battery_low_threshold"] = 99
        assert DEFAULTS["battery_low_threshold"] != 99

    def test_config_path_honours_xdg_config_home(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
        assert config_path() == __import__("pathlib").Path(
            "/tmp/xdg/fedoralink/config.json"
        )


class TestOverrides:
    def test_partial_file_overrides_only_what_it_names(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"battery_low_threshold": 25}))

        loaded = load(path)
        assert loaded["battery_low_threshold"] == 25
        assert loaded["battery_low_warning"] is True

    def test_booleans_are_applied(self):
        assert merge({"lock_on_disconnect": True})["lock_on_disconnect"] is True


class TestRejectsBadValues:
    def test_unknown_keys_are_ignored(self):
        merged = merge({"nonsense": 1})
        assert "nonsense" not in merged

    def test_a_bad_key_does_not_discard_the_good_ones(self):
        merged = merge({"nonsense": 1, "battery_low_threshold": 30})
        assert merged["battery_low_threshold"] == 30

    def test_wrong_type_for_a_bool_is_ignored(self):
        assert merge({"lock_on_disconnect": "yes"})["lock_on_disconnect"] is False

    def test_wrong_type_for_a_number_is_ignored(self):
        merged = merge({"battery_low_threshold": "low"})
        assert merged["battery_low_threshold"] == DEFAULTS["battery_low_threshold"]

    def test_true_is_not_accepted_as_a_number(self):
        # bool subclasses int, so this would slip through a naive check and
        # set the threshold to 1%.
        merged = merge({"battery_low_threshold": True})
        assert merged["battery_low_threshold"] == DEFAULTS["battery_low_threshold"]

    def test_negative_numbers_are_ignored(self):
        merged = merge({"lock_on_disconnect_grace_seconds": -5})
        assert merged["lock_on_disconnect_grace_seconds"] == 30

    def test_threshold_above_100_falls_back(self):
        merged = merge({"battery_low_threshold": 150})
        assert merged["battery_low_threshold"] == DEFAULTS["battery_low_threshold"]

    def test_non_object_json_gives_defaults(self):
        assert merge(["not", "an", "object"]) == DEFAULTS

    def test_malformed_json_gives_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ this is not json")
        assert load(path) == DEFAULTS

    def test_a_directory_instead_of_a_file_gives_defaults(self, tmp_path):
        # Reading a directory raises OSError, not FileNotFoundError.
        assert load(tmp_path) == DEFAULTS
