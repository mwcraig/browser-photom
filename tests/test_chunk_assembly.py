"""ChunkAssembler + FrameProcessor: bytes off the comm and into MEMFS.

Each chunk is appended to <tmpdir>/<name> as it arrives, so peak memory is one
chunk plus the file itself, and the image never touches the contents drive.
The MEMFS copy must be removed however the frame ends — a 350-frame run that
leaked 4 MB per frame would exhaust the kernel heap.
"""

import pytest

from photom_dashboard import ChunkAssembler, FrameProcessor, ProtocolError


@pytest.fixture
def asm(tmp_path):
    return ChunkAssembler(tmp_path)


def test_single_chunk_file_completes_immediately(asm, tmp_path):
    path = asm.add("a.fit", 0, 1, b"hello")
    assert path == str(tmp_path / "a.fit")
    assert (tmp_path / "a.fit").read_bytes() == b"hello"


def test_chunks_are_appended_in_order(asm, tmp_path):
    assert asm.add("a.fit", 0, 3, b"abc") is None
    assert asm.add("a.fit", 1, 3, b"def") is None
    path = asm.add("a.fit", 2, 3, b"ghi")
    assert path is not None
    assert (tmp_path / "a.fit").read_bytes() == b"abcdefghi"


def test_assembled_bytes_are_exact_for_binary_data(asm, tmp_path):
    blob = bytes(range(256)) * 40
    mid = len(blob) // 2
    asm.add("a.fit", 0, 2, blob[:mid])
    asm.add("a.fit", 1, 2, blob[mid:])
    assert (tmp_path / "a.fit").read_bytes() == blob


def test_memoryview_and_bytearray_payloads_are_accepted(asm, tmp_path):
    # anywidget hands binary buffers to Python as memoryview.
    asm.add("a.fit", 0, 2, memoryview(b"abc"))
    asm.add("a.fit", 1, 2, bytearray(b"def"))
    assert (tmp_path / "a.fit").read_bytes() == b"abcdef"


def test_zero_byte_file_still_completes(asm, tmp_path):
    path = asm.add("empty.fit", 0, 1, b"")
    assert path is not None
    assert (tmp_path / "empty.fit").read_bytes() == b""


def test_out_of_order_index_is_rejected(asm):
    asm.add("a.fit", 0, 3, b"abc")
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 2, 3, b"ghi")


def test_first_chunk_must_be_index_zero(asm):
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 1, 2, b"def")


def test_repeated_index_is_rejected(asm):
    asm.add("a.fit", 0, 2, b"abc")
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 0, 2, b"abc")


def test_changing_nchunks_mid_file_is_rejected(asm):
    asm.add("a.fit", 0, 3, b"abc")
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 1, 4, b"def")


def test_index_beyond_nchunks_is_rejected(asm):
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 0, 0, b"abc")


def test_a_rejected_chunk_discards_the_partial_file(asm, tmp_path):
    asm.add("a.fit", 0, 3, b"abc")
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 2, 3, b"ghi")
    assert not (tmp_path / "a.fit").exists()
    assert asm.pending == []


def test_names_are_reduced_to_basenames(asm, tmp_path):
    # The manifest name is attacker-adjacent (it comes from the browser), and
    # /tmp is the kernel's own filesystem.
    path = asm.add("../../etc/passwd", 0, 1, b"x")
    assert path == str(tmp_path / "passwd")
    assert (tmp_path / "passwd").read_bytes() == b"x"


def test_two_files_can_be_in_flight_without_mixing(asm, tmp_path):
    asm.add("a.fit", 0, 2, b"aa")
    asm.add("b.fit", 0, 2, b"bb")
    assert sorted(asm.pending) == ["a.fit", "b.fit"]
    asm.add("a.fit", 1, 2, b"AA")
    asm.add("b.fit", 1, 2, b"BB")
    assert (tmp_path / "a.fit").read_bytes() == b"aaAA"
    assert (tmp_path / "b.fit").read_bytes() == b"bbBB"
    assert asm.pending == []


def test_discard_removes_a_partial_file(asm, tmp_path):
    asm.add("a.fit", 0, 3, b"abc")
    asm.discard("a.fit")
    assert not (tmp_path / "a.fit").exists()
    assert asm.pending == []


def test_reset_clears_every_partial_file(asm, tmp_path):
    asm.add("a.fit", 0, 2, b"a")
    asm.add("b.fit", 0, 2, b"b")
    asm.reset()
    assert asm.pending == []
    assert list(tmp_path.iterdir()) == []


# --- FrameProcessor -------------------------------------------------------


def _frame(tmp_path, name="a.fit"):
    p = tmp_path / name
    p.write_bytes(b"image")
    return str(p)


def test_successful_frame_reports_ok_and_removes_the_copy(tmp_path):
    seen = []
    fp = FrameProcessor(lambda path, name: seen.append((path, name)))
    path = _frame(tmp_path)

    assert fp.run(path, "a.fit") == (True, None)
    assert seen == [(path, "a.fit")]
    assert not (tmp_path / "a.fit").exists()


def test_a_returned_string_is_a_skip_reason(tmp_path):
    fp = FrameProcessor(lambda path, name: "WCS solve failed")
    ok, reason = fp.run(_frame(tmp_path), "a.fit")
    assert (ok, reason) == (False, "WCS solve failed")
    assert not (tmp_path / "a.fit").exists()


def test_an_exception_is_a_skip_not_a_crash(tmp_path):
    def boom(path, name):
        raise RuntimeError("kaboom")

    fp = FrameProcessor(boom)
    ok, reason = fp.run(_frame(tmp_path), "a.fit")
    assert ok is False
    assert "kaboom" in reason
    assert "RuntimeError" in reason


def test_the_copy_is_removed_on_exception_too(tmp_path):
    def boom(path, name):
        raise RuntimeError("kaboom")

    path = _frame(tmp_path)
    FrameProcessor(boom).run(path, "a.fit")
    assert not (tmp_path / "a.fit").exists()


def test_a_frame_that_deleted_its_own_copy_is_not_an_error(tmp_path):
    import os

    fp = FrameProcessor(lambda path, name: os.remove(path))
    assert fp.run(_frame(tmp_path), "a.fit") == (True, None)


def test_a_completed_file_cannot_be_resent_until_reset(asm):
    # A chunk stream restarting at index 0 for a finished name would
    # re-register as brand new and double-count the frame upstream.
    asm.add("a.fit", 0, 1, b"aaaa")
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 0, 1, b"aaaa")
    asm.reset()
    path = asm.add("a.fit", 0, 1, b"bbbb")
    with open(path, "rb") as f:
        assert f.read() == b"bbbb"


def test_mark_completed_refuses_chunks_for_that_name(asm):
    asm.mark_completed("a.fit")
    with pytest.raises(ProtocolError):
        asm.add("a.fit", 0, 1, b"aaaa")


def test_a_non_contiguous_memoryview_payload_is_written_correctly(asm):
    # write() needs a contiguous buffer; the assembler must fall back to a
    # copy for the (never observed, but legal) non-contiguous case.
    path = asm.add("a.fit", 0, 1, memoryview(b"abcdef")[::2])
    with open(path, "rb") as f:
        assert f.read() == b"ace"
