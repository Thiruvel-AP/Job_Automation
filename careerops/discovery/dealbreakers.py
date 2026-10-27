"""Your dealbreakers (`targeting.dealbreakers`) against an ad: a hit needs an ad SENTENCE that states the fact, and the sentence is quoted:

    DEALBREAKER "people management" ← "You will line-manage a team of 4"

Nothing is inferred. A senior-sounding title with no stated years, a missing salary, a "Lead" in the title: none of them is a hit. A sentence that denies the
fact ("There is no people management"), or calls it optional ("security clearance desirable"), is not a hit either.

How a phrase is read:
  * a few known ideas are read by what an ad says about them, not by your exact words: people management (line-manage, direct reports, "manage a team"),
    clearance (SC / DV / CTC / BPSS, "security clearance"), on-site days ("5 days on-site", with "outside the UK" if you add it), "N+ years" of experience,
    and "salary below £N" (the TOP of a band the ad states; no band, no hit);
  * any other phrase is matched as words in a sentence ("weekend work", "on-call"), nothing more.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NEGATION = re.compile(r"\b(?:no|not|without|never|neither|nor|none|don'?t|doesn'?t|isn'?t|aren'?t|won'?t|unnecessary|n’t)\b", re.I)
OPTIONAL = re.compile(r"\b(?:desirable|optional|nice to have|a plus|bonus|ideally|advantageous|beneficial|preferred)\b", re.I)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_N = r"(\d{1,2}|one|two|three|four|five)"
MANAGEMENT = re.compile(r"line[- ]manag\w*|manag\w+ (?:a |the |our |your )?(?:team|people|engineers|analysts|scientists|staff|direct reports)|"
                        r"people[- ]manage(?:r|ment)|direct reports?|managing (?:a )?(?:team|people)|(?:lead|leading) (?:a )?team of \d+|"
                        r"responsible for (?:the )?(?:management|managing) of (?:a )?team", re.I)
CLEARANCE = re.compile(r"\b(?:SC|DV|CTC|BPSS|NPPV)\s+clear\w*|\bsecurity[- ]clear\w*|\bclearance\b", re.I)
ONSITE_DAYS = re.compile(rf"{_N}\s+days?\s+(?:a|per|each)\s+week\s+(?:in|at|on)\s+(?:the\s+)?(?:office|site|-?site)|"
                         rf"{_N}\s+days?\s+(?:on[- ]?site|in[- ]?(?:the[- ])?office)|(?:in|at)\s+the\s+(?:office|site)\s+{_N}\s+days?|"
                         r"fully\s+(?:on[- ]?site|office[- ]based)", re.I)
YEARS_STATED = re.compile(rf"{_N}\s*(?:\+|plus)?\s*years?\b", re.I)


@dataclass(frozen=True)
class Hit:
    phrase: str                 # the dealbreaker, as you wrote it
    sentence: str               # the ad sentence that states it

    def line(self) -> str:
        return f'DEALBREAKER "{self.phrase}" ← "{self.sentence}"'


def sentences(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        for s in re.split(r"(?<=[.!?])\s+", line):
            s = re.sub(r"^\s*(?:[-*•·]|\d+[.)])\s*", "", s).strip()
            if s:
                out.append(s[:240])
    return out


def _count(token: str) -> int:
    return NUMBER_WORDS.get(token.lower()) or int(token)


def _states(rx: re.Pattern, sentence: str) -> bool:
    """Does the sentence state the fact the pattern describes: the pattern is in it, and no negation comes before it and it is not called optional."""
    m = rx.search(sentence)
    return bool(m) and not NEGATION.search(sentence[:m.start()]) and not OPTIONAL.search(sentence)


def _money(token: str) -> int | None:
    m = re.fullmatch(r"£?\s?(\d{1,3}(?:,\d{3})+|\d+)(k)?", token.strip().lower().replace(" ", ""))
    return int(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1) if m else None


def find(phrases, text: str, *, top_pay: int | None = None, outside_home: bool = False) -> Hit | None:
    """The first dealbreaker the ad states, with its sentence, else None. `top_pay` is the top of the salary band the ad states (None: it states none);
    `outside_home` says the work is done outside your home country (for "... outside the UK")."""
    sents = sentences(text)
    for phrase in phrases:
        p = " ".join(str(phrase).lower().split())
        if not p:
            continue
        hit = _one(p, str(phrase).strip(), sents, top_pay, outside_home)
        if hit is not None:
            return hit
    return None


def _one(p: str, phrase: str, sents: list[str], top_pay: int | None, outside_home: bool) -> Hit | None:
    if m := re.search(r"salary\s+(?:below|under|less than|lower than|<)\s*(£?\s?[\d,]+k?)", p):
        floor = _money(m.group(1))
        if floor is None or top_pay is None or top_pay >= floor:
            return None                                                 # no stated band: a missing salary never breaks a floor
        return Hit(phrase, next((s for s in sents if re.search(r"£\s?\d", s)), "")[:240] or f"the ad's top of band is £{top_pay:,}")
    if m := re.search(rf"{_N}\s*\+?\s*years|more than {_N} years", p):
        more = p.startswith("more than") or "more than" in m.group(0)
        n = _count(next(g for g in m.groups() if g))
        for s in sents:
            if re.search(r"experience|exp\.", s, re.I):
                for y in YEARS_STATED.finditer(s):
                    k = _count(y.group(1))
                    if (k > n if more else k >= n) and not NEGATION.search(s[:y.start()]) and not OPTIONAL.search(s):
                        return Hit(phrase, s)
        return None
    if re.search(r"on[- ]?site|in the office|office days|days on", p) and re.search(r"\d|one|two|three|four|five", p):
        need = _count(re.search(_N, p).group(1))
        if "outside the uk" in p and not outside_home:
            return None
        for s in sents:
            m = ONSITE_DAYS.search(s)
            if m and not NEGATION.search(s[:m.start()]) and not OPTIONAL.search(s):
                days = _count(next((g for g in m.groups() if g), "5")) if any(m.groups()) else 5
                if days >= need:
                    return Hit(phrase, s)
        return None
    if re.search(r"people[- ]?manage|line[- ]?manage|manage (?:a )?(?:team|people)|direct reports?|managing", p):
        return next((Hit(phrase, s) for s in sents if _states(MANAGEMENT, s)), None)
    if re.search(r"clearance|\b(?:sc|dv|ctc|bpss|nppv)\b", p):
        return next((Hit(phrase, s) for s in sents if _states(CLEARANCE, s)), None)
    words = r"[\s-]+".join(re.escape(w) for w in re.split(r"[\s-]+", p))
    rx = re.compile(rf"(?<![\w]){words}(?![\w])", re.I)
    return next((Hit(phrase, s) for s in sents if _states(rx, s)), None)
