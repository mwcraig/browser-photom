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
    "https://x.workers.dev/re\nport",
    "ftp://x.workers.dev/report",
    "x.workers.dev/report",
])
def test_everything_the_kernel_would_refuse_fails_the_build(wbi, bad):
    with pytest.raises(ValueError):
        wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: bad})
    assert endpoint_url(bad) == ""


def test_localhost_http_is_for_a_developers_machine_only(wbi):
    local = "http://localhost:8787/report"
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: local}) == local
    assert endpoint_url(local) == local
    with pytest.raises(ValueError, match="https"):
        wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: local, "GITHUB_ACTIONS": "true"})
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: GOOD, "GITHUB_ACTIONS": "true"}) == GOOD
