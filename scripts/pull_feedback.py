"""Download tester reports from the R2 staging bucket and merge them for review.

The feedback worker (feedback-worker/) writes one object per POSTed chunk at
``reports/<YYYY-MM-DD>/<report_id>/<seq>.json``, each holding
``{"received_at", "country", "report": <the client's chunk>}``. This script
lists and downloads those objects through R2's S3-compatible API into
``<out>/raw/`` (mirroring the keys; files already there are not fetched
again), then writes, per report_id, ``<out>/<report_id>.json`` (header
fields, run counters and every row, chunks sorted by seq) and
``<out>/<report_id>.csv`` (the rows; a string cell that a spreadsheet would
take for a formula is prefixed with ``'``), and prints one line per report.
``--since`` selects reports with any chunk stored on or after that UTC day,
and then all of each one's chunks, however many days back they go (the
whole bucket is listed; only the selected reports' objects are fetched).

Every chunk is re-validated against feedback/report.schema.json. The worker
already refused anything invalid, so a failure here means the schema or the
worker changed underneath the stored data, or a mirrored file is damaged:
it is flagged in the summary and in the merged JSON (``invalid_chunks``),
not dropped. Note that Python's ``re`` lets ``$`` match before a final
newline where the worker's JavaScript regexes do not, so this check is, if
anything, the more lenient of the two (the dashboard's own guards use
``fullmatch`` for the same reason).

One report can never stop the others: a report whose merge or write raises
is reported on its own line (``MERGE-ERROR``) and the rest go through.

It only reads from R2 and writes local files; it never posts anything.

Credentials come from the environment: ``R2_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID``
and ``R2_SECRET_ACCESS_KEY`` (an R2 API token scoped read-only to the bucket;
docs/feedback-deploy.md). boto3 is not in the pixi environment, so run it
with a throwaway one::

    pixi exec -s python -s boto3 -s jsonschema -- \\
        python scripts/pull_feedback.py browser-photom-feedback --since 2026-11-01

``--offline`` skips R2 entirely and re-merges what is already under
``<out>/raw/`` (no boto3 needed; ``pixi run python scripts/pull_feedback.py
... --offline`` works).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "feedback" / "report.schema.json"
PREFIX = "reports/"
KEY_RE = re.compile(
    r"^reports/(?P<day>\d{4}-\d{2}-\d{2})/(?P<report_id>[0-9a-f]{32})/(?P<seq>\d{1,3})\.json$"
)
# Fields every chunk of one run should repeat unchanged. comment, final and
# run legitimately change from chunk to chunk; the latest chunk wins.
STABLE_FIELDS = ("obscode", "build", "qa_schema", "site_lat", "site_lon")


# -- pure helpers (tests/test_pull_feedback.py) ------------------------------

def parse_key(key):
    """``(day, report_id, seq)`` for a worker-written key, else None."""
    m = KEY_RE.match(key)
    if m is None:
        return None
    return m["day"], m["report_id"], int(m["seq"])


def keys_since(keys, since=None):
    """The report keys on or after ``since`` (``YYYY-MM-DD``), sorted.

    Keys that are not in the worker's layout are ignored.
    """
    out = []
    for key in keys:
        parsed = parse_key(key)
        if parsed is None:
            continue
        if since is not None and parsed[0] < since:
            continue
        out.append(key)
    return sorted(out)


def load_schema(path=SCHEMA_PATH):
    return json.loads(Path(path).read_text())


def make_validator(schema=None):
    """A jsonschema draft 2020-12 validator for one chunk."""
    from jsonschema import Draft202012Validator

    schema = load_schema() if schema is None else schema
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def chunk_problems(record, validator):
    """Short messages for what is wrong with one stored object; [] if fine."""
    if not isinstance(record, dict) or "report" not in record:
        return ["stored object is not {received_at, country, report}"]
    extra = set(record) - {"received_at", "country", "report"}
    problems = [f"unexpected wrapper field {k!r}" for k in sorted(extra)]
    for err in sorted(validator.iter_errors(record["report"]), key=lambda e: list(e.path)):
        where = "/" + "/".join(str(p) for p in err.absolute_path)
        problems.append(f"{where}: {err.validator} {err.message[:120]}")
    return problems


def _header_values(reports, field):
    """How many distinct *known* values ``field`` takes across the chunks.

    A header legitimately starts out not knowing things: ``qa_schema`` is 0
    until a frame reaches bandaid, and the site is null until a frame's
    header (or the form) supplies it. Unknown-then-known is not a conflict;
    two different known values are.
    """
    def unknown(value):
        # Not a set lookup: a flagged chunk can carry a list or dict here,
        # which is unhashable, and that must stay reviewable, not fatal.
        return value is None or (field == "qa_schema" and value == 0)

    return len({json.dumps(r.get(field), sort_keys=True)
                for r in reports if not unknown(r.get(field))})


def merge_report(report_id, stored, validator=None):
    """Merge one report_id's stored objects into a single review document.

    ``stored`` is a list of ``(key, record)`` with ``record`` the parsed
    object the worker wrote. Chunks are ordered by seq; if the same seq was
    stored twice (a retry that landed after UTC midnight, so under another
    day's prefix) the earliest received, readable copy is kept and the
    duplicate is noted. Header fields come from the highest seq; fields that
    should not change but did are listed in ``header_conflicts``.
    """
    by_seq = {}
    duplicates = []
    differing = []
    unreadable = []

    def rank(key_record):
        # A copy with no readable report (a truncated mirror file, say)
        # must never win over a good one; among readable copies, earliest.
        rec = key_record[1]
        return (not isinstance(rec.get("report"), dict), str(rec.get("received_at", "")))

    for key, record in stored:
        if not isinstance(record, dict):
            record = {"unparseable": f"top level is {type(record).__name__}"}
        report = record.get("report")
        seq = report.get("seq") if isinstance(report, dict) else None
        if not isinstance(seq, int):
            parsed = parse_key(key)
            seq = parsed[2] if parsed else None
        if seq is None:
            unreadable.append(key)
            continue
        if seq in by_seq:
            keep, drop = sorted([by_seq[seq], (key, record)], key=rank)
            by_seq[seq] = keep
            duplicates.append(drop[0])
            kept_report, dropped_report = keep[1].get("report"), drop[1].get("report")
            if (isinstance(kept_report, dict) and isinstance(dropped_report, dict)
                    and kept_report != dropped_report):
                differing.append(drop[0])
        else:
            by_seq[seq] = (key, record)

    seqs = sorted(by_seq)
    chunks = [by_seq[s] for s in seqs]
    reports = [rec.get("report") if isinstance(rec.get("report"), dict) else {}
               for _, rec in chunks]

    invalid = []
    if validator is not None:
        for (key, record), seq in zip(chunks, seqs):
            problems = chunk_problems(record, validator)
            if problems:
                invalid.append({"seq": seq, "key": key, "problems": problems})

    rows = []
    for seq, report in zip(seqs, reports):
        # A flagged chunk can have anything under "rows"; only a list is rows.
        chunk_rows = report.get("rows")
        for row in chunk_rows if isinstance(chunk_rows, list) else []:
            rows.append({"seq": seq, **row} if isinstance(row, dict) else {"seq": seq})

    # Header fields from the highest-seq chunk that carries a header: an
    # unreadable final chunk, or a flagged one that is just {"seq": 3,
    # "rows": []}, must not blank out what the earlier ones say.
    latest = next((r for r in reversed(reports)
                   if r.get("report_id") and isinstance(r.get("run"), dict)),
                  next((r for r in reversed(reports) if r), {}))
    conflicts = [f for f in STABLE_FIELDS if _header_values(reports, f) > 1]
    received = sorted(str(rec.get("received_at")) for _, rec in chunks
                      if rec.get("received_at"))
    countries = sorted({rec.get("country") for _, rec in chunks if rec.get("country")})
    final_seqs = [s for s, r in zip(seqs, reports) if r.get("final") is True]
    missing = sorted(set(range(seqs[-1] + 1)) - set(seqs)) if seqs else []
    inconsistencies = _inconsistencies(seqs, reports, final_seqs, missing, len(rows))

    return {
        "report_id": report_id,
        **{f: latest.get(f) for f in ("schema_version",) + STABLE_FIELDS},
        "comment": latest.get("comment"),
        "run": latest.get("run"),
        "final_received": bool(final_seqs),
        # Exactly one final, and it is the last chunk, for an honest client;
        # anything else is flagged ("final-not-last").
        "final_seqs": final_seqs,
        "seqs": seqs,
        "missing_seqs": missing,
        "duplicate_keys": sorted(duplicates),
        "differing_duplicates": sorted(differing),
        "unreadable_keys": sorted(unreadable),
        "header_conflicts": conflicts,
        "inconsistencies": inconsistencies,
        "invalid_chunks": invalid,
        "first_received_at": received[0] if received else None,
        "last_received_at": received[-1] if received else None,
        "countries": countries,
        "n_rows": len(rows),
        "rows": rows,
    }


# The outcomes the page puts on a final chunk; every other chunk says
# "running" (the kernel's headers always do; the page overrides it only on
# the final).
TERMINAL_OUTCOMES = ("done", "cancelled", "failed", "report_error", "page_closed")


def _inconsistencies(seqs, reports, final_seqs, missing, n_rows):
    """What the dashboard's own chunks always satisfy, checked across the
    whole report. The worker validates each chunk on its own, so a chunk
    that contradicts the others only shows up here: a crafted client, or a
    copy swapped in under a known report_id on the same UTC day, which the
    worker's head-then-put cannot see (it keeps the first copy of a key and
    answers 204 to the second)."""
    found = set()
    prev = None
    for report in reports:
        run = report.get("run")
        if not isinstance(run, dict):
            continue
        outcome, final = run.get("outcome"), report.get("final") is True
        if final and outcome not in TERMINAL_OUTCOMES:
            found.add("final-not-terminal")
        if not final and outcome in TERMINAL_OUTCOMES:
            found.add("terminal-before-final")
        counts = tuple(run.get(f) for f in ("n_processed", "n_skipped"))
        if all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
            if prev is not None and any(c < p for c, p in zip(counts, prev)):
                found.add("counts-decrease")
            prev = counts
    # One row per frame, and the counters are updated before each header is
    # built, so at the final chunk rows == processed + skipped. Checked only
    # for a whole report: a gap explains a shortfall by itself. So does a
    # chunk whose rows could not be read (a truncated mirror copy, or a
    # flagged chunk with something else under "rows"; the worker's schema
    # requires a list, so such a chunk is INVALID in its own right), but
    # only a shortfall: lost rows never make a surplus.
    if final_seqs and final_seqs == seqs[-1:] and not missing and reports:
        last = reports[-1].get("run")
        all_read = all(isinstance(r.get("rows"), list) for r in reports)
        if isinstance(last, dict):
            counts = tuple(last.get(f) for f in ("n_processed", "n_skipped"))
            if all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
                if n_rows > sum(counts) or (all_read and n_rows < sum(counts)):
                    found.add("rows!=counts")
    return sorted(found)


def csv_columns(rows, schema=None):
    """``seq`` then the schema's qa_row properties in schema order, then any
    columns the schema does not know (only possible in a flagged chunk)."""
    schema = load_schema() if schema is None else schema
    known = ["seq"] + list(schema["$defs"]["qa_row"]["properties"])
    extra = sorted({k for row in rows for k in row} - set(known))
    return known + extra


def _escape_char(ch):
    code = ord(ch)
    return f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}"


def csv_cell(value):
    """A value as it goes in the CSV: None blank, numbers as they are, and
    a string made safe to open or ``cat``: any character that is not
    printable (a control or escape-sequence byte, a zero-width or bidi
    format character, a tag character) is written as its ``\\uXXXX`` escape,
    and a string a spreadsheet would read as a formula (leading ``=``,
    ``+``, ``-``, ``@``, tab or CR, after any whitespace) is prefixed with
    ``'`` so it stays text. The strings come from an open endpoint; the
    schema bounds them (and refuses those characters, for a chunk the
    current worker accepted) but does not stop ``=HYPERLINK(...)`` as a
    file name or reason, and a chunk stored under an older schema may hold
    anything. The merged JSON keeps the original, ASCII-escaped."""
    if value is None:
        return ""
    if isinstance(value, str):
        if not value.isprintable():
            value = "".join(ch if ch.isprintable() else _escape_char(ch) for ch in value)
        if value.lstrip()[:1] in ("=", "+", "-", "@"):
            return "'" + value
    return value


def write_csv(rows, path, schema=None):
    columns = csv_columns(rows, schema)
    # Explicit UTF-8, and a stored string the worker's regexes let through
    # that UTF-8 cannot encode (an unpaired surrogate, before the schema
    # refused them) is written escaped rather than raising mid-pull.
    with open(path, "w", newline="", encoding="utf-8", errors="backslashreplace") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, restval="")
        # The column names too: the extra ones come from a flagged chunk's
        # row keys, so they are as untrusted as any cell.
        writer.writerow({c: csv_cell(c) for c in columns})
        for row in rows:
            writer.writerow({k: csv_cell(v) for k, v in row.items()})


def status_tally(rows):
    """``"ok=12 skipped: NoWCS=2"``, most common first (ties by name)."""
    counts = Counter(str(r.get("status")) for r in rows)
    return " ".join(f"{s}={n}" for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _seq_list(seqs, show=5):
    """``"0,1,2,3,4 +994 more"``: a client can make the gap list 999 long
    with one chunk at seq 999, and the line must stay a line."""
    text = ",".join(map(str, seqs[:show]))
    return text if len(seqs) <= show else f"{text} +{len(seqs) - show} more"


def summary_line(merged):
    flags = []
    if merged["invalid_chunks"]:
        flags.append(f"INVALID x{len(merged['invalid_chunks'])}")
    if not merged["final_received"]:
        flags.append("no-final")
    if merged["missing_seqs"]:
        flags.append(f"missing seq {_seq_list(merged['missing_seqs'])}")
    if merged["header_conflicts"]:
        flags.append(f"conflict {','.join(merged['header_conflicts'])}")
    if merged.get("differing_duplicates"):
        # Two stored copies of one seq with different bodies: the dashboard
        # never does that (its retries are byte-identical), so either the
        # tester's page and someone who learned the report_id both posted,
        # or a chunk was tampered with. Trust nothing in this report.
        flags.append("DUP-DIFFERS")
    if merged["duplicate_keys"] or merged["unreadable_keys"]:
        flags.append("dup/unreadable")
    if merged.get("final_seqs") and merged["final_seqs"] != merged["seqs"][-1:]:
        flags.append("final-not-last")
    if merged.get("inconsistencies"):
        # The chunks contradict each other in a way the dashboard never
        # produces (see _inconsistencies). Like DUP-DIFFERS: trust nothing.
        flags.append("INCONSISTENT " + ",".join(merged["inconsistencies"]))
    if merged.get("merge_error"):
        flags.append(f"MERGE-ERROR {merged['merge_error']}")
    run = merged.get("run")
    outcome = run.get("outcome") if isinstance(run, dict) else None
    comment = " ".join(str(merged.get("comment") or "").split())[:80]
    parts = [
        merged["report_id"][:8],
        str(merged.get("obscode")),
        f"rows={merged['n_rows']}",
        f"outcome={outcome}",
        status_tally(merged["rows"]) or "-",
    ]
    if flags:
        parts.append("[" + "; ".join(flags) + "]")
    return " ".join(parts) + f" | {comment!r}"


# The lifecycle rule docs/feedback-deploy.md has you create; the consent text
# promises it. Nothing in code enforces it, so the pull warns when it sees an
# object the rule should have removed.
RETENTION_DAYS = 30


def select_keys(keys, since=None):
    """The worker-layout keys to mirror: every chunk of every report that
    has at least one chunk on or after ``since`` (all of them without it).

    Whole reports, not chunks: a run can straddle UTC midnight, or be
    suspended for days between chunks, and its earlier chunks must come
    along however far back they are.
    """
    if since is None:
        return keys_since(keys)
    wanted = {parse_key(k)[1] for k in keys_since(keys, since)}
    return sorted(k for k in keys_since(keys) if parse_key(k)[1] in wanted)


def stale_keys(keys, today=None, retention_days=RETENTION_DAYS):
    """Keys whose day is more than ``retention_days`` + 1 days ago -- objects
    the bucket's lifecycle rule should have deleted by now. The key's day is
    the worker's UTC date, so ``today`` is UTC too (a local date east of
    UTC could flag an object still inside R2's day of lifecycle lag), and
    the extra day is that lag."""
    today = datetime.now(timezone.utc).date() if today is None else today
    cutoff = (today - timedelta(days=retention_days + 1)).isoformat()
    return [k for k in keys_since(keys) if parse_key(k)[0] < cutoff]


def load_raw(raw_dir, since=None, report_ids=None):
    """``{report_id: [(key, record), ...]}`` from a mirrored ``raw/`` tree.

    ``since`` selects whole reports, not chunks: a report is in if any of
    its chunks arrived on or after that day, and then all of its chunks
    are, so a run that crossed UTC midnight is not cut in two (`download`
    selects by the same rule). ``report_ids`` narrows to those reports.

    A file that is not JSON is kept as ``{"unparseable": <error>}`` so the
    merge flags it instead of the script stopping.
    """
    raw_dir = Path(raw_dir)
    grouped = {}
    for path in sorted(raw_dir.glob("reports/*/*/*.json")):
        key = path.relative_to(raw_dir).as_posix()
        if parse_key(key) is None:
            continue
        try:
            record = json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError) as exc:
            record = {"unparseable": str(exc)[:200]}
        if not isinstance(record, dict):
            record = {"unparseable": f"top level is {type(record).__name__}"}
        grouped.setdefault(parse_key(key)[1], []).append((key, record))
    if since is not None:
        grouped = {rid: items for rid, items in grouped.items()
                   if keys_since([k for k, _ in items], since)}
    if report_ids is not None:
        grouped = {rid: items for rid, items in grouped.items() if rid in set(report_ids)}
    return grouped


def merge_all(raw_dir, out_dir, since=None, schema=None, report_ids=None):
    """Merge every report under ``raw_dir`` into ``out_dir``; return the merged docs."""
    schema = load_schema() if schema is None else schema
    validator = make_validator(schema)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_docs = []
    for report_id, stored in sorted(load_raw(raw_dir, since, report_ids).items()):
        try:
            merged = merge_report(report_id, stored, validator)
            (out_dir / f"{report_id}.json").write_text(json.dumps(merged, indent=1) + "\n")
            write_csv(merged["rows"], out_dir / f"{report_id}.csv", schema)
        except Exception as exc:  # noqa: BLE001 - one report must not stop the rest
            merged = failed_merge(report_id, stored, exc)
        merged_docs.append(merged)
    merged_docs.sort(key=lambda m: m["first_received_at"] or "")
    return merged_docs


def failed_merge(report_id, stored, exc):
    """A stand-in merged doc for a report whose merge or write raised, with
    the keys `summary_line` needs, so the pull still reports every other one."""
    return {
        "report_id": report_id, "obscode": None, "comment": None, "run": None,
        "n_rows": 0, "rows": [], "seqs": [], "missing_seqs": [], "final_seqs": [],
        "final_received": False, "duplicate_keys": [], "differing_duplicates": [],
        "unreadable_keys": [],
        "header_conflicts": [], "inconsistencies": [], "invalid_chunks": [],
        "first_received_at": None, "last_received_at": None, "countries": [],
        "keys": sorted(k for k, _ in stored),
        "merge_error": f"{type(exc).__name__}: {exc}"[:200],
    }


# -- R2 (boto3, imported only here) -------------------------------------------

def r2_client(environ=None):
    environ = os.environ if environ is None else environ
    missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
               if not environ.get(v)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} (docs/feedback-deploy.md)")
    try:
        import boto3
    except ImportError:
        raise SystemExit(
            "boto3 is not installed here; run with\n"
            "  pixi exec -s python -s boto3 -s jsonschema -- python scripts/pull_feedback.py ...\n"
            "or use --offline to re-merge what is already downloaded"
        ) from None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


# More objects than this selected for one pull is not testers, it is a
# flood (a tester's 350-frame run is ~15 objects, ~70 if the tab stayed
# hidden throughout; 60 a minute from one address for a day is 86,400), and
# mirroring it would fill the disk and the merge would write two files per
# fake report. --max-objects changes
# it; 0 lifts it.
MAX_OBJECTS = 20_000


def download(client, bucket, raw_dir, since=None, report_ids=None, max_objects=MAX_OBJECTS):
    """Mirror report objects into ``raw_dir``; return (n selected, n fetched,
    n failed).

    The whole bucket is listed (at most 30 days of small objects, one
    request per thousand), then with ``since`` only the objects of reports
    that have a chunk on or after that day are fetched -- all of their
    objects, so a run that crossed UTC midnight or sat suspended for days
    comes whole, and `load_raw` selects by the same rule. ``report_ids``
    narrows the selection to those reports (the way to pull known testers'
    runs out of a flooded bucket). More than ``max_objects`` selected
    raises SystemExit before anything is fetched. Each file is written
    whole or not at all (to a ``.part`` name, then renamed), so an
    interrupted pull never leaves a truncated file that the next pull would
    skip as "already here". A get that fails (an object the lifecycle rule
    deleted between the list and the get, a 5xx boto gave up on) is
    reported on stderr and skipped; the next pull fetches it if it is still
    there. Objects older than the retention the consent text promises are
    reported on stderr too: that is the lifecycle rule missing.
    """
    raw_dir = Path(raw_dir)
    listed = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=PREFIX):
        listed.extend(obj["Key"] for obj in page.get("Contents", []))
    stale = stale_keys(listed)
    if stale:
        print(f"WARNING: {len(stale)} object(s) older than {RETENTION_DAYS} days are "
              f"still in {bucket} (oldest {stale[0]}); the bucket's lifecycle "
              "rule is missing or not working (docs/feedback-deploy.md step 3)",
              file=sys.stderr)
    keys = select_keys(listed, since)
    if report_ids is not None:
        wanted = set(report_ids)
        keys = [k for k in keys if parse_key(k)[1] in wanted]
    if max_objects and len(keys) > max_objects:
        by_day = Counter(parse_key(k)[0] for k in keys)
        days = ", ".join(f"{d}: {n}" for d, n in sorted(by_day.items()))
        raise SystemExit(
            f"{len(keys)} objects selected in {bucket}, more than --max-objects "
            f"{max_objects}; that is a flood, not testers (per day: {days}). "
            "Pull known runs with --report-id, raise --max-objects, or see "
            "docs/feedback-deploy.md section 10 for purging the bucket."
        )
    fetched = failed = 0
    for key in keys:
        dest = raw_dir / key
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 - botocore.ClientError, mostly
            print(f"could not fetch {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(body)
        os.replace(part, dest)
        fetched += 1
    return len(keys), fetched, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bucket", help="R2 bucket name, e.g. browser-photom-feedback")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only reports with a chunk stored on or after this UTC day")
    parser.add_argument("--out", type=Path, default=Path("feedback-reports"),
                        help="local directory (default: ./feedback-reports)")
    parser.add_argument("--offline", action="store_true",
                        help="do not contact R2; re-merge <out>/raw only")
    parser.add_argument("--report-id", action="append", metavar="HEX32", dest="report_ids",
                        help="only this report (repeatable); the way to pull known "
                             "testers' runs out of a flooded bucket")
    parser.add_argument("--max-objects", type=int, default=MAX_OBJECTS, metavar="N",
                        help=f"refuse to mirror more than N objects (default {MAX_OBJECTS}; "
                             "0 = no limit)")
    args = parser.parse_args(argv)
    if args.since:
        try:
            # Normalised: fromisoformat also takes 20261018 and 2026-W42-1,
            # and the keys are compared as YYYY-MM-DD strings.
            args.since = date.fromisoformat(args.since).isoformat()
        except ValueError:
            parser.error("--since must be a real YYYY-MM-DD date")
    if args.report_ids and not all(re.fullmatch(r"[0-9a-f]{32}", r) for r in args.report_ids):
        parser.error("--report-id must be 32 lowercase hex characters")

    raw_dir = args.out / "raw"
    if not args.offline:
        n_listed, n_fetched, n_failed = download(
            r2_client(), args.bucket, raw_dir, args.since, args.report_ids, args.max_objects)
        print(f"{n_listed} objects in {args.bucket}"
              f"{' since ' + args.since if args.since else ''}; fetched {n_fetched} new"
              + (f"; {n_failed} could not be fetched" if n_failed else ""))
    merged = merge_all(raw_dir, args.out, args.since, report_ids=args.report_ids)
    for doc in merged:
        print(summary_line(doc))
    n_invalid = sum(1 for m in merged if m["invalid_chunks"])
    n_errors = sum(1 for m in merged if m.get("merge_error"))
    print(f"{len(merged)} reports in {args.out}"
          + (f"; {n_invalid} with chunks that fail the schema" if n_invalid else "")
          + (f"; {n_errors} could not be merged" if n_errors else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
