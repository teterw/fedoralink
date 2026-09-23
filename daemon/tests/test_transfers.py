"""File transfer accounting — the part where a mistake corrupts a file."""

from __future__ import annotations

import hashlib

import pytest

from fedoralink.transfers import (
    CHUNK_BYTES,
    MAX_FILE_BYTES,
    IncomingTransfer,
    OutgoingTransfer,
    TransferError,
    safe_name,
    unique_path,
    validate_offer,
)


class TestSafeName:
    def test_ordinary_name_survives(self):
        assert safe_name("holiday.jpg") == "holiday.jpg"

    def test_path_traversal_is_stripped(self):
        # The peer chose this string and it lands on our filesystem.
        assert safe_name("../../etc/passwd") == "passwd"

    def test_absolute_path_is_stripped(self):
        assert safe_name("/etc/shadow") == "shadow"

    def test_windows_separators_are_stripped(self):
        assert safe_name("..\\..\\windows\\system32\\evil.dll") == "evil.dll"

    def test_leading_dots_are_removed(self):
        # Otherwise a peer can drop a hidden file into Downloads.
        assert not safe_name(".bashrc").startswith(".")

    def test_shell_metacharacters_are_replaced(self):
        assert safe_name("a;rm -rf ~.txt") == "a_rm_-rf__.txt"

    def test_non_string_falls_back(self):
        assert safe_name(None) == "received-file"
        assert safe_name(1234) == "received-file"

    def test_empty_falls_back(self):
        assert safe_name("") == "received-file"
        assert safe_name("...") == "received-file"

    def test_absurdly_long_name_is_truncated(self):
        assert len(safe_name("x" * 5000)) <= 200

    def test_result_is_never_a_path(self):
        for hostile in ["../x", "/x", "a/b/c", "..", "."]:
            assert "/" not in safe_name(hostile)


class TestUniquePath:
    def test_uses_the_name_when_free(self, tmp_path):
        assert unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"

    def test_does_not_overwrite_an_existing_file(self, tmp_path):
        # Silently clobbering something the user already had is not an
        # acceptable outcome of accepting a transfer.
        (tmp_path / "a.txt").write_text("mine")
        assert unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"

    def test_keeps_counting(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "a (2).txt").write_text("x")
        assert unique_path(tmp_path, "a.txt") == tmp_path / "a (3).txt"

    def test_handles_names_without_a_suffix(self, tmp_path):
        (tmp_path / "data").write_text("x")
        assert unique_path(tmp_path, "data") == tmp_path / "data (2)"


class TestValidateOffer:
    def test_a_good_offer_passes(self):
        assert validate_offer({"id": "t1", "name": "a.txt", "size": 10}) == (
            "t1", "a.txt", 10,
        )

    def test_missing_id_is_refused(self):
        with pytest.raises(TransferError, match="no id"):
            validate_offer({"name": "a", "size": 1})

    def test_non_string_id_is_refused(self):
        with pytest.raises(TransferError, match="no id"):
            validate_offer({"id": 1, "size": 1})

    def test_missing_size_is_refused(self):
        with pytest.raises(TransferError, match="usable size"):
            validate_offer({"id": "t1"})

    def test_negative_size_is_refused(self):
        with pytest.raises(TransferError, match="usable size"):
            validate_offer({"id": "t1", "size": -1})

    def test_boolean_size_is_refused(self):
        # bool subclasses int, so True would otherwise pass as size 1.
        with pytest.raises(TransferError, match="usable size"):
            validate_offer({"id": "t1", "size": True})

    def test_absurd_size_is_refused(self):
        with pytest.raises(TransferError, match="too large"):
            validate_offer({"id": "t1", "size": MAX_FILE_BYTES + 1})

    def test_zero_size_is_allowed(self):
        assert validate_offer({"id": "t1", "name": "e", "size": 0})[2] == 0

    def test_hostile_name_is_sanitised(self):
        assert validate_offer({"id": "t1", "name": "../x", "size": 1})[1] == "x"


class TestIncomingTransfer:
    def start(self, tmp_path, payload=b"hello world"):
        dest = tmp_path / "out.bin"
        return IncomingTransfer("t1", dest, len(payload)), dest, payload

    def test_whole_file_round_trips(self, tmp_path):
        transfer, dest, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload)
        assert transfer.finish(hashlib.sha256(payload).hexdigest()) == dest
        assert dest.read_bytes() == payload

    def test_several_chunks(self, tmp_path):
        payload = bytes(range(256)) * 10
        transfer = IncomingTransfer("t1", tmp_path / "o.bin", len(payload))
        for seq, start in enumerate(range(0, len(payload), 100)):
            transfer.write_chunk(seq, payload[start : start + 100])
        transfer.finish(hashlib.sha256(payload).hexdigest())
        assert (tmp_path / "o.bin").read_bytes() == payload

    def test_nothing_appears_until_it_is_verified(self, tmp_path):
        # A dropped link must not leave a half-file wearing the real name.
        transfer, dest, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload[:4])
        assert not dest.exists()
        assert transfer.temp_path.exists()

    def test_out_of_order_chunk_is_refused(self, tmp_path):
        transfer, _, payload = self.start(tmp_path)
        with pytest.raises(TransferError, match="out of order"):
            transfer.write_chunk(1, payload)

    def test_duplicate_chunk_is_refused(self, tmp_path):
        transfer, _, _ = self.start(tmp_path)
        transfer.write_chunk(0, b"hello")
        with pytest.raises(TransferError, match="out of order"):
            transfer.write_chunk(0, b"hello")

    def test_non_integer_sequence_is_refused(self, tmp_path):
        transfer, _, _ = self.start(tmp_path)
        with pytest.raises(TransferError, match="sequence"):
            transfer.write_chunk("0", b"hello")

    def test_overlong_payload_is_refused(self, tmp_path):
        # A peer that keeps sending must not be able to fill the disk.
        transfer, _, payload = self.start(tmp_path)
        with pytest.raises(TransferError, match="more data than it declared"):
            transfer.write_chunk(0, payload + b"extra")

    def test_wrong_hash_discards_the_file(self, tmp_path):
        transfer, dest, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload)
        with pytest.raises(TransferError, match="hash does not match"):
            transfer.finish("0" * 64)

        assert not dest.exists()
        assert not transfer.temp_path.exists()

    def test_short_transfer_is_refused_and_discarded(self, tmp_path):
        transfer, dest, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload[:4])
        with pytest.raises(TransferError, match="ended early"):
            transfer.finish(None)
        assert not dest.exists()
        assert not transfer.temp_path.exists()

    def test_discard_leaves_nothing(self, tmp_path):
        transfer, dest, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload[:4])
        transfer.discard()
        assert not dest.exists()
        assert not transfer.temp_path.exists()

    def test_discard_is_idempotent(self, tmp_path):
        transfer, _, _ = self.start(tmp_path)
        transfer.discard()
        transfer.discard()

    def test_chunk_after_finish_is_refused(self, tmp_path):
        transfer, _, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload)
        transfer.finish(None)
        with pytest.raises(TransferError, match="finished"):
            transfer.write_chunk(1, b"more")

    def test_empty_file_works(self, tmp_path):
        transfer = IncomingTransfer("t1", tmp_path / "empty", 0)
        transfer.finish(hashlib.sha256(b"").hexdigest())
        assert (tmp_path / "empty").read_bytes() == b""

    def test_progress_reaches_one(self, tmp_path):
        transfer, _, payload = self.start(tmp_path)
        assert transfer.progress == 0.0
        transfer.write_chunk(0, payload)
        assert transfer.progress == 1.0

    def test_missing_hash_is_accepted(self, tmp_path):
        # An older peer may not send one; length still has to match.
        transfer, dest, payload = self.start(tmp_path)
        transfer.write_chunk(0, payload)
        assert transfer.finish(None) == dest


class TestOutgoingTransfer:
    def test_reads_the_whole_file(self, tmp_path):
        source = tmp_path / "in.bin"
        payload = bytes(range(256)) * 500
        source.write_bytes(payload)

        transfer = OutgoingTransfer("t1", source)
        collected = b""
        expected_seq = 0
        while True:
            chunk = transfer.next_chunk()
            if chunk is None:
                break
            seq, data = chunk
            assert seq == expected_seq
            expected_seq += 1
            collected += data

        assert collected == payload
        assert transfer.sha256() == hashlib.sha256(payload).hexdigest()
        assert transfer.done
        transfer.close()

    def test_chunks_respect_the_size(self, tmp_path):
        source = tmp_path / "in.bin"
        source.write_bytes(b"x" * (CHUNK_BYTES * 2 + 5))
        transfer = OutgoingTransfer("t1", source)

        first = transfer.next_chunk()
        assert first is not None and len(first[1]) == CHUNK_BYTES
        transfer.close()

    def test_empty_file_is_immediately_done(self, tmp_path):
        source = tmp_path / "empty"
        source.write_bytes(b"")
        transfer = OutgoingTransfer("t1", source)
        assert transfer.next_chunk() is None
        assert transfer.done
        assert transfer.progress == 1.0
        transfer.close()

    def test_progress_advances(self, tmp_path):
        source = tmp_path / "in.bin"
        source.write_bytes(b"x" * (CHUNK_BYTES * 4))
        transfer = OutgoingTransfer("t1", source)
        transfer.next_chunk()
        assert 0.0 < transfer.progress < 1.0
        transfer.close()

    def test_name_comes_from_the_path(self, tmp_path):
        source = tmp_path / "photo.jpg"
        source.write_bytes(b"x")
        assert OutgoingTransfer("t1", source).name == "photo.jpg"

    def test_close_is_idempotent(self, tmp_path):
        source = tmp_path / "a"
        source.write_bytes(b"x")
        transfer = OutgoingTransfer("t1", source)
        transfer.close()
        transfer.close()
        assert transfer.next_chunk() is None


class TestRoundTrip:
    def test_outgoing_feeds_incoming(self, tmp_path):
        payload = bytes(range(256)) * 300
        source = tmp_path / "src.bin"
        source.write_bytes(payload)

        out = OutgoingTransfer("t1", source)
        incoming = IncomingTransfer("t1", tmp_path / "dst.bin", out.size)

        while True:
            chunk = out.next_chunk()
            if chunk is None:
                break
            incoming.write_chunk(*chunk)

        assert incoming.finish(out.sha256()) == tmp_path / "dst.bin"
        assert (tmp_path / "dst.bin").read_bytes() == payload
        out.close()
