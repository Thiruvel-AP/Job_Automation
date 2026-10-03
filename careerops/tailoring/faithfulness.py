"""Faithfulness guard: deterministic checks that generated text adds no facts the fact bank lacks.

Hard violations (text is rejected and regenerated, then falls back to the canonical bullet):
  * a deny-listed term (your confirmed-absent skills, standing exclusions)
  * a number not present in the source        ("cut latency 40%" when the source has no 40)
  * a JD keyword the source does not evidence  (source text + its `terms`)
Soft warnings (shown at review): capitalised names that appear neither in the fact bank nor the JD.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .scorer import Keyword, _contains, norm_literal, norm_stem

NUM = re.compile(r"(?<![\w.])[£$€]?\d[\d,]*(?:\.\d+)?\s*(?:%|k|m|x|ms|s)?", re.I)


def numbers(text: str) -> set[str]:
    return {re.sub(r"[,\s£$€]", "", m.group(0)).lower() for m in NUM.finditer(text)}


@dataclass
class Check:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def deny_hits(text: str, deny: list[str]) -> list[str]:
    lt = norm_literal(text)
    return [d for d in deny if _contains(lt, norm_literal(d))]


def check(new: str, source: str, evidence_terms: list[str], deny: list[str],
          keywords: list[Keyword], vocabulary: str = "", max_growth: float = 1.6) -> Check:
    """`source` = what the text is allowed to be built from; `vocabulary` = fact bank + JD, for name warnings."""
    c = Check()
    src_all = source + " " + " ".join(evidence_terms)
    for d in deny_hits(new, deny):
        if d not in deny_hits(source, deny):
            c.violations.append(f"deny-listed term '{d}'")
    extra = numbers(new) - numbers(src_all)
    if extra:
        c.violations.append(f"new numbers {sorted(extra)}")
    stem_src, stem_new = norm_stem(src_all), norm_stem(new)
    for k in keywords:
        for w in k.members():                            # an any-of alternative is only usable if it is evidenced itself
            ks = norm_stem(w)
            if _contains(stem_new, ks) and not _contains(stem_src, ks):
                c.violations.append(f"keyword '{w}' not evidenced by source")
    if len(new) > max_growth * max(len(source), 40):
        c.violations.append(f"text grew {len(new) / max(len(source), 1):.1f}x — likely padding")
    if vocabulary:
        voc = norm_literal(vocabulary + " " + src_all)
        for m in re.finditer(r"(?<=[a-z,;:] )([A-Z][A-Za-z0-9]+(?:\s[A-Z][A-Za-z0-9]+)*)", new):
            name = m.group(1)
            if norm_literal(name) not in voc:
                c.warnings.append(f"unrecognised name '{name}'")
    return c
