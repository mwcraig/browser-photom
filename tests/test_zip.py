"""build_results_zip(): the dashboard's only output path.

The .star starlists still land in results/ on the contents drive (only the
images bypass it), so the download button has to collect them back off the
drive and hand the bytes to JS over the comm.
"""

import io
import zipfile

import pytest

from photom_dashboard import build_results_zip


def _write(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_round_trips_star_file_contents(tmp_path):
    _write(tmp_path, "a.star", "#AAVSO\nrow a\n")
    _write(tmp_path, "b.star", "#AAVSO\nrow b\n")

    with zipfile.ZipFile(io.BytesIO(build_results_zip(tmp_path))) as zf:
        assert zf.namelist() == ["a.star", "b.star"]
        assert zf.read("a.star").decode() == "#AAVSO\nrow a\n"
        assert zf.read("b.star").decode() == "#AAVSO\nrow b\n"


def test_only_star_files_are_included(tmp_path):
    _write(tmp_path, "a.star", "keep")
    _write(tmp_path, "notes.txt", "drop")
    _write(tmp_path, "frame.fit", "drop")
    _write(tmp_path, "a.star.bak", "drop")

    with zipfile.ZipFile(io.BytesIO(build_results_zip(tmp_path))) as zf:
        assert zf.namelist() == ["a.star"]


def test_entries_are_basenames_not_paths(tmp_path):
    _write(tmp_path, "sub/nested.star", "x")

    with zipfile.ZipFile(io.BytesIO(build_results_zip(tmp_path))) as zf:
        assert zf.namelist() == ["nested.star"]


def test_order_is_sorted_and_bytes_are_deterministic(tmp_path):
    # Written out of order on purpose.
    for name in ("c.star", "a.star", "b.star"):
        _write(tmp_path, name, name)

    first = build_results_zip(tmp_path)
    second = build_results_zip(tmp_path)

    with zipfile.ZipFile(io.BytesIO(first)) as zf:
        assert zf.namelist() == ["a.star", "b.star", "c.star"]
    # Same inputs -> same bytes: mtimes must not leak into the archive.
    assert first == second


def test_empty_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        build_results_zip(tmp_path)


def test_directory_with_no_star_files_raises(tmp_path):
    _write(tmp_path, "notes.txt", "x")
    with pytest.raises(ValueError):
        build_results_zip(tmp_path)


def test_missing_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        build_results_zip(tmp_path / "nope")
