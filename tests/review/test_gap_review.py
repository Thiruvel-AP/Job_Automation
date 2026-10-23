"""The review prompt for a job in the review band: the gap report instead of the plain ATS header, and the choices a / s / r / f / o / q.
`r` re-tailors with the claimable keywords ranked first; `f` asks for facts about the top gap keywords, stores them as provisional (never in the
fact bank), and re-tailors with them. The pipeline stages (tailoring, PDFs) are stubbed: this is about what the prompt asks and writes."""
import copy
import hashlib

import pytest
import yaml

from careerops import bands
from careerops.config import load_yaml
from careerops.review import workflow as review
from careerops.tailoring import provisional as prov

from conftest import PROFILE_FIX
from test_gap_report import report

CHOICES = ("[a] apply as is   [s] skip   [r] re-tailor with the claimable points forced in",
           "[f] add facts     [o] open PDF   [q] quit")
FACT = "Scheduled a nightly Dagster pipeline that retrained a churn model, which cut manual reruns to zero."


class World:
    """One job in the review band, with stubs for the report loader, the summary and the re-tailor hook."""

    def __init__(self, monkeypatch, db, settings, tmp_path, rep):
        self.rep, self.calls, self.opened = rep, [], []
        self.fb_path = tmp_path / "fact_bank.yaml"
        self.fb_path.write_bytes((PROFILE_FIX / "fact_bank.yaml").read_bytes())
        self.fb = load_yaml(self.fb_path)
        self.job = {"id": "manual:gap1", "source": "manual", "company": "Acme", "title": "Data Scientist", "location": "Bristol, UK",
                    "url": "https://acme.example/1", "apply_url": "https://acme.example/1", "description": "d"}
        db.upsert_job(self.job)
        db.update_job(self.job["id"], status="needs_review", out_dir=str(tmp_path / "out"))
        self.db, self.settings = db, settings
        monkeypatch.setattr(review, "load_report", lambda job: self.rep)
        monkeypatch.setattr(review, "summary", lambda job, rep, settings=None, fb=None: "\n".join(["SUMMARY", *review.provisional_lines(rep)]))
        monkeypatch.setattr(review, "open_file", lambda p: self.opened.append(p))
        self.next_rep = None

    def retailor(self, job_id, fb, boost):
        self.calls.append({"job": job_id, "fb": copy.deepcopy(fb), "boost": list(boost)})
        if self.next_rep is not None:
            self.rep = self.next_rep

    def run(self, *answers, retailor=True, allow="aelgnowsq"):
        it = iter(answers)
        self.prompts = []

        def ask(prompt=""):
            self.prompts.append(prompt)
            return next(it)

        return review.review_job(self.db, self.db.job(self.job["id"]), self.settings, self.fb, ask=ask, allow=allow,
                                 retailor=self.retailor if retailor else None)


@pytest.fixture
def world(monkeypatch, db, settings, tmp_path):
    fb = load_yaml(PROFILE_FIX / "fact_bank.yaml")
    rep = report(fb)
    rep["band"] = bands.classify(60, 75, bands.Bands()).to_dict()
    rep["cv_pdf"], rep["letter_pdf"] = "/x/cv.pdf", None
    rep["final"].update(chosen_attempt=1, score=0.6)                          # what approve() logs
    rep["attempts"][0]["selection"] = {}
    settings.raw["paths"]["fact_bank"] = str(tmp_path / "fact_bank.yaml")
    w = World(monkeypatch, db, settings, tmp_path, rep)
    return w


def test_a_job_in_the_review_band_gets_the_choices_of_the_gap_report(world, capsys):
    assert world.run("q") == "quit"
    out = capsys.readouterr().out
    assert CHOICES[0] in out and CHOICES[1] in out
    assert world.prompts[-1].strip() == ">"


def test_a_job_in_the_auto_band_keeps_the_plain_prompt(world, capsys):
    world.rep["band"] = bands.classify(75, 90, bands.Bands()).to_dict()
    world.rep["ats"].update(strict=75.0, ceiling=90.0)
    assert world.run("q") == "quit"
    assert "[a]pprove" in world.prompts[0] and CHOICES[0] not in capsys.readouterr().out


def test_a_apply_as_is_approves(world):
    assert world.run("a") == "approved"
    assert world.db.job(world.job["id"])["status"] == "approved"


def test_s_skips_as_skipped_user_and_o_opens_the_pdf(world):
    assert world.run("o", "s") == "skipped (user)"
    assert world.opened == ["/x/cv.pdf"]


def test_r_in_this_prompt_re_tailors_instead_of_rejecting(world, capsys):
    world.next_rep = dict(world.rep, ats=dict(world.rep["ats"], strict=70.0))
    assert world.run("r", "q", allow="aerlgnowsq") == "quit"                              # the `review` command offers r = reject elsewhere
    assert world.db.job(world.job["id"])["status"] == "needs_review"
    assert len(world.calls) == 1


def test_r_re_tailors_once_with_the_claimable_keywords_ranked_first_and_shows_the_new_numbers(world, capsys):
    world.next_rep = dict(world.rep, ats=dict(world.rep["ats"], strict=70.0, ceiling=75.0))
    world.next_rep["band"] = bands.classify(70, 75, bands.Bands()).to_dict()
    world.run("r", "q")
    [call] = world.calls
    assert call["job"] == "manual:gap1" and call["boost"] == ["SQL", "MLflow"]             # the claimable ones, largest gain first
    assert not prov.provisional_bullets(call["fb"])                                       # no typed fact: the bank as it is
    out = capsys.readouterr().out
    assert "re-tailored: ATS strict 60 -> 70, ceiling 75 -> 75" in out


def test_r_without_a_re_tailor_hook_says_so(world, capsys):
    world.run("r", "q", retailor=False)
    assert "no re-tailoring" in capsys.readouterr().out


def test_f_asks_for_the_top_five_gap_keywords_one_by_one_and_skipping_stores_nothing(world, tmp_path):
    world.run("f", "", "", "", "q")
    asked = [p for p in world.prompts if "have you used it?" in p]
    assert asked == [
        "Dagster (+10.0): have you used it? Describe it in one line (where, what, result), or press enter to skip. ",
        "Redshift (+5.0): have you used it? Describe it in one line (where, what, result), or press enter to skip. ",
        "Spark (+5.0): have you used it? Describe it in one line (where, what, result), or press enter to skip. "]    # Terraform is deny-listed: not asked
    assert world.calls == [] and not prov.Store(tmp_path / "facts" / "proposed.yaml").facts()    # a skipped answer is never a claim


def test_f_stores_what_was_typed_as_provisional_and_never_in_the_fact_bank(world, tmp_path, capsys):
    bank_before = hashlib.sha256(world.fb_path.read_bytes()).hexdigest()
    world.next_rep = dict(world.rep, ats=dict(world.rep["ats"], strict=80.0, ceiling=85.0),
                          provisional=[{"id": "prov1", "keyword": "Dagster", "text": FACT, "on_cv": True}])
    world.run("f", FACT, "az", "", "", "q")
    store = prov.Store(tmp_path / "facts" / "proposed.yaml")
    [f] = store.facts()
    assert (f.keyword, f.text, f.target, f.job_id, f.status) == ("Dagster", FACT, "az", "manual:gap1", "provisional")
    assert hashlib.sha256(world.fb_path.read_bytes()).hexdigest() == bank_before          # the fact bank is untouched
    assert not (tmp_path / "backups").exists()


def test_f_re_tailors_with_the_bank_plus_the_provisional_fact_and_the_claimable_keywords(world):
    world.run("f", FACT, "az", "", "", "q")
    [call] = world.calls
    [b] = prov.provisional_bullets(call["fb"])
    assert b["text"] == FACT and b["terms"] == ["Dagster"] and b["provisional"]
    assert call["boost"] == ["SQL", "MLflow", "Dagster"]


def test_f_prints_the_new_numbers_and_labels_the_provisional_bullets(world, capsys, monkeypatch):
    new = dict(world.rep, ats=dict(world.rep["ats"], strict=80.0, ceiling=85.0),
               provisional=[{"id": "prov1", "keyword": "Dagster", "text": FACT, "on_cv": True}])
    new["band"] = bands.classify(80, 85, bands.Bands()).to_dict()
    world.next_rep = new
    world.run("f", FACT, "az", "", "", "a")
    out = capsys.readouterr().out
    assert "re-tailored: ATS strict 60 -> 80, ceiling 75 -> 85" in out
    assert f"[provisional] {FACT}" in out


def test_a_typed_fact_that_names_a_deny_listed_tool_is_refused_before_it_is_stored(world, tmp_path, capsys):
    world.run("f", "Provisioned it with Terraform on a side project.", "", "", "q")
    assert "terraform" in capsys.readouterr().out.lower() and not prov.Store(tmp_path / "facts" / "proposed.yaml").facts()
    assert world.calls == []


def test_a_fact_with_no_role_is_dropped(world, tmp_path):
    world.run("f", FACT, "", "", "", "", "q")                                           # the role prompt answered with enter: skipped
    assert not prov.Store(tmp_path / "facts" / "proposed.yaml").facts() and world.calls == []


def test_the_role_can_be_chosen_by_number(world, tmp_path):
    world.run("f", FACT, "2", "", "", "q")
    [f] = prov.Store(tmp_path / "facts" / "proposed.yaml").facts()
    assert f.target == "dc"                                                              # the second entry of the bank


def test_a_sentence_without_the_keyword_is_noted_but_kept(world, tmp_path, capsys):
    world.run("f", "Automated nightly retraining of a churn model on a schedule.", "az", "", "", "q")
    assert "does not say 'Dagster'" in capsys.readouterr().out
    assert len(prov.Store(tmp_path / "facts" / "proposed.yaml").facts()) == 1


def test_a_second_f_does_not_ask_again_for_what_was_typed_and_keeps_the_earlier_fact(world, tmp_path):
    world.run("f", FACT, "az", "", "", "f", "Loaded the tables into Redshift for a coursework project.", "az", "", "q")
    store = prov.Store(tmp_path / "facts" / "proposed.yaml")
    assert [f.keyword for f in store.facts()] == ["Dagster", "Redshift"]
    second_round = [p for p in world.prompts if "have you used it?" in p][3:]
    assert [p.split(" ")[0] for p in second_round] == ["Redshift", "Spark"]               # Dagster has its fact: not asked again
    [_, second] = world.calls
    assert [b["terms"] for b in prov.provisional_bullets(second["fb"])] == [["Dagster"], ["Redshift"]]    # the first fact rides along


def test_provisional_lines_label_each_bullet_the_cv_uses():
    rep = {"provisional": [{"id": "prov1", "keyword": "Dagster", "text": FACT, "on_cv": True},
                           {"id": "prov2", "keyword": "Spark", "text": "Ran Spark jobs.", "on_cv": False}]}
    lines = review.provisional_lines(rep)
    assert any(l.strip().startswith("[provisional] " + FACT) for l in lines)
    [spark] = [l for l in lines if "Ran Spark jobs." in l]
    assert "[provisional]" not in spark and "not on the CV" in spark
    assert review.provisional_lines({"provisional": []}) == [] and review.provisional_lines({}) == []


def test_skip_user_outside_live_skips_the_job_and_files_its_jd(world, settings):
    inbox = settings.path("jds") / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "acme.txt").write_text("Company: Acme\nTitle: Data Scientist\nURL: https://acme.example/1\n\nSomething.\n", encoding="utf-8")
    review.skip_user(world.db, settings, world.job["id"])
    job = world.db.job(world.job["id"])
    assert (job["status"], job["reason"]) == ("skipped", "skipped (user)")
    assert [p.name for p in (settings.path("jds_done") / "skipped").iterdir()] == ["acme.txt"] and not (inbox / "acme.txt").exists()


def test_the_review_command_applies_a_skipped_user_answer(world, monkeypatch, settings):
    monkeypatch.setattr(review, "order", lambda jobs, s, tailored=True: list(jobs))
    answers = iter(["s"])
    world.db.update_job(world.job["id"], status="needs_review")
    monkeypatch.setattr("builtins.input", lambda p="": next(answers))
    review.run(world.db, settings, world.fb)
    assert world.db.job(world.job["id"])["status"] == "skipped"


def test_the_cli_retailor_hook_tailors_the_one_job_with_the_given_bank_and_boost(world, monkeypatch, settings):
    from careerops import cli

    seen = {}
    monkeypatch.setattr(cli, "tailor_jobs", lambda db, s, bank, llm, jobs, **kw: seen.update(bank=bank, jobs=[j["id"] for j in jobs], **kw))
    monkeypatch.setattr(cli, "_llm", lambda args, s, db: "LLM")
    hook = cli.retailor_hook(world.db, settings, type("A", (), {"mock": True})())
    hook(world.job["id"], {"the": "bank"}, ["SQL"])
    assert seen == {"bank": {"the": "bank"}, "jobs": [world.job["id"]], "mock": True, "boost": ["SQL"]}
