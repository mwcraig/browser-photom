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
and then all of each one's chunks.

Every chunk is re-validated against feedback/report.schema.json. The worker
already refused anything invalid, so a failure here means the schema or the
worker changed underneath the stored data: it is flagged in the summary and
in the merged JSON (``invalid_chunks``), not dropped. Note that Python's
``re`` lets ``$`` match before a final newline where the worker's JavaScript
regexes do not, so this check is, if anything, the more lenient of the two.

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
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "feedback" / "report.schema.json"
PREFIX = "reports/"
KEY_RE = re.compile(
    r"^reports/(?P<day>\d{4}-\d{2}-\d{2})/(?P<report_id>[0-9a-f]{32})/(?P<seq>\d{1,3})\.json$"
)
# Fields every chunk of one run should repeat unchanged. comment, final and
# run legitimately change from chunk to chunk; the latest chunk wins.
STABLE_FIELDS = ("obscode", "build", "qa_schema", "user_agent", "site_lat", "site_lon")


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
    unknown = {None, 0} if field == "qa_schema" else {None}
    return len({json.dumps(r.get(field), sort_keys=True)
                for r in reports if r.get(field) not in unknown})


def merge_report(report_id, stored, validator=None):
    """Merge one report_id's stored objects into a single review document.

    ``stored`` is a list of ``(key, record)`` with ``record`` the parsed
    object the worker wrote. Chunks are ordered by seq; if the same seq was
    stored twice (a retry that landed after UTC midnight, so under another
    day's prefix) the earliest received copy is kept and the duplicate is
    noted. Header fields come from the highest seq; fields that should not
    change but did are listed in ``header_conflicts``.
    """
    by_seq = {}
    duplicates = []
    unreadable = []
    for key, record in stored:
        report = record.get("report") if isinstance(record, dict) else None
        seq = report.get("seq") if isinstance(report, dict) else None
        if not isinstance(seq, int):
            parsed = parse_key(key)
            seq = parsed[2] if parsed else None
        if seq is None:
            unreadable.append(key)
            continue
        if seq in by_seq:
            keep, drop = sorted(
                [by_seq[seq], (key, record)],
                key=lambda kr: str(kr[1].get("received_at", "")),
            )
            by_seq[seq] = keep
            duplicates.append(drop[0])
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

    latest = reports[-1] if reports else {}
    conflicts = [f for f in STABLE_FIELDS if _header_values(reports, f) > 1]
    received = sorted(str(rec.get("received_at")) for _, rec in chunks
                      if rec.get("received_at"))
    countries = sorted({rec.get("country") for _, rec in chunks if rec.get("country")})
    final_seqs = [s for s, r in zip(seqs, reports) if r.get("final") is True]
    missing = sorted(set(range(seqs[-1] + 1)) - set(seqs)) if seqs else []

    return {
        "report_id": report_id,
        **{f: latest.get(f) for f in ("schema_version",) + STABLE_FIELDS},
        "comment": latest.get("comment"),
        "run": latest.get("run"),
        "final_received": bool(final_seqs),
        "seqs": seqs,
        "missing_seqs": missing,
        "duplicate_keys": sorted(duplicates),
        "unreadable_keys": sorted(unreadable),
        "header_conflicts": conflicts,
        "invalid_chunks": invalid,
        "first_received_at": received[0] if received else None,
        "last_received_at": received[-1] if received else None,
        "countries": countries,
        "n_rows": len(rows),
        "rows": rows,
    }


def csv_columns(rows, schema=None):
    """``seq`` then the schema's qa_row properties in schema order, then any
    columns the schema does not know (only possible in a flagged chunk)."""
    schema = load_schema() if schema is None else schema
    known = ["seq"] + list(schema["$defs"]["qa_row"]["properties"])
    extra = sorted({k for row in rows for k in row} - set(known))
    return known + extra


def csv_cell(value):
    """A value as it goes in the CSV: None blank, numbers as they are, and a
    string that a spreadsheet would read as a formula (leading ``=``, ``+``,
    ``-``, ``@``, tab or CR, after any whitespace) prefixed with ``'`` so it
    stays text. The strings come from an open endpoint; the schema bounds
    them but does not stop ``=HYPERLINK(...)`` as a file name or reason. The
    merged JSON keeps the original."""
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip()[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def write_csv(rows, path, schema=None):
    columns = csv_columns(rows, schema)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_cell(v) for k, v in row.items()})


def status_tally(rows):
    """``"ok=12 skipped: NoWCS=2"``, most common first (ties by name)."""
    counts = Counter(str(r.get("status")) for r in rows)
    return " ".join(f"{s}={n}" for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def summary_line(merged):
    flags = []
    if merged["invalid_chunks"]:
        flags.append(f"INVALID x{len(merged['invalid_chunks'])}")
    if not merged["final_received"]:
        flags.append("no-final")
    if merged["missing_seqs"]:
        flags.append(f"missing seq {','.join(map(str, merged['missing_seqs']))}")
    if merged["header_conflicts"]:
        flags.append(f"conflict {','.join(merged['header_conflicts'])}")
    if merged["duplicate_keys"] or merged["unreadable_keys"]:
        flags.append("dup/unreadable")
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


def day_before(day):
    """``YYYY-MM-DD`` of the day before ``day``."""
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def load_raw(raw_dir, since=None):
    """``{report_id: [(key, record), ...]}`` from a mirrored ``raw/`` tree.

    ``since`` selects whole reports, not chunks: a report is in if any of
    its chunks arrived on or after that day, and then all of its chunks
    are, so a run that crossed UTC midnight is not cut in two (`download`
    mirrors from the day before for the same reason).

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
    return grouped


def merge_all(raw_dir, out_dir, since=None, schema=None):
    """Merge every report under ``raw_dir`` into ``out_dir``; return the merged docs."""
    schema = load_schema() if schema is None else schema
    validator = make_validator(schema)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_docs = []
    for report_id, stored in sorted(load_raw(raw_dir, since).items()):
        merged = merge_report(report_id, stored, validator)
        (out_dir / f"{report_id}.json").write_text(json.dumps(merged, indent=1) + "\n")
        write_csv(merged["rows"], out_dir / f"{report_id}.csv", schema)
        merged_docs.append(merged)
    merged_docs.sort(key=lambda m: m["first_received_at"] or "")
    return merged_docs


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


def download(client, bucket, raw_dir, since=None):
    """Mirror report objects into ``raw_dir``; return (n listed, n fetched).

    With ``since``, objects from the day before are mirrored too: a run that
    crossed UTC midnight has its first chunks under the earlier day, and
    `load_raw` decides by report, not by chunk.
    """
    raw_dir = Path(raw_dir)
    listed = []
    kwargs = {"Bucket": bucket, "Prefix": PREFIX}
    first_day = day_before(since) if since else None
    if first_day:
        # Keys sort by day, so start listing just before that day's prefix.
        kwargs["StartAfter"] = f"{PREFIX}{first_day}"
    for page in client.get_paginator("list_objects_v2").paginate(**kwargs):
        listed.extend(obj["Key"] for obj in page.get("Contents", []))
    keys = keys_since(listed, first_day)
    fetched = 0
    for key in keys:
        dest = raw_dir / key
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        dest.write_bytes(body)
        fetched += 1
    return len(keys), fetched


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bucket", help="R2 bucket name, e.g. browser-photom-feedback")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only reports with a chunk stored on or after this UTC day")
    parser.add_argument("--out", type=Path, default=Path("feedback-reports"),
                        help="local directory (default: ./feedback-reports)")
    parser.add_argument("--offline", action="store_true",
                        help="do not contact R2; re-merge <out>/raw only")
    args = parser.parse_args(argv)
    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        parser.error("--since must be YYYY-MM-DD")

    raw_dir = args.out / "raw"
    if not args.offline:
        n_listed, n_fetched = download(r2_client(), args.bucket, raw_dir, args.since)
        print(f"{n_listed} objects in {args.bucket}"
              f"{' since ' + args.since if args.since else ''}; fetched {n_fetched} new")
    merged = merge_all(raw_dir, args.out, args.since)
    for doc in merged:
        print(summary_line(doc))
    n_invalid = sum(1 for m in merged if m["invalid_chunks"])
    print(f"{len(merged)} reports in {args.out}"
          + (f"; {n_invalid} with chunks that fail the schema" if n_invalid else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
