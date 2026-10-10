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


def _forms(word: str) -> set[str]:
    out = {word, word + "s", word + "es"}
    if word.endswith("y"):
        out.add(word[:-1] + "ies")
    return out


_WORD_GROUP: dict[str, str] = {}
for _group, _words in DOMAIN_GROUPS.items():
    for _w in _words:
        for _f in _forms(_w):
            _WORD_GROUP.setdefault(_f, _group)

_TOKEN = re.compile(r"[a-z]+")
JD_FULL_WEIGHT = 3          # a group named this many times in the JD counts in full; fewer counts in proportion


def domain_counts(text: str) -> dict[str, int]:
    """How many times each domain group is named in `text`: {"finance": 7, "customer": 4}. Hyphens split words
    ("bank-account" names bank); nothing here depends on case."""
    out: dict[str, int] = {}
    for w in _TOKEN.findall((text or "").lower()):
        g = _WORD_GROUP.get(w)
        if g:
            out[g] = out.get(g, 0) + 1
    return out


def domain_groups(text: str) -> set[str]:
    """The domain groups `text` names at all."""
    return set(domain_counts(text))


def jd_domain(jd: str) -> dict[str, float]:
    """The JD's domain as {group: weight}, weight 1/3, 2/3 or 1 by how often the JD names the group (a bank JD that says
    "bank" twenty times is about banking; one passing mention of "customers" counts a third)."""
    return {g: min(n, JD_FULL_WEIGHT) / JD_FULL_WEIGHT for g, n in domain_counts(jd).items()}


def domain_overlap(text: str, weights: dict[str, float]) -> float:
    """Sum of the JD domain weights of the groups `text` names (each group once)."""
    return sum(w for g, w in weights.items() if g in domain_groups(text))
