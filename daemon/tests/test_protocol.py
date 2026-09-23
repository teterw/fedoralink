"""Framing tests for the wire protocol.

``PacketReader`` is where a bug breaks everything at once: it sits under
every feature, and RFCOMM hands it a byte stream that can split anywhere.
These tests drive it with the splits a real socket produces.

Pure Python by design — ``protocol.py`` imports no gi, so this suite runs
without PyGObject.
"""

from __future__ import annotations

import json

import pytest

from fedoralink.protocol import (
    MAX_LINE_BYTES,
    PacketReader,
    ProtocolError,
    make_packet,
    serialize,
)


def line(packet_type: str, **body: object) -> bytes:
    return serialize(make_packet(packet_type, body))


class TestReassembly:
    def test_whole_packet_in_one_feed(self):
        reader = PacketReader()
        packets = list(reader.feed(line("fedoralink.battery", level=42)))

        assert len(packets) == 1
        assert packets[0]["type"] == "fedoralink.battery"
        assert packets[0]["body"]["level"] == 42

    def test_packet_split_across_two_reads(self):
        reader = PacketReader()
        raw = line("fedoralink.battery", level=42)
        cut = len(raw) // 2

        # First half carries no newline, so nothing is complete yet.
        assert list(reader.feed(raw[:cut])) == []

        packets = list(reader.feed(raw[cut:]))
        assert len(packets) == 1
        assert packets[0]["body"]["level"] == 42

    def test_packet_split_byte_by_byte(self):
        reader = PacketReader()
        raw = line("fedoralink.ping", ring=True)

        yielded = []
        for index in range(len(raw)):
            yielded.extend(reader.feed(raw[index : index + 1]))

        assert len(yielded) == 1
        assert yielded[0]["body"]["ring"] is True

    def test_several_packets_in_one_feed(self):
        reader = PacketReader()
        raw = line("fedoralink.battery", level=1) + line("fedoralink.battery", level=2)

        packets = list(reader.feed(raw))
        assert [p["body"]["level"] for p in packets] == [1, 2]

    def test_trailing_partial_packet_is_held_for_the_next_read(self):
        reader = PacketReader()
        complete = line("fedoralink.battery", level=7)
        partial = line("fedoralink.battery", level=8)[:5]

        packets = list(reader.feed(complete + partial))
        assert [p["body"]["level"] for p in packets] == [7]

        rest = line("fedoralink.battery", level=8)[5:]
        assert [p["body"]["level"] for p in reader.feed(rest)] == [8]


class TestMultibyte:
    # A notification body is arbitrary user text, so this is the normal
    # case rather than an exotic one.
    THAI = "ทดสอบการแจ้งเตือน"
    EMOJI = "ระบบ 🔔 พร้อม"

    def test_split_mid_multibyte_character_does_not_mojibake(self):
        reader = PacketReader()
        raw = line("fedoralink.notification", body=self.THAI)

        # Cut inside a multi-byte sequence: decoding the halves
        # independently would raise or produce replacement characters.
        cut = raw.index(self.THAI.encode("utf-8")) + 1
        assert list(reader.feed(raw[:cut])) == []

        packets = list(reader.feed(raw[cut:]))
        assert packets[0]["body"]["body"] == self.THAI
        assert "�" not in packets[0]["body"]["body"]

    def test_emoji_split_byte_by_byte(self):
        reader = PacketReader()
        raw = line("fedoralink.notification", body=self.EMOJI)

        yielded = []
        for index in range(len(raw)):
            yielded.extend(reader.feed(raw[index : index + 1]))

        assert yielded[0]["body"]["body"] == self.EMOJI

    def test_serialize_does_not_escape_non_ascii(self):
        # ensure_ascii=False is deliberate: \uXXXX escapes would roughly
        # triple the size of a Thai notification.
        raw = line("fedoralink.notification", body=self.THAI)
        assert self.THAI.encode("utf-8") in raw


class TestOversizeLine:
    def test_oversize_line_with_no_newline_raises(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError, match="exceeded"):
            reader.feed(b"x" * (MAX_LINE_BYTES + 1)).__next__()

    def test_oversize_line_clears_the_buffer(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError):
            list(reader.feed(b"x" * (MAX_LINE_BYTES + 1)))

        # The garbage must not still be sitting in the buffer, or the next
        # good packet is prefixed with it and fails to parse.
        packets = list(reader.feed(line("fedoralink.battery", level=5)))
        assert [p["body"]["level"] for p in packets] == [5]

    def test_a_large_but_legal_packet_is_accepted(self):
        reader = PacketReader()
        big = "c" * (MAX_LINE_BYTES // 2)

        packets = list(reader.feed(line("fedoralink.clipboard", content=big)))
        assert packets[0]["body"]["content"] == big


class TestMalformed:
    def test_malformed_json_raises(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError, match="malformed"):
            list(reader.feed(b"{not json at all}\n"))

    def test_stream_stays_usable_after_a_malformed_packet(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError):
            list(reader.feed(b"{not json}\n"))

        # Framing is intact — the newline ended the bad line, so the next
        # packet must parse. This is why on_packet survives one bad packet
        # instead of dropping the socket.
        packets = list(reader.feed(line("fedoralink.battery", level=9)))
        assert [p["body"]["level"] for p in packets] == [9]

    def test_packet_without_a_type_raises(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError, match="missing 'type'"):
            list(reader.feed(b'{"body":{}}\n'))

    def test_non_object_packet_raises(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError, match="missing 'type'"):
            list(reader.feed(b'["not", "an", "object"]\n'))

    def test_invalid_utf8_raises(self):
        reader = PacketReader()

        with pytest.raises(ProtocolError, match="malformed"):
            list(reader.feed(b"\xff\xfe not utf-8\n"))

    def test_body_defaults_to_empty_dict(self):
        reader = PacketReader()

        packets = list(reader.feed(b'{"type":"fedoralink.ping"}\n'))
        assert packets[0]["body"] == {}


class TestBlankLines:
    def test_blank_lines_are_skipped(self):
        reader = PacketReader()
        raw = b"\n\n" + line("fedoralink.ping") + b"\n"

        packets = list(reader.feed(raw))
        assert len(packets) == 1
        assert packets[0]["type"] == "fedoralink.ping"

    def test_whitespace_only_lines_are_skipped(self):
        reader = PacketReader()

        packets = list(reader.feed(b"   \n\t\n" + line("fedoralink.ping")))
        assert len(packets) == 1


class TestReset:
    def test_reset_discards_a_partial_packet(self):
        reader = PacketReader()
        reader.feed(line("fedoralink.battery", level=3)[:6])

        # A reconnect must not splice the old half-packet onto the new
        # stream's first read.
        reader.reset()

        packets = list(reader.feed(line("fedoralink.battery", level=4)))
        assert [p["body"]["level"] for p in packets] == [4]


class TestSerialize:
    def test_serialize_ends_with_exactly_one_newline(self):
        raw = serialize(make_packet("fedoralink.ping"))
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1

    def test_serialize_is_compact(self):
        raw = serialize(make_packet("fedoralink.battery", {"level": 1}))
        assert b", " not in raw
        assert b'": ' not in raw

    def test_embedded_newline_in_text_is_escaped_not_emitted(self):
        # A notification body with a newline must not split into two
        # packets — json escapes it, and this pins that guarantee.
        raw = serialize(make_packet("fedoralink.notification", {"body": "a\nb"}))
        assert raw.count(b"\n") == 1

        packets = list(PacketReader().feed(raw))
        assert packets[0]["body"]["body"] == "a\nb"

    def test_round_trip_through_the_reader(self):
        packet = make_packet("fedoralink.clipboard", {"content": "hello"})
        packets = list(PacketReader().feed(serialize(packet)))
        assert packets[0] == packet

    def test_make_packet_has_an_id_and_a_body(self):
        packet = make_packet("fedoralink.ping")
        assert isinstance(packet["id"], int)
        assert packet["body"] == {}

    def test_make_packet_body_is_preserved_verbatim(self):
        body = {"ring": False, "nested": {"a": [1, 2]}}
        packet = make_packet("fedoralink.ping", body)
        assert json.loads(serialize(packet).decode())["body"] == body
