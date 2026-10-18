"""scripts/write_build_info.py: the build-time check of the report endpoint.

The kernel's `feedback_report.endpoint_url` quietly disables reporting on
anything odd; this is the loud half, and it must refuse everything the
kernel would, or a build ships whose reporting silently does nothing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "content"))

from feedback_report import endpoint_url  # noqa: E402


@pytest.fixture(scope="module")
def wbi():
    spec = importlib.util.spec_from_file_location(
        "write_build_info", REPO_ROOT / "scripts" / "write_build_info.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD = "https://browser-photom-feedback.mwcraig2.workers.dev/report"


def test_unset_or_blank_disables_reporting(wbi):
    assert wbi.feedback_url_from_env({}) == ""
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: "   "}) == ""


def test_a_good_url_passes_both_checks(wbi):
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: f" {GOOD} "}) == GOOD
    assert endpoint_url(GOOD) == GOOD


@pytest.mark.parametrize("bad", [
    "http://browser-photom-feedback.mwcraig2.workers.dev/report",   # mixed content
    "https://user:pw@x.workers.dev/report",                          # userinfo
    "https://x.workers.dev/re port",                                 # whitespace
    "https://x.workers.dev/re\x7fport",                              # control character
    "https://x.workers.dev/re\x9bport",                              # C1 control (CSI)
    "https://x.workers.dev/re\x85port",                              # C1 control (NEL)
    "https://x.workers.dev/re\nport",
    "ftp://x.workers.dev/report",
    "x.workers.dev/report",
])
def test_everything_the_kernel_would_refuse_fails_the_build(wbi, bad):
    with pytest.raises(ValueError):
        wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: bad})
    assert endpoint_url(bad) == ""


@pytest.mark.parametrize("code", list(range(0x20)) + list(range(0x7F, 0xA0)))
def test_every_control_character_is_refused_by_both_checks(wbi, code):
    # Copilot (PR #16, 2026-10-18): the kernel's class grew to C1 in the fifth
    # round and this check did not follow, so a URL with U+009B built fine and
    # was silently disabled by the kernel. Whole class, both sides.
    url = f"https://x.workers.dev/re{chr(code)}port"
    with pytest.raises(ValueError):
        wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: url})
    assert endpoint_url(url) == ""


def test_localhost_http_is_for_a_developers_machine_only(wbi):
    local = "http://localhost:8787/report"
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: local}) == local
    assert endpoint_url(local) == local
    with pytest.raises(ValueError, match="https"):
        wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: local, "GITHUB_ACTIONS": "true"})
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: GOOD, "GITHUB_ACTIONS": "true"}) == GOOD
