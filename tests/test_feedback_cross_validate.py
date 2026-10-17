"""The kernel's chunks through the *worker's* validator, not Python's.

`test_feedback_report.py` validates every chunk `feedback_report` builds with
python-jsonschema. That is almost the worker's check, but not quite: the
worker runs `@cfworker/json-schema` with JavaScript regexes, and the two
engines differ at the edges (Python's ``$`` matches before a final newline;
the two read ``\\u`` escapes and surrogates their own ways). A chunk that
passes here but not there costs a tester 25 rows, and nothing in Python can
see it -- so this test hands real producer output to the real JS validator,
run under node from the worker's own node_modules.

Skipped when node or the worker's dependencies are not installed (``npm
install`` in feedback-worker/), which is the case in a fresh checkout.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from feedback_report import (
    COMMENT_MAX_CHARS,
    build_chunk,
    build_header,
    clean_row,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = REPO_ROOT / "feedback-worker"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (WORKER_DIR / "node_modules" / "@cfworker" / "json-schema").exists(),
    reason="needs node and `npm install` in feedback-worker/",
)

VALIDATE_JS = """
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
// argv: node, this script, worker dir, schema path, chunks path.
const require = createRequire(process.argv[2] + '/package.json');
const { Validator } = require('@cfworker/json-schema');
const schema = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const validator = new Validator(schema, '2020-12', true);
const chunks = JSON.parse(readFileSync(process.argv[4], 'utf8'));
const out = chunks.map(([name, chunk]) => {
  const r = validator.validate(chunk);
  return [name, r.valid, r.valid ? null : r.errors.map((e) => e.instanceLocation + ' ' + e.keyword)];
});
process.stdout.write(JSON.stringify(out));
"""


def _header(**kw):
    args = dict(report_id="0123456789abcdef0123456789abcdef", obscode="lgeb",
                site_lat=39.7, site_lon=-105.2, build="bandaid-d6ba44a.browser-photom-4840cff",
                qa_schema=1, comment="", n_total=3, n_processed=1, n_skipped=1, median_s=3.4)
    args.update(kw)
    return build_header(**args)


def _hostile_chunks():
    """(name, chunk) pairs built by the real producer from hostile input."""
    qa = {"file": "x", "status": "ok", "n_detected": 412, "fwhm": float("nan"),
          "wcs_solved": True, "dropped_filters": "TB", "plane_fallback": False, "g_cut": float("inf")}
    rows = {
        "trailing newline in status/filters/fallback": clean_row(
            {**qa, "status": "error: Foo\n", "dropped_filters": "V;B\n", "plane_fallback": "plane\n"},
            name="a.fit"),
        "newline-only status": clean_row({**qa, "status": "\n"}, name="a.fit"),
        "surrogates and format chars in name and reason": clean_row(
            qa, name="﻿L\ud800ight​.fit", reason="x‮y\udfff  z"),
        "URL and traceback in reason": clean_row(
            qa, name="a.fit", reason="HTTPError: 503 for https://gea.esac.esa.int/x\nTraceback..."),
        "wide characters everywhere": clean_row(
            qa, name="😀" * 300 + ".fit", reason="中文" * 300, elapsed_s=1e13),
        "no QA record": clean_row(None, name="b.fit", reason="empty file (0 bytes)"),
        "bool-ish and huge counts": clean_row(
            {**qa, "n_detected": True, "n_good_stars": 10**9, "plane_n_used": 3.0}, name="a.fit"),
    }
    comments = {
        "comment with trailing newline": "fine\n",
        "comment with surrogate": "broken \ud83d emoji",
        "comment hiding www. with zero-width": "see w​ww.example.com",
        "comment at the limit, 4-byte": "😀" * COMMENT_MAX_CHARS,
        "comment with CRLF and tabs": "a\r\nb\tc",
    }
    chunks = [(name, build_chunk(_header(), [row])) for name, row in rows.items()]
    chunks += [(name, build_chunk(_header(comment=text), [])) for name, text in comments.items()]
    chunks.append(("build tag with a newline", build_chunk(_header(build="tag\n"), [])))
    chunks.append(("obscode with surrounding space", build_chunk(_header(obscode=" lgeb "), [])))
    chunks.append(("site on the 0..360 convention", build_chunk(_header(site_lon=359.6), [])))
    chunks.append(("final page_closed with hidden time",
                   build_chunk(_header(), [], seq=7, final=True, outcome="page_closed",
                               hidden_ms=1_000_000_000)))
    chunks.append(("report_error outcome",
                   build_chunk(_header(), [], seq=1, final=True, outcome="report_error")))
    return chunks


def test_every_chunk_the_kernel_builds_passes_the_workers_validator(tmp_path):
    chunks = _hostile_chunks()
    script = tmp_path / "validate.mjs"
    script.write_text(VALIDATE_JS)
    payload = tmp_path / "chunks.json"
    # ensure_ascii=False, as chunk_json does: the wire bytes are UTF-8 text.
    payload.write_text(json.dumps(chunks, allow_nan=False, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(script), str(WORKER_DIR), str(REPO_ROOT / "feedback" / "report.schema.json"),
         str(payload)],
        capture_output=True, text=True, check=True, cwd=tmp_path,
    )
    results = json.loads(proc.stdout)
    assert len(results) == len(chunks)
    failures = [(name, errors) for name, ok, errors in results if not ok]
    assert failures == [], failures
