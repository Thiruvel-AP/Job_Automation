"""JD section detection (M5 1d): headings, clause-level 'nice to have', skip sections.

Heading lines are copied verbatim from the user's calibration JDs (Creditspring, Chattermill, Lendable, Zone & Co).
"""
import pytest

from careerops.tailoring import scorer

M5_1D = pytest.mark.xfail(strict=True, reason="M5 1d — section detection not implemented yet")


def kw(jd: str) -> dict[str, scorer.Keyword]:
    return {k.term.lower(): k for k in scorer.taxonomy_keywords(jd)}


@M5_1D
@pytest.mark.parametrize("line, kind", [
    ("What you'll need to succeed", "req"),                                        # Creditspring
    ("Desirable experience:", "pref"),
    ("What you’ll need:", "req"),                                                  # Chattermill, curly apostrophe
    ("➕It would be a bonus if you:", "pref"),
    ("Our Perks", "skip"),
    ("🦸‍♀️ The Role 🦸‍♀️", "neutral"),
    ("What we are looking for", "req"),                                            # Lendable
    ("Strong engineering skills and experience building production systems.", None),
    ("Why join us", "skip"),
    ("About Lendable", "skip"),
    ("What You'll Bring:", "req"),                                                 # Zone & Co
    ("What You'll Do:", "neutral"),
    ("Benefits", "skip"),
])
def test_classify_heading(line, kind):
    assert scorer.classify_heading(line) == kind


@M5_1D
def test_clause_level_nice_to_have_keeps_rest_required():
    jd = ("What you’ll need:\n"
          "Proficiency in PyTorch, including training loops and standard model fine-tuning workflows; "
          "exposure to parameter-efficient techniques such as LoRA is a plus.\n")
    k = kw(jd)
    assert k["pytorch"].weight == 2.0 and k["pytorch"].required
    assert k["lora"].weight == 1.0 and not k["lora"].required


@M5_1D
def test_clause_split_before_ideally():
    k = kw("Requirements\nStrong SQL, ideally Docker\n")
    assert k["sql"].weight == 2.0
    assert k["docker"].weight == 1.0


@M5_1D
def test_skip_sections_and_hashtags_add_no_keywords():
    jd = ("What you’ll need:\n"
          "Strong Python\n"
          "Our Perks\n"
          "Optional private healthcare plan\n"
          "💖 Our Values\n"
          " – Whether it's with each other, our customers, partners, or other stakeholders, "
          "we always communicate with openness and trust.\n"
          "#machinelearning #LLM #PyTorch\n")
    k = kw(jd)
    assert k["python"].weight == 2.0
    assert not {"healthcare", "stakeholders", "llm", "pytorch"} & k.keys()


@M5_1D
def test_lendable_headings_and_mode_reset():
    jd = ("What we are looking for\n"
          "Strong engineering skills and experience building production systems.\n"
          "Hands-on experience using LLMs in real workflows.\n"
          "Why join us\n"
          "Work with Docker and Kubernetes every day.\n"
          "Life at Lendable\n"
          "We run on AWS.\n")
    k = kw(jd)
    assert k["llms"].weight == 2.0 and k["llms"].required
    assert not {"docker", "kubernetes", "aws"} & k.keys()


@M5_1D
def test_zoneco_bring_is_required_and_benefits_skipped():
    jd = ("What You'll Do:\n"
          "Integrate retrieval into backend applications.\n"
          "What You'll Bring:\n"
          "Strong understanding of both relational (SQL) and non-relational databases.\n"
          "Benefits\n"
          "AWS credits for side projects\n")
    k = kw(jd)
    assert k["retrieval"].weight == 1.5
    assert k["sql"].weight == 2.0 and k["sql"].required
    assert "aws" not in k


def test_text_before_any_heading_is_neutral():
    assert kw("We use Python and Docker.\n")["python"].weight == 1.5
