"""Write content/build_info.py: the git SHAs a build of the dashboard embeds.

The starlist schema has no field yet for the software that produced a
starlist, so until it does the dashboard stamps the two SHAs that matter
-- the pinned bandaid commit and this repo's own commit -- into every
``.star`` file name and into the zip name (`photom_dashboard.provenance_tag`).
Neither SHA is knowable from inside the kernel: bandaid has no tags, so its
hatch-vcs version string is not a reliable carrier, and browser-photom is
not a package at all. So the ``build-info`` pixi task runs this on the host,
after ``fetch-bandaid`` and before ``build``/``build-dash``, and the
generated module is copied into the kernel with the rest of content/. It is
gitignored: it describes a build, not the source.

Usage: ``python scripts/write_build_info.py [--repo DIR] [--bandaid DIR]
[--out FILE]``. A dirty checkout (tracked changes; untracked files ignored,
since the fetched clones live inside the repo) gets a ``-dirty`` suffix so a
starlist from a locally patched tree cannot pass for the committed one.

The same module carries one build-time setting: ``FEEDBACK_URL``, the
endpoint the dashboard's opt-in tester reports are POSTed to
(`feedback_report`, `docs/dashboard.md` §11). It comes from the
``BROWSER_PHOTOM_FEEDBACK_URL`` environment variable -- the Pages workflow
sets it from the ``FEEDBACK_URL`` repository variable -- and is written as
``""`` when unset, which disables reporting in that build. A value that is
set but is not an https URL without credentials (or an http one on
localhost, for a ``wrangler dev`` worker) fails the build rather than
silently shipping a dashboard whose reporting quietly does nothing: the page
is served over https, so any other http endpoint would be blocked as mixed
content and every send would fail, and fetch and sendBeacon throw on a URL
with userinfo.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FEEDBACK_URL_ENV = "BROWSER_PHOTOM_FEEDBACK_URL"


def git_sha(repo, *, short=7):
    """``<short sha>``, with ``-dirty`` appended when tracked files differ.

    Raises ``subprocess.CalledProcessError`` when ``repo`` is not a git
    checkout: a build that cannot say what it built should fail, not stamp
    the starlists with a placeholder.
    """
    repo = Path(repo)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"--short={short}", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True,
    ).stdout
    return f"{sha}-dirty" if status.strip() else sha


# https anywhere, or plain http only on this machine (a `wrangler dev` worker
# behind a local `build-dash` page). feedback_report.endpoint_url is the
# kernel's copy of the same rule.
# A copy of feedback_report.usable_endpoint (this script runs outside the
# kernel environment); tests/test_write_build_info.py keeps the two in step.
# The same control class as feedback_report._CONTROL_RE: C0, DEL and C1
# (U+0080-009F; `\S` lets most of C1 through, and a UTF-8 terminal reads
# U+009B as an escape introducer).
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# What `new URL()` in the page would accept and the worker would answer:
# https (or http on localhost / 127.0.0.1) with no userinfo, a host of
# letters, digits, dots and hyphens, an optional port, and the worker's one
# path, /report. `[0-9]` rather than `\d`, which in Python takes every
# Unicode digit. Each of these built, and the kernel enabled reporting on
# it, before the checks below: `https://?x` and `https://h:bad/report` (the
# host class), `https://h:65536/report` (port range), `https://h:25/report`
# (parses, but the Fetch standard blocks the port), `https://example.123/report`
# and `https://1.2.3.999/report` (a numeric last label is parsed as IPv4
# and fails unless it is a dotted quad), `https://xn--a/report` (invalid
# punycode; Python's idna codec refuses what the URL parser refuses on
# every case tried), and a URL without /report, which the worker answers
# 404.
_ENDPOINT_RE = re.compile(
    r"(?:https://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)|http://(?:localhost|127\.0\.0\.1))"
    r"(?::([0-9]{1,5}))?/report(?:\?\S*)?"
)
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4_RE = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")

# The Fetch standard's bad port list (https://fetch.spec.whatwg.org/#port-blocking),
# Copilot's ninth pass: `new URL()` parses `https://h:25/report`, but every
# fetch and sendBeacon to a port on this list is a network error before any
# connection, keepalive retry included, so a build with one has reporting
# that never sends. Port 0 is on the standard's list and blocked by Chromium
# and Firefox, though node's fetch (undici) lets it through; the parity test
# in tests/test_write_build_info.py allows for exactly that one.
_BAD_PORTS = frozenset((
    0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
    87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
    139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723,
    2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
    6679, 6697, 10080,
))


def usable_endpoint(text):
    """True when ``text`` is an endpoint URL every send could reach."""
    match = _ENDPOINT_RE.fullmatch(text)
    if match is None or CONTROL_RE.search(text):
        return False
    host, port = match.group(1), match.group(2)
    if port is not None and (int(port) > 65535 or int(port) in _BAD_PORTS):
        return False
    if host is not None:
        if _NUMERIC_LABEL_RE.fullmatch(host.rsplit(".", 1)[-1]) and not _IPV4_RE.fullmatch(host):
            return False
        try:
            host.encode("ascii").decode("idna")
        except UnicodeError:
            return False
    return True


def feedback_url_from_env(environ=None):
    """``BROWSER_PHOTOM_FEEDBACK_URL``, stripped; ``""`` when unset or blank.

    Raises ``ValueError`` for a value that is set but is not an https URL
    without whitespace, control characters or userinfo -- or, in a GitHub
    Actions build (``GITHUB_ACTIONS`` is ``true``), that is not https at all: the
    localhost exception is for a developer's own machine, and a published
    page must never post to its visitors' localhost. The kernel re-checks
    with `feedback_report.endpoint_url` and disables reporting on anything
    odd; this is the loud half of that, at build time, and refuses
    everything the kernel would.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(FEEDBACK_URL_ENV, "").strip()
    if not value:
        return ""
    if not usable_endpoint(value):
        raise ValueError(
            f"{FEEDBACK_URL_ENV}={value!r} is not an https URL (without "
            "credentials) ending in /report; unset it to build without tester reporting"
        )
    # GitHub sets GITHUB_ACTIONS to the string "true" in every Actions job;
    # a developer's GITHUB_ACTIONS=false or 0 is not an Actions build.
    if environ.get("GITHUB_ACTIONS") == "true" and not value.startswith("https://"):
        raise ValueError(
            f"{FEEDBACK_URL_ENV}={value!r}: a published build must use https"
        )
    return value


def render(bandaid_sha, browser_photom_sha, feedback_url=""):
    return (
        "# Generated by scripts/write_build_info.py (the `build-info` pixi task).\n"
        "# Describes one build; gitignored. Do not edit by hand.\n"
        f"BANDAID_SHA = {bandaid_sha!r}\n"
        f"BROWSER_PHOTOM_SHA = {browser_photom_sha!r}\n"
        f"# Tester-report endpoint, from ${FEEDBACK_URL_ENV}; empty = disabled.\n"
        f"FEEDBACK_URL = {feedback_url!r}\n"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=REPO_ROOT, type=Path,
                        help="browser-photom checkout (default: this repo)")
    parser.add_argument("--bandaid", default=REPO_ROOT / "bandaid-src", type=Path,
                        help="bandaid checkout (default: bandaid-src/)")
    parser.add_argument("--out", default=REPO_ROOT / "content" / "build_info.py",
                        type=Path, help="module to write")
    args = parser.parse_args(argv)

    try:
        feedback_url = feedback_url_from_env()
    except ValueError as exc:
        parser.error(str(exc))
    bandaid_sha = git_sha(args.bandaid)
    browser_photom_sha = git_sha(args.repo)
    args.out.write_text(render(bandaid_sha, browser_photom_sha, feedback_url))
    print(f"{args.out}: bandaid {bandaid_sha}, browser-photom {browser_photom_sha}, "
          f"tester reports {'-> ' + feedback_url if feedback_url else 'disabled'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
