"""Company-domain terms: the industry words a JD is about (bank, banking, fintech, payments, customers ...).

They are not skills, so they are not JD keywords, but a letter that says "I know your world" needs them: the evidence bullets
are ranked by overlap with the JD's keywords AND with the JD's domain (budget.letter_scores), and a hook that links the
company's mission to the candidate's work with "mirrors / reflects / aligns" must share a domain term with that work
(grounding.hook_link_problems). Words are grouped, so "bank" in the JD meets "fintech" or "financial services" in a bullet.
Code only: no model, no network.
"""
from __future__ import annotations

import re

# group -> singular words (plurals are generated). A word belongs to one group.
DOMAIN_GROUPS: dict[str, tuple[str, ...]] = {
    "finance": ("bank", "banking", "banker", "fintech", "financial", "finance", "payment", "credit", "lending", "lender",
                "loan", "mortgage", "insurance", "insurer", "pension", "invest", "investing", "investment", "investor",
                "wealth", "fraud", "fincrime", "money", "transaction", "trading", "brokerage"),
    "customer": ("customer", "consumer", "churn", "retention", "subscriber"),
    "health": ("health", "healthcare", "clinical", "patient", "medical", "pharma", "pharmaceutical", "biotech", "hospital",
               "cancer", "oncology", "omics", "genomic", "genomics", "genetic", "genetics"),
    "travel": ("travel", "traveller", "traveler", "rail", "airline", "airport", "aviation", "flight", "hotel", "tourism"),
    "retail": ("retail", "retailer", "commerce", "shopper", "merchant", "marketplace", "grocery"),
    "advertising": ("advertising", "advertiser", "adtech", "marketing", "campaign"),
    "energy": ("energy", "utility", "renewable"),
    "telecom": ("telecom", "telecoms", "telecommunications"),
    "logistics": ("logistics", "freight", "warehouse", "shipping"),
    "public": ("government", "council", "legal", "defence", "defense"),
    "education": ("education", "student", "school", "edtech", "learner"),
    "gaming": ("gaming", "game", "gamer"),
}


# Topics a JD can be about that rank CV content without being a company's industry. They are used only to rank bullets and projects
# for the CV (budget.relevance_scores); the letter's hook check (grounding.hook_link_problems) still sees DOMAIN_GROUPS alone.
TOPIC_GROUPS: dict[str, tuple[str, ...]] = {
    "behaviour": ("behaviour", "behavior", "behavioural", "behavioral", "segmentation", "segment", "segmenting", "segmented"),
    "validation": ("validation", "validate", "validated", "validating"),
    "statistics": ("statistic", "statistics", "statistical", "statistician"),
}


def _forms(word: str) -> set[str]:
    out = {word, word + "s", word + "es"}
    if word.endswith("y"):
        out.add(word[:-1] + "ies")
    return out


def _table(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for group, words in groups.items():
        for w in words:
            for f in _forms(w):
                out.setdefault(f, group)
    return out


_WORD_GROUP = _table(DOMAIN_GROUPS)
_RANK_GROUP = _table({**DOMAIN_GROUPS, **TOPIC_GROUPS})      # the company domains and the topics, for CV ranking

_TOKEN = re.compile(r"[a-z]+")
JD_FULL_WEIGHT = 3          # a group named this many times in the JD counts in full; fewer counts in proportion


def _counts(text: str, table: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for w in _TOKEN.findall((text or "").lower()):
        g = table.get(w)
        if g:
            out[g] = out.get(g, 0) + 1
    return out


def domain_counts(text: str) -> dict[str, int]:
    """How many times each domain group is named in `text`: {"finance": 7, "customer": 4}. Hyphens split words
    ("bank-account" names bank); nothing here depends on case."""
    return _counts(text, _WORD_GROUP)


def domain_groups(text: str) -> set[str]:
    """The domain groups `text` names at all."""
    return set(domain_counts(text))


def _weights(counts: dict[str, int]) -> dict[str, float]:
    return {g: min(n, JD_FULL_WEIGHT) / JD_FULL_WEIGHT for g, n in counts.items()}


def jd_domain(jd: str) -> dict[str, float]:
    """The JD's domain as {group: weight}, weight 1/3, 2/3 or 1 by how often the JD names the group (a bank JD that says
    "bank" twenty times is about banking; one passing mention of "customers" counts a third)."""
    return _weights(domain_counts(jd))


def domain_overlap(text: str, weights: dict[str, float]) -> float:
    """Sum of the JD domain weights of the groups `text` names (each group once)."""
    return sum(w for g, w in weights.items() if g in domain_groups(text))


# ---- the same for ranking CV content: company domains plus the topics (behaviour, segmentation, validation, statistics)
def rank_groups(text: str) -> set[str]:
    """The domain and topic groups `text` names."""
    return set(_counts(text, _RANK_GROUP))


def jd_rank_weights(jd: str) -> dict[str, float]:
    """`jd_domain` with the topics: {group: weight} for every domain or topic group the JD names."""
    return _weights(_counts(jd, _RANK_GROUP))


def rank_overlap(text: str, weights: dict[str, float]) -> float:
    groups = rank_groups(text)
    return sum(w for g, w in weights.items() if g in groups)
