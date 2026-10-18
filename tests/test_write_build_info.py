"""scripts/write_build_info.py: the build-time check of the report endpoint.

The kernel's `feedback_report.endpoint_url` quietly disables reporting on
anything odd; this is the loud half, and it must refuse everything the
kernel would, or a build ships whose reporting silently does nothing.
"""

import importlib.util
import sys
from pathlib import Path

import json
import shutil
import subprocess

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


PARSES = [
    "https://x.workers.dev/report",
    "https://x.workers.dev:8443/report",
    "https://x.workers.dev:0/report",
    "https://h:65535/report",
    "https://feedback.example-site.org/report?x=1",
    "https://1.2.3.4/report",
    "https://xn--bcher-kva.example/report",
    "http://127.0.0.1:8787/report",
    "http://localhost/report",
]
# Accepted by a letters-digits-dots-hyphens host class with `\d{1,5}` for the
# port, and thrown on by `new URL()` (seventh-round whole-PR review).
UNPARSEABLE = [
    "https://h:65536/report",
    "https://h:99999/report",
    "https://h:\u0661\u0662\u0663/report",     # Arabic-Indic digits: Python's \d
    "https://1.2.3.999/report",
    "https://example.123/report",
    "https://a.0x/report",
    "https://1.2.3.4.5/report",
    "https://xn--a/report",
    "https://xn--/report",
]


@pytest.mark.parametrize("good", PARSES)
def test_hosts_ports_and_the_report_path_pass_both_checks(wbi, good):
    assert wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: good}) == good
    assert endpoint_url(good) == good


@pytest.mark.parametrize("bad", UNPARSEABLE)
def test_what_the_url_parser_throws_on_fails_both_checks(wbi, bad):
    with pytest.raises(ValueError):
        wbi.feedback_url_from_env({wbi.FEEDBACK_URL_ENV: bad})
    assert endpoint_url(bad) == ""


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_the_lists_above_agree_with_the_browsers_url_parser():
    # The checks exist so that a value the page's `new URL()` would throw on
    # (every send failing) never builds; this pins the lists to node's WHATWG
    # parser rather than to a re-implementation of it.
    parses = ("for (const u of JSON.parse(process.argv[1])) { const p = new URL(u);"
              " if (p.pathname !== '/report') throw new Error('path ' + u); }")
    subprocess.run([NODE, "-e", parses, json.dumps(PARSES)], check=True)
    throws = ("for (const u of JSON.parse(process.argv[1])) { let threw = false;"
              " try { new URL(u); } catch (e) { threw = true; }"
              " if (!threw) throw new Error('parsed ' + u); }")
    subprocess.run([NODE, "-e", throws, json.dumps(UNPARSEABLE)], check=True)


def test_github_actions_is_only_the_literal_true(wbi):
    local = "http://localhost:8787/report"
    for not_actions in ("false", "0", ""):
        env = {wbi.FEEDBACK_URL_ENV: local, "GITHUB_ACTIONS": not_actions}
        assert wbi.feedback_url_from_env(env) == local


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
    # Copilot (PR #16, 2026-10-18): hosts `new URL()` throws on, which the
    # old `[^\s/@]+` host class let build.
    "https://?x",
    "https://[broken/report",
    "https://x.workers.dev:invalid/report",
    "https://x.workers.dev:/report",
    "https://x.workers.dev:123456/report",
    "https://x_y.workers.dev/report",
    # The worker answers 404 to any path but /report.
    "https://x.workers.dev",
    "https://x.workers.dev/",
    "https://x.workers.dev/report/",
    "https://x.workers.dev/Report",
    "https://x.workers.dev/v1/report",
    "http://localhost:8787",
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
