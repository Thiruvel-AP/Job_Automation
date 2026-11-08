"""v3.4: the whole tailoring in plain style keeps the strict ATS score of the technical style (fixture bank with three plain bullets that drop
JD keywords, the fixture JD, MockLLM, real xelatex PDFs)."""
import copy

import pytest

from careerops.llm import MockLLM
from careerops.tailoring.render import pdf_text
from careerops.tailoring.tailor import tailor_job

pytestmark = pytest.mark.slow          # renders PDFs with xelatex

PLAIN = {"a1": "Researchers needed answers they could trust, so I built an AI assistant that the whole research team used.",
         "a3": "Releases were copied by hand, so I moved the assistant to the cloud, replacing a manual copy-and-paste release process.",
         "b1": "A charity's data sat in three places, so I joined it and gave the trustees a report they reused every quarter."}


def plain_bank(fb):
    fb = copy.deepcopy(fb)
    for e in fb["experience"] + fb["projects"]:
        for b in e["bullets"]:
            if b["id"] in PLAIN:
                b["plain"] = PLAIN[b["id"]]
    return fb


def run(settings, fb, job, style, sub):
    settings.raw["cv"]["style"] = style
    return tailor_job(dict(job), fb, MockLLM(), settings, settings.path("outputs") / sub)


def test_plain_style_keeps_strict_ats_at_least_the_technical_one(settings, fb_fixture, job, capsys):
    fb = plain_bank(fb_fixture)
    tech = run(settings, fb, job, "technical", "tech")
    plain = run(settings, fb, job, "plain", "plain")
    assert tech["style"]["style"] == "technical" and plain["style"]["style"] == "plain"
    assert plain["ats"]["strict"] >= tech["ats"]["strict"] - 0.01, (plain["ats"]["strict"], tech["ats"]["strict"])
    st = plain["style"]
    assert st["strict_plain"] >= st["strict_technical"]
    assert "plain style: strict" in capsys.readouterr().out
    text = pdf_text(__import__("pathlib").Path(plain["cv_pdf"]))
    on_cv = [b for b in st["plain_on_cv"]]
    assert on_cv and all(b in st["readability"] for b in on_cv)
    assert "Researchers needed answers they could trust" in " ".join(text.split()) or "a1" not in on_cv
    assert any("plain version missing" in w for w in plain["analysis_warnings"])
