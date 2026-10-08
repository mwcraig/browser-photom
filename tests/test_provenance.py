"""The software stamp in every output name.

The starlist schema cannot yet record what produced a starlist, so the
bandaid and browser-photom SHAs ride in the `.star` and zip file names
instead. The SHAs come from a generated `content/build_info.py`
(scripts/write_build_info.py, the `build-info` pixi task); without one the
names say "unknown" rather than the kernel failing in its one output path.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from photom_dashboard import (
    PhotometryDashboard,
    provenance_tag,
    software_versions,
    starlist_name,
    zip_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_SCRIPT = REPO_ROOT / "scripts" / "write_build_info.py"


# -- the tag and the names -------------------------------------------------


def test_tag_names_both_shas():
    tag = provenance_tag({"bandaid": "33bebf5", "browser_photom": "e2a4c9c"})
    assert tag == "bandaid-33bebf5.browser-photom-e2a4c9c"


def test_star_and_zip_names_carry_the_tag():
    tag = "bandaid-33bebf5.browser-photom-e2a4c9c"
    assert starlist_name("Light_EY_UMa_10.0s_IRCUT_20250305-040530", tag) == (
        "Light_EY_UMa_10.0s_IRCUT_20250305-040530.bandaid-33bebf5.browser-photom-e2a4c9c.star"
    )
    assert zip_name("ey_uma", tag) == "ey_uma-starlists.bandaid-33bebf5.browser-photom-e2a4c9c.zip"


def test_stamped_star_name_still_matches_the_star_glob():
    # build_results_zip, _push_runs and _clear_results all find starlists by
    # `*.star`; the stamp must not change that.
    assert Path(starlist_name("a", "bandaid-x.browser-photom-y")).match("*.star")


# -- where the SHAs come from ----------------------------------------------


@pytest.fixture
def no_build_info(monkeypatch):
    """Guarantee `import build_info` fails, whatever the host has generated:
    a None entry in sys.modules makes the import raise ImportError."""
    monkeypatch.setitem(sys.modules, "build_info", None)


def test_versions_are_unknown_without_build_info(no_build_info):
    assert software_versions() == {"bandaid": "unknown", "browser_photom": "unknown"}
    assert provenance_tag() == "bandaid-unknown.browser-photom-unknown"


def test_versions_come_from_build_info(monkeypatch, tmp_path):
    (tmp_path / "build_info.py").write_text(
        'BANDAID_SHA = "33bebf5"\nBROWSER_PHOTOM_SHA = "e2a4c9c-dirty"\n'
    )
    monkeypatch.delitem(sys.modules, "build_info", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert software_versions() == {"bandaid": "33bebf5", "browser_photom": "e2a4c9c-dirty"}
    assert provenance_tag() == "bandaid-33bebf5.browser-photom-e2a4c9c-dirty"


def test_dashboard_defaults_to_the_build_tag(monkeypatch, tmp_path):
    (tmp_path / "build_info.py").write_text(
        'BANDAID_SHA = "33bebf5"\nBROWSER_PHOTOM_SHA = "e2a4c9c"\n'
    )
    monkeypatch.delitem(sys.modules, "build_info", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))

    class Widget:
        def send(self, *a, **k):
            pass

        def on_msg(self, h):
            pass

    dash = PhotometryDashboard(
        lambda p, n: None, Widget(), Widget(),
        results_dir=tmp_path / "results", tmpdir=tmp_path / "tmp",
    )
    assert dash.provenance == "bandaid-33bebf5.browser-photom-e2a4c9c"


# -- the generator ---------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(path):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short=7", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_write_build_info_records_both_checkouts(tmp_path):
    repo_sha = _init_repo(tmp_path / "repo")
    bandaid_sha = _init_repo(tmp_path / "bandaid")
    out = tmp_path / "build_info.py"

    subprocess.run(
        [sys.executable, str(WRITE_SCRIPT), "--repo", str(tmp_path / "repo"),
         "--bandaid", str(tmp_path / "bandaid"), "--out", str(out)],
        check=True, capture_output=True,
    )
    ns = {}
    exec(out.read_text(), ns)
    assert ns["BANDAID_SHA"] == bandaid_sha
    assert ns["BROWSER_PHOTOM_SHA"] == repo_sha


def test_write_build_info_marks_a_dirty_tree_but_ignores_untracked(tmp_path):
    repo = tmp_path / "repo"
    repo_sha = _init_repo(repo)
    bandaid_sha = _init_repo(tmp_path / "bandaid")
    (repo / "tracked.txt").write_text("v1\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add")
    repo_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=7", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    # Untracked files alone (the fetched clones live inside the repo and are
    # untracked/ignored) do not make a build dirty ...
    (repo / "untracked.txt").write_text("x\n")
    out = tmp_path / "build_info.py"
    subprocess.run(
        [sys.executable, str(WRITE_SCRIPT), "--repo", str(repo),
         "--bandaid", str(tmp_path / "bandaid"), "--out", str(out)],
        check=True, capture_output=True,
    )
    ns = {}
    exec(out.read_text(), ns)
    assert ns["BROWSER_PHOTOM_SHA"] == repo_sha
    assert ns["BANDAID_SHA"] == bandaid_sha

    # ... but a modified tracked file does.
    (repo / "tracked.txt").write_text("v2\n")
    subprocess.run(
        [sys.executable, str(WRITE_SCRIPT), "--repo", str(repo),
         "--bandaid", str(tmp_path / "bandaid"), "--out", str(out)],
        check=True, capture_output=True,
    )
    ns = {}
    exec(out.read_text(), ns)
    assert ns["BROWSER_PHOTOM_SHA"] == f"{repo_sha}-dirty"


def test_write_build_info_fails_loudly_outside_a_checkout(tmp_path):
    (tmp_path / "notgit").mkdir()
    _init_repo(tmp_path / "bandaid")
    result = subprocess.run(
        [sys.executable, str(WRITE_SCRIPT), "--repo", str(tmp_path / "notgit"),
         "--bandaid", str(tmp_path / "bandaid"), "--out", str(tmp_path / "out.py")],
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "out.py").exists()
