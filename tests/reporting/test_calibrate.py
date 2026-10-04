"""`careerops calibrate`: manifest, per-JD measurements, aggregates, the report, --compare, the rubric and --summarise.
Everything runs on a MockLLM and on files under tmp_path; the real manifest, DB and outputs are never opened."""
import csv
import json
import shutil
from pathlib import Path

import pytest
import yaml

from careerops import cli
from careerops.config import load_yaml
from careerops.llm import BudgetExhausted, MockLLM
from careerops.reporting import calibrate as cal
from careerops.tailoring.render import pdf_text

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "tailoring"
HEADER = ("SOMECO - Data Scientist\nSource: freehire.me mirror\nFetched 2026-10-02. Page chrome removed.\n"
          + "=" * 70 + "\n\n")


@pytest.fixture
def fb(settings):
    return load_yaml(settings.path("fact_bank"))          # gates.load_roles needs the real family project ids


@pytest.fixture
def manifest(tmp_path):
    jds = tmp_path / "jds"
    jds.mkdir()
    shutil.copy(FIX / "jd_northwind.txt", jds / "northwind.txt")
    (jds / "sales.txt").write_text(HEADER + (FIX / "jd_chattermill.txt").read_text(encoding="utf-8"), encoding="utf-8")
    entries = [
        {"id": "cal-northwind", "company": "Northwind Health AI", "title": "AI Engineer",
         "jd_file": str(jds / "northwind.txt"), "manual_ats": 0.80},
        {"id": "cal-sales", "company": "Acme", "title": "Sales Manager", "jd_file": str(jds / "sales.txt"),
         "outcome": "interview"}]
    path = tmp_path / "manifest.yaml"
    path.write_text("# comments are fine\n" + yaml.safe_dump(entries), encoding="utf-8")
    return path


def _run(settings, db, fb, manifest, out, llm=None, **kw):
    kw.setdefault("log", lambda m: None)
    return cal.calibrate(settings, db, llm or MockLLM(), fb, manifest, out, **kw)


# ================================================================== manifest
def test_the_manifest_is_loaded_with_resolved_jd_paths(manifest, settings):
    entries = cal.load_manifest(manifest, settings.root)
    assert [e["id"] for e in entries] == ["cal-northwind", "cal-sales"]
    assert entries[0]["_jd_path"].name == "northwind.txt" and entries[1]["outcome"] == "interview"


def test_relative_jd_paths_resolve_against_the_repo_root(tmp_path, settings):
    (tmp_path / "x.txt").write_text("jd", encoding="utf-8")
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump([{"id": "a", "company": "A", "title": "T", "jd_file": "x.txt"}]))
    with pytest.raises(cal.ManifestError, match="jd_file not found"):
        cal.load_manifest(m, settings.root)                 # relative to the repo root, not to the manifest
    assert cal.load_manifest(m, tmp_path)[0]["_jd_path"] == tmp_path / "x.txt"


@pytest.mark.parametrize("entries, message", [
    ([], "expected a list"), ("text", "expected a list"), (["x"], "not a mapping"),
    ([{"id": "a", "company": "A", "title": "T"}], "missing jd_file"),
    ([{"company": "A", "title": "T", "jd_file": "j"}], "missing id"),
    ([{"id": "a", "company": "A", "title": "T", "jd_file": "j"}] * 2, "duplicate id a"),
    ([{"id": "a", "company": "A", "title": "T", "jd_file": "j", "outcome": "ghosted"}], "outcome must be one of")])
def test_manifest_problems_are_named(tmp_path, entries, message):
    (tmp_path / "j").write_text("jd", encoding="utf-8")
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump(entries))
    with pytest.raises(cal.ManifestError, match=message):
        cal.load_manifest(m, tmp_path)


def test_a_header_block_saved_with_the_jd_is_not_part_of_it(tmp_path):
    f = tmp_path / "j.txt"
    f.write_text(HEADER + "Real JD line one.\nLine two.\n", encoding="utf-8")
    assert cal.jd_text(f) == "Real JD line one.\nLine two.\n"
    plain = tmp_path / "p.txt"
    plain.write_text("About us\n==========\nWe build things.\n", encoding="utf-8")        # an underlined heading, no Source:
    assert cal.jd_text(plain) == "About us\n==========\nWe build things.\n"
    short = tmp_path / "s.txt"
    short.write_text("Title\n=====\nBody\n", encoding="utf-8")                              # too short to be our header rule
    assert cal.jd_text(short) == "Title\n=====\nBody\n"


# ================================================================== a run
@pytest.fixture
def run(settings, db, fb, manifest, tmp_path):
    out = tmp_path / "cal"
    return _run(settings, db, fb, manifest, out, stamp="20261004-100000"), out


def test_a_run_writes_the_report_the_json_and_the_rubric(run, settings):
    res, out = run
    assert res["report"] == out / "report-20261004-100000.md" and res["report"].is_file()
    assert res["json"].is_file() and res["rubric"] == out / "rubric.csv"
    md = res["report"].read_text(encoding="utf-8")
    for must in ("# Calibration report 20261004-100000", "## Per JD", "## Aggregates", "## Detail", "cal-northwind",
                 "cal-sales", "Guard block rate", "Band hit rate", "Headroom left", "Manual agreement", "Cost",
                 "temperature 0", "seed 7", "MockLLM"):
        assert must in md, must
    data = json.loads(res["json"].read_text(encoding="utf-8"))
    assert [r["id"] for r in data["rows"]] == ["cal-northwind", "cal-sales"] and data["meta"]["stamp"]


def test_every_jd_is_tailored_whatever_triage_said_and_the_verdict_is_recorded(run):
    res, _ = run
    north, sales = res["rows"]
    assert north["status"] == sales["status"] == "ok"
    assert sales["triage"]["passed"] is False and any("manager" in r for r in sales["triage"]["reasons"])
    assert north["triage"]["family"] == "ai_engineer" and sales["triage"]["family"] is None


def test_a_row_carries_everything_the_report_promises(run):
    north = run[0]["rows"][0]
    assert north["keywords"]["n"] == len(north["keywords"]["terms"]) <= 25
    assert 0 < north["ceiling"] <= 1 and 0 <= north["final"]["literal"] <= 1 and north["final"]["pages"] == 1
    assert north["final"]["band"] in ("below", "in", "above") and north["attempts"] >= 1
    assert set(north["rephrase"]) == {"proposed", "accepted", "blocked", "calls", "coverage"}
    assert set(north["rephrase"]["coverage"]) == {"eligible", "rephrased", "ratio"}
    assert north["selection"] and all(isinstance(v, list) and v for v in north["selection"].values())
    assert isinstance(north["violations"], list) and north["letter"]["status"] in ("ok", "fallback")
    assert len(north["letter"]["evidence"]) == 2 and north["letter"]["pages"] == 1
    assert north["usage"]["calls"] == 0 and north["usage"]["by_task"] == {}        # a MockLLM reports no tokens
    assert north["seconds"] >= 0 and north["manual_ats"] == 0.80 and north["reference"] is None
    assert Path(north["out_dir"]).is_dir()


def test_nothing_real_is_touched_jobs_are_not_inserted_and_cvs_go_under_the_run_folder(run, db, settings):
    res, out = run
    assert db.jobs() == []                                                  # no row in the jobs table
    assert not settings.path("outputs").exists() or not list(settings.path("outputs").iterdir())
    assert all(Path(r["out_dir"]).is_relative_to(out / "runs" / "20261004-100000" / "outputs") for r in res["rows"])
    assert (out / "runs" / "20261004-100000" / "outputs").is_dir()


def test_only_and_limit_choose_the_jds(settings, db, fb, manifest, tmp_path):
    only = _run(settings, db, fb, manifest, tmp_path / "a", only=["cal-sales"], stamp="s1")
    assert [r["id"] for r in only["rows"]] == ["cal-sales"]
    limited = _run(settings, db, fb, manifest, tmp_path / "b", limit=1, stamp="s2")
    assert [r["id"] for r in limited["rows"]] == ["cal-northwind"]
    with pytest.raises(cal.ManifestError, match="not in the manifest: nope"):
        _run(settings, db, fb, manifest, tmp_path / "c", only=["nope"])
    with open(tmp_path / "a" / "rubric.csv", encoding="utf-8", newline="") as f:
        assert [r["id"] for r in csv.DictReader(f)] == ["cal-northwind", "cal-sales"]      # the rubric lists every JD


# ================================================================== budget, errors, pacing
class BudgetLLM(MockLLM):
    """Room for `n` jobs, then the daily token budget is spent."""

    def __init__(self, n):
        super().__init__()
        self.n, self.begun = n, 0

    def begin_job(self, est_tokens):
        self.begun += 1
        if self.begun > self.n:
            raise BudgetExhausted("groq: 195,000 of 200,000 tokens used today, 5,000 left, a job needs ~20,000")
        return ["mock"]


def test_the_run_stops_when_the_daily_budget_cannot_cover_another_job_and_says_so(settings, db, fb, manifest,
                                                                                    tmp_path):
    logs = []
    res = _run(settings, db, fb, manifest, tmp_path / "cal", BudgetLLM(1), stamp="s", log=logs.append)
    assert [r["status"] for r in res["rows"]] == ["ok", "skipped_budget"]
    assert "daily LLM token budget" in res["rows"][1]["error"] and "195,000" in res["rows"][1]["error"]
    assert any("STOP" in m for m in logs) and any("00:00 UTC" in m for m in logs)
    a = res["aggregates"]
    assert (a["jobs"], a["ok"], a["not_run"]) == (2, 1, 1)
    assert "not run (daily token budget)" in res["report"].read_text(encoding="utf-8")


def test_every_started_job_ends_its_budget_window(settings, db, fb, manifest, tmp_path):
    class Spy(MockLLM):
        def __init__(self):
            super().__init__()
            self.events = []

        def begin_job(self, est):
            self.events.append(("begin", est))
            return ["mock"]

        def end_job(self):
            self.events.append(("end",))

    spy = Spy()
    _run(settings, db, fb, manifest, tmp_path / "cal", spy, stamp="s")
    est = settings["llm"]["job_token_estimate"]
    assert spy.events == [("begin", est), ("end",), ("begin", est), ("end",)]


def test_a_jd_that_fails_is_reported_and_the_others_are_still_measured(settings, db, fb, manifest, tmp_path,
                                                                        monkeypatch):
    real = cal.tailor_job

    def flaky(job, *a, **k):
        if job["id"].endswith("cal-sales"):
            raise RuntimeError("xelatex exploded")
        return real(job, *a, **k)

    monkeypatch.setattr(cal, "tailor_job", flaky)
    res = _run(settings, db, fb, manifest, tmp_path / "cal", stamp="s")
    assert [r["status"] for r in res["rows"]] == ["ok", "error"]
    assert "RuntimeError: xelatex exploded" in res["rows"][1]["error"]
    assert res["aggregates"]["errors"] == 1 and res["aggregates"]["ok"] == 1
    md = res["report"].read_text(encoding="utf-8")
    assert "error: RuntimeError: xelatex exploded" in md


def test_the_run_announces_its_token_estimate_and_warns_when_the_day_cannot_cover_it(settings, db, fb, manifest,
                                                                                       tmp_path):
    class Prov:
        cfg = {"model": "m"}
        name = "p"

        def tokens_left(self):
            return 15_000

    llm = BudgetLLM(5)
    llm.providers = [Prov()]
    logs = []
    _run(settings, db, fb, manifest, tmp_path / "cal", llm, stamp="s", log=logs.append, limit=1)
    assert "Calibrating 1 JD(s), about 10,000 tokens" in logs[0] and "15,000 left today" in logs[0]
    logs.clear()
    _run(settings, db, fb, manifest, tmp_path / "cal2", llm, stamp="s2", log=logs.append)
    assert any("more than today's budget" in m for m in logs)


# ================================================================== reference CV and --compare
def test_a_reference_cv_is_scored_with_the_pipelines_keywords_and_a_second_run_can_be_compared(
        settings, db, fb, manifest, tmp_path):
    first = _run(settings, db, fb, manifest, tmp_path / "cal", stamp="20261004-100000")
    cv = next(Path(first["rows"][0]["out_dir"]).glob("* CV.pdf"))
    ref = tmp_path / "reference.pdf"
    shutil.copy(cv, ref)                                                     # "the CV I actually sent" = the pipeline's own
    entries = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entries[0]["reference_cv"] = str(ref)
    entries = [e for e in entries if e["id"] == "cal-northwind"]             # the second run drops cal-sales
    manifest.write_text(yaml.safe_dump(entries), encoding="utf-8")
    second = _run(settings, db, fb, manifest, tmp_path / "cal", stamp="20261004-110000",
                  compare_to=first["report"])
    row = second["rows"][0]
    assert row["reference"] == pytest.approx(row["final"]["literal"]) and pdf_text(ref)
    assert second["aggregates"]["reference"] == {"n": 1, "mean_delta": pytest.approx(0.0)}
    md = second["report"].read_text(encoding="utf-8")
    assert "## Compared with 20261004-100000" in md and "| Metric | Before | Now | Δ |" in md
    assert "| cal-northwind | " in md.split("## Compared with")[1]
    assert "Only in the earlier run: cal-sales" in md


def test_compare_accepts_the_md_or_the_json_and_refuses_a_report_without_a_sidecar(settings, db, fb, manifest,
                                                                                      tmp_path):
    first = _run(settings, db, fb, manifest, tmp_path / "cal", limit=1, stamp="s1")
    assert cal.load_previous(first["report"])["meta"]["stamp"] == cal.load_previous(first["json"])["meta"]["stamp"]
    lonely = tmp_path / "old-report.md"
    lonely.write_text("# an old report", encoding="utf-8")
    with pytest.raises(cal.ManifestError, match="no JSON sidecar"):
        _run(settings, db, fb, manifest, tmp_path / "cal2", compare_to=lonely)


def test_compare_lines_show_the_deltas_between_two_sets_of_aggregates(settings):
    before = [_row("a", ceiling=0.9, final=0.70, cov=(1, 4), violations=3, tokens=9000)]
    after = [_row("a", ceiling=0.9, final=0.80, cov=(3, 4), violations=1, tokens=7000)]
    prev = {"meta": {"stamp": "old"}, "rows": before, "aggregates": cal.aggregates(before, settings)}
    text = "\n".join(cal.compare_lines(prev, after, cal.aggregates(after, settings)))
    assert "| Mean final | 70% | 80% | +10% |" in text
    assert "| Violations | 3 | 1 | -2 |" in text
    assert "| Tokens per JD | 9,000 | 7,000 | -2,000 |" in text
    assert "| a | 70% → 80% | 90% → 90% | 1/4 → 3/4 | 3 → 1 | ok → ok | 9,000 → 7,000 |" in text


# ================================================================== aggregates
def _row(i, ceiling=0.9, final=0.78, band="in", cov=(2, 4), proposed=4, blocked=1, manual=None, reference=None,
         violations=0, letter="ok", calls=6, tokens=8000, seconds=30.0, kw=20, max_prompt=3000, status="ok"):
    return {"id": i, "company": "C", "title": "T", "status": status, "triage": {"passed": True, "reasons": [],
            "flags": [], "family": "data_scientist", "tier": 1, "ceiling_pre": 0.8},
            "keywords": {"n": kw, "required": 5, "terms": ["python"] * kw}, "ceiling": ceiling, "gaps": [],
            "final": {"literal": final, "stemmed": final, "band": band, "pages": 1, "misses": [], "wording_misses": []},
            "attempts": 1, "rephrase": {"proposed": proposed, "accepted": proposed - blocked, "blocked": blocked,
                                        "calls": 2, "coverage": {"eligible": cov[1], "rephrased": cov[0],
                                                                 "ratio": cov[0] / cov[1] if cov[1] else None}},
            "selection": {"ex1": ["a1"]}, "violations": ["v"] * violations, "profile": {"facts": ["f", "g"]},
            "letter": {"status": letter, "problems": [], "score": 0.3, "pages": 1, "evidence": ["a1", "b1"]},
            "usage": {"calls": calls, "total_tokens": tokens, "prompt_tokens": tokens - 500, "completion_tokens": 500,
                      "max_prompt": max_prompt, "by_task": {}},
            "seconds": seconds, "manual_ats": manual, "reference": reference, "outcome": None, "out_dir": "x"}


def test_the_guard_block_rate_is_blocked_over_proposed(settings):
    a = cal.aggregates([_row("a", proposed=4, blocked=1), _row("b", proposed=6, blocked=4)], settings)["guard"]
    assert (a["proposed"], a["blocked"], a["rate"], a["investigate"]) == (10, 5, 0.5, True)
    ok = cal.aggregates([_row("a", proposed=10, blocked=3)], settings)["guard"]
    assert ok["rate"] == 0.3 and ok["investigate"] is False                  # "> 0.30" investigates, 0.30 does not
    assert cal.aggregates([_row("a", proposed=0, blocked=0)], settings)["guard"]["rate"] is None


def test_band_hit_rate_counts_only_jds_whose_ceiling_reaches_the_floor_and_headroom_uses_the_same_jds(settings):
    rows = [_row("a", ceiling=0.90, final=0.78, band="in"), _row("b", ceiling=0.80, final=0.65, band="below"),
            _row("c", ceiling=0.60, final=0.60, band="below"),                          # ceiling < 0.70: out of scope
            _row("d", ceiling=0.95, final=0.84, band="in"), _row("e", ceiling=0.85, final=0.86, band="above")]
    a = cal.aggregates(rows, settings)
    assert a["band"] == {"in_scope": 4, "in_band": 2, "rate": 0.5}
    assert a["headroom"] == pytest.approx((0.12 + 0.15 + 0.11 - 0.01) / 4)


def test_manual_agreement_and_rank_correlation_use_the_manual_scores_when_there_are_too_few_references(settings):
    rows = [_row("a", final=0.70, manual=0.60), _row("b", final=0.75, manual=0.70), _row("c", final=0.80, manual=0.90),
            _row("d", final=0.85)]                                                       # no manual score
    a = cal.aggregates(rows, settings)
    assert a["manual"]["n"] == 3 and a["manual"]["mean_abs_diff"] == pytest.approx((0.10 + 0.05 + 0.10) / 3)
    assert a["manual"]["rho"] == pytest.approx(1.0)
    assert a["rank"]["basis"] == "manual" and a["rank"]["n"] == 3 and a["rank"]["rho"] == pytest.approx(1.0)


def test_four_reference_scores_switch_the_rank_comparison_to_the_references(settings):
    rows = [_row(c, final=f, manual=m, reference=r) for c, f, m, r in
            (("a", 0.70, 0.9, 0.60), ("b", 0.75, 0.8, 0.65), ("c", 0.80, 0.7, 0.70), ("d", 0.85, 0.6, 0.80))]
    a = cal.aggregates(rows, settings)
    assert a["rank"]["basis"] == "reference" and a["rank"]["rho"] == pytest.approx(1.0)
    assert a["manual"]["rho"] == pytest.approx(-1.0)
    assert a["reference"] == {"n": 4, "mean_delta": pytest.approx(0.0875)}
    three = cal.aggregates(rows[:3], settings)
    assert three["rank"]["basis"] == "manual"                                           # < 4 references: not enough


def test_cost_rephrase_letters_violations_and_not_run_jobs(settings):
    rows = [_row("a", calls=6, tokens=8000, seconds=30, cov=(1, 4), violations=2, max_prompt=3000),
            _row("b", calls=8, tokens=12000, seconds=50, cov=(3, 4), violations=1, letter="fallback", max_prompt=4500),
            {"id": "c", "company": "C", "title": "T", "status": "error", "error": "boom", "seconds": 1.0,
             "manual_ats": None, "outcome": None},
            {"id": "d", "company": "C", "title": "T", "status": "skipped_budget", "error": "budget", "seconds": 0.0,
             "manual_ats": None, "outcome": None}]
    a = cal.aggregates(rows, settings)
    assert (a["jobs"], a["ok"], a["errors"], a["not_run"]) == (4, 2, 1, 1)
    assert a["cost"] == {"calls_per_job": 7, "seconds_per_job": 40, "tokens_per_job": 10000, "tokens_total": 20000,
                         "max_prompt": 4500}
    assert a["rephrase"] == {"eligible": 8, "rephrased": 4, "ratio": 0.5}
    assert a["letters"] == {"ok": 1, "fallback": 1} and a["violations"] == 3


def test_an_empty_run_has_empty_aggregates_not_a_crash(settings):
    a = cal.aggregates([], settings)
    assert a["ok"] == 0 and a["headroom"] is None and a["guard"]["rate"] is None and a["cost"]["max_prompt"] is None
    meta = {"stamp": "s", "models": ["m"], "temperature": 0, "seed": 7, "band": [0.7, 0.85], "max_keywords": 25}
    assert "0 of 0 JD(s) measured" in cal.render_report(meta, [], a)


def test_spearman():
    assert cal.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert cal.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert cal.spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(0.8)
    assert cal.spearman([1, 2, 2, 3], [1, 2, 3, 3]) == pytest.approx(0.9, abs=0.1)         # ties share average ranks
    assert cal.spearman([1, 2], [1, 2]) is None and cal.spearman([1, 1, 1], [1, 2, 3]) is None
    assert cal.spearman([1, 2, 3], [1, 2]) is None


# ================================================================== rubric and --summarise
def test_the_rubric_has_the_agreed_columns_and_never_overwrites_ratings(tmp_path):
    path = tmp_path / "rubric.csv"
    assert cal.write_rubric(path, ["a", "b"]) == 2
    rows = list(csv.reader(path.open(encoding="utf-8", newline="")))
    assert rows[0] == ["id", "cv_false_claim(Y/N)", "cv_relevance(1-5)", "cv_readability(1-5)",
                       "letter_false_claim(Y/N)", "letter_tone(1-5)", "would_send(Y/N)", "notes"]
    assert rows[1:] == [["a"] + [""] * 7, ["b"] + [""] * 7]
    rows[1][1], rows[1][7] = "N", "good CV"
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    assert cal.write_rubric(path, ["a", "b", "c"]) == 1                        # only the new id is added
    back = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    assert [r["id"] for r in back] == ["a", "b", "c"] and back[0]["cv_false_claim(Y/N)"] == "N"
    assert back[0]["notes"] == "good CV"
    assert cal.write_rubric(path, ["a", "b", "c"]) == 0


def _report_with_rows(tmp_path, rows, settings, stamp="20261004-120000"):
    meta = {"stamp": stamp, "models": ["m"], "temperature": 0, "seed": 7, "band": [0.7, 0.85], "max_keywords": 25}
    agg = cal.aggregates(rows, settings)
    (tmp_path / f"report-{stamp}.md").write_text(cal.render_report(meta, rows, agg), encoding="utf-8")
    (tmp_path / f"report-{stamp}.json").write_text(json.dumps({"meta": meta, "rows": rows, "aggregates": agg}),
                                                   encoding="utf-8")


def _fill(path, **ratings):
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    cols = list(rows[0])
    for r in rows:
        for col, value in ratings.get(r["id"], {}).items():
            r[col] = value
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def test_summarise_merges_the_filled_rubric_into_the_latest_report_and_checks_the_exit_criteria(tmp_path, settings):
    rows = [_row(i, ceiling=0.9, final=0.78, band="in") for i in ("a", "b", "c", "d")]
    _report_with_rows(tmp_path, [_row("old")], settings, stamp="20261001-000000")      # an older report is left alone
    _report_with_rows(tmp_path, rows, settings)
    cal.write_rubric(tmp_path / "rubric.csv", ["a", "b", "c", "d"])
    good = {"cv_false_claim(Y/N)": "N", "letter_false_claim(Y/N)": "N", "would_send(Y/N)": "Y", "cv_relevance(1-5)": "4",
            "notes": "fine"}
    _fill(tmp_path / "rubric.csv", a=good, b=good, c=good, d={**good, "would_send(Y/N)": "N"})
    report = cal.summarise(tmp_path)
    assert report.name == "report-20261004-120000.md"
    text = report.read_text(encoding="utf-8")
    assert "## Rubric (4 of 4 JD(s) rated)" in text and "| a | 78% | ok | N | 4 |" in text
    assert "✓ zero false claims (CV and letter)" in text and "✓ band hit rate ≥ 3 of 4 in-scope JDs" in text
    assert "✓ no letter with rule problems" in text and "✓ would_send = Y for at least 3 JDs" in text
    assert "Rubric" not in (tmp_path / "report-20261001-000000.md").read_text(encoding="utf-8")
    _fill(tmp_path / "rubric.csv", a={"cv_false_claim(Y/N)": "Y"})
    again = cal.summarise(tmp_path).read_text(encoding="utf-8")
    assert again.count("## Rubric") == 1 and "✗ zero false claims (CV and letter)" in again    # replaced, not appended


def test_summarise_says_what_is_missing(tmp_path, settings):
    with pytest.raises(cal.ManifestError, match="no report-"):
        cal.summarise(tmp_path)
    _report_with_rows(tmp_path, [_row("a")], settings)
    with pytest.raises(cal.ManifestError, match="rubric.csv does not exist"):
        cal.summarise(tmp_path)


def test_unrated_rows_do_not_count_as_rated(tmp_path, settings):
    _report_with_rows(tmp_path, [_row("a"), _row("b")], settings)
    cal.write_rubric(tmp_path / "rubric.csv", ["a", "b"])
    text = cal.summarise(tmp_path).read_text(encoding="utf-8")
    assert "## Rubric (0 of 2 JD(s) rated)" in text and "✗ zero false claims" in text and "✗ would_send" in text


# ================================================================== the command
@pytest.fixture
def config(settings, tmp_path):
    settings.raw["paths"]["mock_root"] = str(tmp_path / "mockroot")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump(settings.raw), encoding="utf-8")
    return cfg


def test_the_command_runs_on_a_mock_root_and_prints_where_the_files_are(config, manifest, tmp_path, capsys):
    cli.main(["--config", str(config), "calibrate", "--mock", "--manifest", str(manifest), "--limit", "1"])
    out = capsys.readouterr().out
    mock_cal = tmp_path / "mockroot" / "calibration"
    assert "Calibrating 1 JD(s)" in out and "Wrote " in out and "--summarise" in out
    assert len(list(mock_cal.glob("report-*.md"))) == 1 and (mock_cal / "rubric.csv").is_file()
    assert not (tmp_path / "calibration" / "rubric.csv").exists()          # the configured (real) folder was not written


def test_the_command_summarises_and_reports_manifest_errors(config, manifest, tmp_path, capsys):
    cli.main(["--config", str(config), "calibrate", "--mock", "--manifest", str(manifest), "--limit", "1"])
    cli.main(["--config", str(config), "calibrate", "--mock", "--summarise"])
    assert "Merged the rubric into " in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump([{"id": "a"}]))
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(config), "calibrate", "--mock", "--manifest", str(bad)])
    assert e.value.code == 1 and "is missing company, title, jd_file" in capsys.readouterr().err


def test_the_command_exits_1_when_a_jd_errors_or_the_budget_ran_out(config, manifest, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: BudgetLLM(0))
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(config), "calibrate", "--mock", "--manifest", str(manifest)])
    assert e.value.code == 1 and "STOP" in capsys.readouterr().out


# ================================================================== --provider and the provider/model record
def _served_llm(providers, pinned=None):
    """A MockLLM that records, as the Router does, which provider and model answered each call (cycling `providers`)."""
    class P:
        def __init__(self, name):
            self.name, self.cfg = name, {"model": f"{name}-model"}

        def tokens_left(self):
            return None

    class Served(MockLLM):
        def __init__(self):
            super().__init__()
            self.providers, self.pinned, self.n = [P(n) for n in providers], pinned, 0

        def json(self, task, system, user, context=None):
            out = super().json(task, system, user, context)
            name = providers[self.n % len(providers)]
            self.n += 1
            self.usage.append({"task": task, "provider": name, "model": f"{name}-served", "prompt_tokens": 100,
                               "completion_tokens": 10, "total_tokens": 110})
            return out

    return Served()


def test_the_report_records_the_provider_and_model_that_served_every_call(settings, db, fb, manifest, tmp_path):
    res = _run(settings, db, fb, manifest, tmp_path / "cal", _served_llm(["ollama"], pinned="ollama"), stamp="s", limit=1)
    row = res["rows"][0]
    assert row["usage"]["served_by"] == ["ollama/ollama-served"]
    assert row["usage"]["calls"] > 0
    assert all(t["served_by"] == ["ollama/ollama-served"] for t in row["usage"]["by_task"].values())
    md = res["report"].read_text(encoding="utf-8")
    assert "Provider: ollama only (--provider: no fallback)" in md
    assert "Served every call: ollama/ollama-served" in md and "MIXED" not in md
    assert "| served by |" in md and "| ollama/ollama-served |" in md
    data = json.loads(res["json"].read_text(encoding="utf-8"))
    assert data["meta"]["pinned"] == "ollama" and data["meta"]["providers"] == ["ollama (ollama-model)"]


def test_an_unpinned_run_that_fell_back_is_flagged_as_mixed(settings, db, fb, manifest, tmp_path):
    res = _run(settings, db, fb, manifest, tmp_path / "cal", _served_llm(["groq", "ollama"]), stamp="s", limit=1)
    assert res["rows"][0]["usage"]["served_by"] == ["groq/groq-served", "ollama/ollama-served"]
    md = res["report"].read_text(encoding="utf-8")
    assert "Provider: groq (groq-model), ollama (ollama-model) (fallback in this order)" in md
    assert "MIXED providers" in md


def test_a_mock_run_names_no_provider(run):
    md = run[0]["report"].read_text(encoding="utf-8")
    assert "Provider: MockLLM (fallback" in md and "Served every call: –" in md


def test_calibrate_provider_builds_a_pinned_router(config, manifest, monkeypatch):
    got = {}

    def fake(settings, db, llm, fb, manifest, out, **kw):
        got["llm"] = llm
        return {"report": "r", "json": "j", "rubric": "u", "aggregates": {"errors": 0, "not_run": 0}}

    monkeypatch.setattr(cal, "calibrate", fake)
    cli.main(["--config", str(config), "calibrate", "--provider", "ollama", "--manifest", str(manifest)])
    assert [p.name for p in got["llm"].providers] == ["ollama"] and got["llm"].pinned == "ollama"
    cli.main(["--config", str(config), "calibrate", "--manifest", str(manifest)])
    assert [p.name for p in got["llm"].providers] == ["groq", "ollama"] and got["llm"].pinned is None


def test_calibrate_provider_rejects_an_unknown_name_and_mock(config, manifest, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(config), "calibrate", "--provider", "gemini", "--manifest", str(manifest)])
    assert e.value.code == 1 and "no LLM provider named 'gemini'; configured: groq, ollama" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(config), "calibrate", "--mock", "--provider", "ollama"])
    assert e.value.code == 1 and "--provider" in capsys.readouterr().err
