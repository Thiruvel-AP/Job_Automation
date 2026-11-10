"""v3.5 / v3.5a on the review screen: the version's queue row, Claude asked once (fake binary), who decided, shadow vs live, fallback."""
import pytest

from careerops import judge
from careerops.review import claude_review
from careerops.review import decision_queue as dq
from careerops.review import workflow as review
from careerops.review.judge import Verdict
from careerops.tailoring.doccheck import DocCheck
from judge_fakes import SEND, FakeClaude, use_claude

pytestmark = pytest.mark.slow          # the `tailored` fixture renders PDFs with xelatex


def scripted(*answers):
    it = iter(answers)
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        return next(it)
    ask.prompts = prompts
    return ask


@pytest.fixture
def sendable(monkeypatch):
    """Guards and checks pass, so a send can be carried out (the fixture's fabricating model would fail them)."""
    monkeypatch.setattr(dq, "send_blockers", lambda *a, **k: [])
    monkeypatch.setattr(review, "doc_check", lambda *a, **k: DocCheck([]))
    monkeypatch.setattr(review, "letter_status", lambda rep: "ok")
    monkeypatch.setattr(review, "contacts_step", lambda *a, **k: None)


def test_shadow_mode_never_uses_claudes_answer(tailored, settings, db, job, tmp_path, monkeypatch, sendable, capsys):
    _, fb, _ = tailored
    use_claude(settings, "shadow")
    fake = FakeClaude(tmp_path, monkeypatch).set(answer=SEND)
    ask = scripted("s")
    out = review.review_job(db, db.job(job["id"]), settings, fb, ask=ask, claude=judge.Session(settings), queue_apply=True)
    assert out == "skipped" and db.job(job["id"])["status"] == "needs_review" and len(ask.prompts) == 1
    assert "decided by: claude (high) | send [shadow]" in capsys.readouterr().out
    assert len(fake.judge_calls()) == 1
    st = dq.stats(settings)["kinds"]["review"]
    assert st["confusion"] == {"claude send / you park": 1}                # both answers stored


def test_live_mode_carries_out_claudes_send(tailored, settings, db, job, tmp_path, monkeypatch, sendable, capsys):
    _, fb, _ = tailored
    use_claude(settings, "live")
    FakeClaude(tmp_path, monkeypatch).set(answer=SEND)
    ask = scripted()                                                       # no prompt may be asked
    out = review.review_job(db, db.job(job["id"]), settings, fb, ask=ask, claude=judge.Session(settings), queue_apply=True)
    assert out == "approved" and db.job(job["id"])["status"] == "approved" and ask.prompts == []
    assert "approved by claude (high)" in capsys.readouterr().out


def test_live_mode_falls_back_to_you_when_claude_is_unavailable(tailored, settings, db, job, tmp_path, monkeypatch, sendable, capsys):
    _, fb, _ = tailored
    use_claude(settings, "live")
    FakeClaude(tmp_path, monkeypatch).set(mode="exit1")
    ask = scripted("a")
    out = review.review_job(db, db.job(job["id"]), settings, fb, ask=ask, claude=judge.Session(settings), queue_apply=True)
    assert out == "approved" and len(ask.prompts) == 1
    assert "decided by: you (claude unavailable: claude exited 1)" in capsys.readouterr().out
    assert dq.log_lines(settings, "decision")[-1]["by"] == "user"


def test_the_prompt_shows_claudes_prior_decision_and_your_cli_park_is_carried_out(tailored, settings, db, job, sendable, capsys):
    _, fb, _ = tailored
    use_claude(settings, "live")
    rep = review.load_report(db.job(job["id"]))
    row = dq.review_row(settings, db.job(job["id"]), rep)
    dq.decide(settings, row, "send", by="claude", confidence="low", reasons=["the hook misses the ad's problem"])
    ask = scripted("s")
    review.review_job(db, db.job(job["id"]), settings, fb, ask=ask, queue_apply=True)    # no session: nothing is asked of Claude
    out = capsys.readouterr().out
    assert "decided by: claude (low) | send [low confidence: yours to decide] | the hook misses the ad's problem" in out
    dq.decide(settings, row, "park", by="user")                           # `careerops decide <qid> park --by user`
    got = review.review_job(db, db.job(job["id"]), settings, fb, ask=scripted(), queue_apply=True)
    assert got == review.PARKED and db.job(job["id"])["status"] == "needs_review"


def test_a_disagreement_with_the_second_opinion_judge_escalates():
    d = {"by": "claude", "option": "send"}
    assert "would not send" in claude_review.escalation(d, Verdict(would_send=False, score=2))
    assert claude_review.escalation(d, Verdict(would_send=True, score=5)) == ""
    assert claude_review.escalation(d, None) == ""
    assert claude_review.escalation({"by": "user", "option": "send"}, Verdict(would_send=False)) == ""
