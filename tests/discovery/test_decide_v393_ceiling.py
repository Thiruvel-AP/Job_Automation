"""v3.9.3 (display only): a shortlist line shows both ATS ceiling numbers, labelled. Cambridge Consultants' line said "ATS ceiling 100%" in its why
text (the figure the model wrote into its cached 7 Nov evaluation) while the gate said "ATS ceiling 47%" (today's pre-tailoring estimate)."""
from types import SimpleNamespace

from careerops.discovery import decide
from careerops.discovery import evaluate as ev_mod

SETTINGS = SimpleNamespace(raw={"apply": {"bands": {"auto": 70, "review": 50, "rescue_ceiling": 60}}})


def _ev(source: str) -> ev_mod.Evaluation:
    return ev_mod.Evaluation(job_id="manual:x", company="Cambridge Consultants", title="Graduate AI Engineer (2027 start)",
                             dimensions={"cv_match": {"score": 5.0, "status": "supported", "source": source},
                                         "north_star": {"score": 5.0, "status": "supported", "source": "Target role family ai_engineer matches title"}},
                             global_score=4.4, confidence="Medium", red_flags=[], legitimacy="high", top3_checks=["Ask for salary range"],
                             ts="2026-11-07T15:23:19+00:00")


def _item(source: str, ceiling: float | None) -> decide.Item:
    decision, reason = decide.ceiling_floor(decide.AUTO, "", ceiling, SETTINGS)
    return decide.Item(key="cambridgeconsultants|graduate ai engineer", company="Cambridge Consultants", title="Graduate AI Engineer (2027 start)",
                       decision=decision, reason=reason, ev=_ev(source), ceiling=ceiling, n=15)


def test_the_line_labels_the_evaluation_figure_and_the_estimate_the_gate_uses():
    it = _item("Fact-bank coverage 5.0, ATS ceiling 100%", 47.0)
    line = it.line()
    assert "| ATS ceiling: evaluation 100% (model's note, 2026-11-07) vs estimate 47% (pre-tailoring, today; the gate uses this) |" in line
    assert line.endswith("| ATS ceiling 47% is under 50% (apply.bands.review): never auto-approved")
    assert it.decision == decide.MARGINAL                                                    # unchanged logic: the estimate decides


def test_with_no_figure_in_the_evaluation_only_the_estimate_is_shown_and_with_neither_nothing():
    assert _item("Fact-bank coverage 4.0", 62.0).ceiling_text() == "ATS ceiling: estimate 62% (pre-tailoring, today; the gate uses this)"
    it = _item("Fact-bank coverage 4.0", None)
    assert it.ceiling_text() == "" and "ATS ceiling" not in it.line()
