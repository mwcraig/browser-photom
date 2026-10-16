"""scripts/pull_feedback.py: merging tester-report chunks for review.

Only the pure merge/validate/summary half is tested; the R2 half needs
boto3 and credentials and is a thin list-and-get loop. Nothing here touches
the network, and boto3 must not be needed to import the script.
"""

import ast
import csv
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "pull_feedback.py"

RID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def pf():
    spec = importlib.util.spec_from_file_location("pull_feedback", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator(pf):
    return pf.make_validator()


def chunk(seq, rows, *, final=False, comment="", **over):
    report = {
        "schema_version": 1, "report_id": RID, "seq": seq, "final": final,
        "build": "bandaid-d6ba44a.browser-photom-1854672", "qa_schema": 3,
        "obscode": "CMW", "site_lat": 47, "site_lon": -96, "comment": comment,
        "run": {"n_total": 3, "n_processed": seq + 1, "n_skipped": 0,
                "outcome": "done" if final else "running"},
        "rows": rows,
    }
    report.update(over)
    return report


def stored(report, received_at="2026-11-05T03:00:00.000Z", country="US"):
    return {"received_at": received_at, "country": country, "report": report}


def key(seq, day="2026-11-05", rid=RID):
    return f"reports/{day}/{rid}/{seq}.json"


ROW_OK = {"file": "a.fits", "status": "ok", "fwhm": 3.4, "wcs_solved": True}
ROW_SKIP = {"file": "b.fits", "status": "skipped: NoWCS", "reason": "no solve", "fwhm": None}


def test_importing_does_not_need_boto3(pf):
    # boto3 is imported inside r2_client() only, so these tests (and
    # --offline) run in the pixi env, which does not have it.
    source = SCRIPT.read_text()
    top_level = [ln for ln in source.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("boto3" in ln for ln in top_level)


def test_parse_key_and_since(pf):
    assert pf.parse_key(key(7)) == ("2026-11-05", RID, 7)
    assert pf.parse_key("reports/2026-11-05/NOTHEX/0.json") is None
    assert pf.parse_key("other/thing.json") is None
    keys = [key(1, "2026-11-06"), key(0, "2026-11-04"), "junk", key(0, "2026-11-05")]
    assert pf.keys_since(keys, "2026-11-05") == [key(0, "2026-11-05"), key(1, "2026-11-06")]
    assert len(pf.keys_since(keys)) == 3


def test_schema_patterns_work_in_python(validator):
    # The lookahead and \x escapes in the schema mean the same in Python's re.
    ok = chunk(0, [ROW_OK], comment="line one\nline two")
    assert list(validator.iter_errors(ok)) == []
    for bad in ("see https://x.org", "www.example.org", "a\tb"):
        assert list(validator.iter_errors(chunk(0, [], comment=bad)))
    assert list(validator.iter_errors(chunk(0, [{**ROW_OK, "file": "a/b.fits"}])))


def test_merge_orders_by_seq_and_concatenates_rows(pf, validator):
    items = [
        (key(1), stored(chunk(1, [ROW_SKIP], final=True, comment="later"),
                        received_at="2026-11-05T03:05:00Z", country="CA")),
        (key(0), stored(chunk(0, [ROW_OK], comment="first"))),
    ]
    merged = pf.merge_report(RID, items, validator)
    assert merged["seqs"] == [0, 1]
    assert [r["file"] for r in merged["rows"]] == ["a.fits", "b.fits"]
    assert [r["seq"] for r in merged["rows"]] == [0, 1]
    assert merged["n_rows"] == 2
    assert merged["comment"] == "later"          # latest chunk wins
    assert merged["run"]["outcome"] == "done"
    assert merged["final_received"] is True
    assert merged["missing_seqs"] == []
    assert merged["invalid_chunks"] == []
    assert merged["header_conflicts"] == []
    assert merged["first_received_at"] == "2026-11-05T03:00:00.000Z"
    assert merged["last_received_at"] == "2026-11-05T03:05:00Z"
    assert merged["countries"] == ["CA", "US"]
    assert merged["obscode"] == "CMW"


def test_merge_flags_gaps_no_final_conflicts_and_duplicates(pf, validator):
    items = [
        (key(0), stored(chunk(0, [ROW_OK]))),
        (key(2), stored(chunk(2, [ROW_OK], obscode="XYZ"))),
        # Same seq again under the next UTC day: the earlier copy is kept.
        (key(2, "2026-11-06"), stored(chunk(2, [], obscode="XYZ"),
                                      received_at="2026-11-06T00:00:01Z")),
    ]
    merged = pf.merge_report(RID, items, validator)
    assert merged["seqs"] == [0, 2]
    assert merged["missing_seqs"] == [1]
    assert merged["final_received"] is False
    assert merged["header_conflicts"] == ["obscode"]
    assert merged["duplicate_keys"] == [key(2, "2026-11-06")]
    assert merged["n_rows"] == 2


def test_invalid_chunks_are_flagged_not_dropped(pf, validator):
    bad = chunk(1, [{**ROW_OK, "extra": "x"}], comment="http://spam")
    items = [(key(0), stored(chunk(0, [ROW_OK]))), (key(1), stored(bad))]
    merged = pf.merge_report(RID, items, validator)
    assert merged["n_rows"] == 2                  # the bad chunk's row is still there
    assert [c["seq"] for c in merged["invalid_chunks"]] == [1]
    problems = " ".join(merged["invalid_chunks"][0]["problems"])
    assert "/comment" in problems and "additionalProperties" in problems
    assert "INVALID x1" in pf.summary_line(merged)


def test_wrapper_problems(pf, validator):
    assert pf.chunk_problems({"unparseable": "x"}, validator)
    assert pf.chunk_problems({**stored(chunk(0, [])), "ip": "1.2.3.4"}, validator) == [
        "unexpected wrapper field 'ip'"]
    assert pf.chunk_problems(stored(chunk(0, [])), validator) == []


def test_summary_line(pf, validator):
    rows = [ROW_OK, ROW_OK, ROW_SKIP]
    comment = "word " * 30 + "\nsecond line"
    merged = pf.merge_report(RID, [(key(0), stored(chunk(0, rows, final=True, comment=comment)))],
                             validator)
    line = pf.summary_line(merged)
    assert "\n" not in line
    assert line.startswith("01234567 CMW rows=3 outcome=done ok=2 skipped: NoWCS=1 |")
    shown = line.split(" | ", 1)[1]
    assert len(ast.literal_eval(shown)) == 80  # repr of the first 80 chars, whitespace collapsed
    assert pf.status_tally([]) == ""


def test_csv_columns_follow_schema_order(pf, tmp_path):
    rows = [{"seq": 0, **ROW_OK}, {"seq": 1, **ROW_SKIP, "surprise": 1}]
    path = tmp_path / "r.csv"
    pf.write_csv(rows, path)
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        got = list(reader)
        header = reader.fieldnames
    assert header[:4] == ["seq", "file", "status", "reason"]
    assert header[-1] == "surprise"
    assert got[0]["fwhm"] == "3.4" and got[1]["fwhm"] == ""
    assert got[1]["status"] == "skipped: NoWCS"


def test_merge_all_end_to_end_offline(pf, tmp_path, capsys):
    raw = tmp_path / "out" / "raw"
    other = "f" * 32
    files = {
        key(0): stored(chunk(0, [ROW_OK])),
        key(1): stored(chunk(1, [ROW_SKIP], final=True)),
        key(0, "2026-10-30", other): stored(chunk(0, [], report_id=other)),
    }
    for k, rec in files.items():
        (raw / k).parent.mkdir(parents=True, exist_ok=True)
        (raw / k).write_text(json.dumps(rec))
    broken = raw / key(5, "2026-11-07")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json")

    assert pf.main(["bucket", "--offline", "--out", str(tmp_path / "out"),
                    "--since", "2026-11-01"]) == 0
    out = capsys.readouterr().out
    assert "1 reports in" in out and "with chunks that fail the schema" in out
    merged = json.loads((tmp_path / "out" / f"{RID}.json").read_text())
    assert merged["seqs"] == [0, 1, 5]
    assert [c["seq"] for c in merged["invalid_chunks"]] == [5]
    assert (tmp_path / "out" / f"{RID}.csv").exists()
    assert not (tmp_path / "out" / f"{other}.json").exists()   # before --since


def test_r2_client_needs_credentials(pf):
    with pytest.raises(SystemExit, match="R2_ACCOUNT_ID"):
        pf.r2_client({})


class FakeS3:
    """The two boto3 S3 calls download() makes, over an in-memory bucket."""

    def __init__(self, objects):
        self.objects = objects
        self.list_kwargs = None
        self.gets = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class Paginator:
            def paginate(self, **kwargs):
                fake.list_kwargs = kwargs
                after = kwargs.get("StartAfter", "")
                keys = sorted(k for k in fake.objects
                              if k.startswith(kwargs["Prefix"]) and k > after)
                yield {"Contents": [{"Key": k} for k in keys[:1]]}
                yield {"Contents": [{"Key": k} for k in keys[1:]]}

        return Paginator()

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        body = self.objects[Key]

        class Body:
            def read(self):
                return body

        return {"Body": Body()}


def test_download_mirrors_new_keys_only(pf, tmp_path):
    objects = {
        key(0, "2026-10-31"): b"{}",
        key(0): json.dumps(stored(chunk(0, [ROW_OK]))).encode(),
        key(1): b"{}",
    }
    client = FakeS3(objects)
    (tmp_path / key(1)).parent.mkdir(parents=True)
    (tmp_path / key(1)).write_bytes(b"already here")
    # --since 2026-11-01 lists from the day before (a run can straddle UTC
    # midnight); load_raw is what then decides by whole report.
    assert pf.download(client, "bkt", tmp_path, "2026-11-01") == (3, 2)
    assert client.list_kwargs == {"Bucket": "bkt", "Prefix": "reports/",
                                  "StartAfter": "reports/2026-10-31"}
    assert client.gets == [key(0, "2026-10-31"), key(0)]
    assert (tmp_path / key(1)).read_bytes() == b"already here"
    assert (tmp_path / key(0, "2026-10-31")).read_bytes() == b"{}"


# -- review fixes (PR #16) ---------------------------------------------------


def test_csv_cells_that_look_like_formulas_are_neutralised(pf, tmp_path):
    hostile = {"seq": 0, "file": "=HYPERLINK(\"http://x\")", "status": "ok",
               "reason": "  +cmd|' /C calc'!A0", "fwhm": -3.4}
    plain = {"seq": 1, "file": "-dash.fits", "status": "ok", "reason": "@home", "fwhm": None}
    path = tmp_path / "r.csv"
    pf.write_csv([hostile, plain], path)
    with open(path, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert got[0]["file"] == "'=HYPERLINK(\"http://x\")"
    assert got[0]["reason"] == "'  +cmd|' /C calc'!A0"   # leading whitespace too
    assert got[0]["fwhm"] == "-3.4"                       # numbers are not strings
    assert got[1]["file"] == "'-dash.fits" and got[1]["reason"] == "'@home"
    assert got[1]["fwhm"] == ""
    assert pf.csv_cell("plain text") == "plain text" and pf.csv_cell("") == ""


def test_wrong_type_rows_and_run_are_flagged_not_fatal(pf, validator):
    items = [
        (key(0), stored(chunk(0, [ROW_OK]))),
        (key(1), stored(chunk(1, 1, run="done", final=True))),
    ]
    merged = pf.merge_report(RID, items, validator)
    assert [c["seq"] for c in merged["invalid_chunks"]] == [1]
    assert merged["n_rows"] == 1
    assert merged["run"] == "done"
    assert "outcome=None" in pf.summary_line(merged)


def test_unknown_then_known_header_fields_are_not_conflicts(pf, validator):
    items = [
        (key(0), stored(chunk(0, [ROW_OK], qa_schema=0, site_lat=None, site_lon=None))),
        (key(1), stored(chunk(1, [ROW_OK], qa_schema=3, site_lat=47, site_lon=-96))),
        (key(2), stored(chunk(2, [ROW_OK], qa_schema=3, site_lat=47, site_lon=-96, final=True))),
    ]
    assert pf.merge_report(RID, items, validator)["header_conflicts"] == []
    items.append((key(3), stored(chunk(3, [], qa_schema=4, site_lat=46))))
    assert pf.merge_report(RID, items, validator)["header_conflicts"] == ["qa_schema", "site_lat"]


def test_since_selects_whole_reports_across_utc_midnight(pf, tmp_path):
    raw = tmp_path / "raw"
    straddler = "a" * 32
    files = {
        key(0, "2026-11-04", straddler): stored(chunk(0, [ROW_OK], report_id=straddler)),
        key(1, "2026-11-05", straddler): stored(chunk(1, [ROW_SKIP], report_id=straddler, final=True)),
        key(0, "2026-11-04"): stored(chunk(0, [ROW_OK], final=True)),   # wholly before
    }
    for k, rec in files.items():
        (raw / k).parent.mkdir(parents=True, exist_ok=True)
        (raw / k).write_text(json.dumps(rec))
    grouped = pf.load_raw(raw, "2026-11-05")
    assert list(grouped) == [straddler]
    assert [pf.parse_key(k)[2] for k, _ in grouped[straddler]] == [0, 1]
    assert set(pf.load_raw(raw)) == {straddler, RID}

    # download mirrors from the day before so those earlier chunks exist locally.
    assert pf.day_before("2026-11-01") == "2026-10-31"
    objects = {k: json.dumps(v).encode() for k, v in files.items()}
    objects[key(0, "2026-11-03")] = b"{}"
    client = FakeS3(objects)
    assert pf.download(client, "bkt", tmp_path / "mirror", "2026-11-05") == (3, 3)
    assert client.list_kwargs["StartAfter"] == "reports/2026-11-04"
    assert not (tmp_path / "mirror" / key(0, "2026-11-03")).exists()
