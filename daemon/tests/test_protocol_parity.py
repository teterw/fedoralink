"""The two protocol definitions must stay in step.

`protocol.py` and `Protocol.kt` are the same contract written twice, and
nothing but discipline keeps them together. Adding a packet type to one and
forgetting the other is the easy mistake — it has already happened once,
caught only when CI failed to compile the phone app.

These tests read both files as text. No Kotlin toolchain, no gi, so they
run everywhere the rest of the suite does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PY = REPO / "daemon" / "fedoralink" / "protocol.py"
KT = (
    REPO
    / "android/app/src/main/kotlin/dev/fedoralink/android/Protocol.kt"
)


@pytest.fixture(scope="module")
def sources() -> tuple[str, str]:
    assert PY.is_file(), f"missing {PY}"
    assert KT.is_file(), f"missing {KT}"
    return PY.read_text(encoding="utf-8"), KT.read_text(encoding="utf-8")


def packet_types(python: str, kotlin: str) -> tuple[set[str], set[str]]:
    return (
        set(re.findall(r'^[A-Z_]+ = "(fedoralink\.[a-z.]+)"', python, re.M)),
        set(re.findall(r'const val [A-Z_]+ = "(fedoralink\.[a-z.]+)"', kotlin)),
    )


class TestPacketTypes:
    def test_both_sides_declare_the_same_types(self, sources):
        py_types, kt_types = packet_types(*sources)

        missing_from_kotlin = sorted(py_types - kt_types)
        missing_from_python = sorted(kt_types - py_types)

        assert not missing_from_kotlin, (
            f"declared in protocol.py but not Protocol.kt: {missing_from_kotlin}"
        )
        assert not missing_from_python, (
            f"declared in Protocol.kt but not protocol.py: {missing_from_python}"
        )

    def test_there_are_some_to_compare(self, sources):
        # Guards the regexes: a rename that broke them would otherwise make
        # every test above pass by comparing two empty sets.
        py_types, _ = packet_types(*sources)
        assert len(py_types) >= 10


class TestVersions:
    def test_protocol_version_matches(self, sources):
        python, kotlin = sources
        py_version = re.search(r"^PROTOCOL_VERSION = (\d+)", python, re.M)
        kt_version = re.search(r"const val PROTOCOL_VERSION = (\d+)", kotlin)

        assert py_version and kt_version
        assert py_version.group(1) == kt_version.group(1), (
            "the two sides disagree about the protocol version, so they will "
            "refuse each other"
        )

    def test_minimum_version_matches(self, sources):
        python, kotlin = sources
        py_min = re.search(r"^MIN_PROTOCOL_VERSION = (\d+)", python, re.M)
        kt_min = re.search(r"const val MIN_PROTOCOL_VERSION = (\d+)", kotlin)

        assert py_min and kt_min
        assert py_min.group(1) == kt_min.group(1)

    def test_service_uuid_matches(self, sources):
        python, kotlin = sources
        py_uuid = re.search(r'SERVICE_UUID = "([0-9a-f-]+)"', python)
        kt_uuid = re.search(r'UUID\.fromString\("([0-9a-f-]+)"\)', kotlin)

        assert py_uuid and kt_uuid
        # Get this wrong and the phone simply never finds the PC.
        assert py_uuid.group(1) == kt_uuid.group(1)

    def test_max_line_bytes_matches(self, sources):
        python, kotlin = sources
        py_max = re.search(r"MAX_LINE_BYTES = (\d+) \* 1024", python)
        kt_max = re.search(r"MAX_LINE_BYTES = (\d+) \* 1024", kotlin)

        assert py_max and kt_max
        # A mismatch means one side accepts a packet the other rejects.
        assert py_max.group(1) == kt_max.group(1)


class TestAuthStages:
    def test_kotlin_declares_every_stage_the_daemon_sends(self, sources):
        _, kotlin = sources
        plugin = (REPO / "daemon/fedoralink/plugins/auth.py").read_text("utf-8")

        py_stages = set(re.findall(r'^STAGE_[A-Z]+ = "([a-z]+)"', plugin, re.M))
        kt_stages = set(re.findall(r'const val STAGE_[A-Z]+ = "([a-z]+)"', kotlin))

        assert py_stages, "no auth stages found — did the regex rot?"
        assert py_stages == kt_stages, (
            f"auth stages differ: python={sorted(py_stages)} "
            f"kotlin={sorted(kt_stages)}"
        )
