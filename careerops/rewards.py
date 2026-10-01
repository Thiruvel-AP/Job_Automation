"""Reward functions for Phase-2 RL fine-tuning of the bullet re-writer.

Signature follows TRL's GRPOTrainer convention: f(completions, **dataset_columns) -> list[float].
Dataset rows (export with `careerops export-prefs --grpo`) carry: keywords, source_text, evidence_terms, deny.

GRPO recap: for each prompt sample G completions, score them r_1..r_G, and use the group-relative
advantage  A_i = (r_i − mean(r)) / std(r)  in a clipped policy-gradient objective (+ optional KL to a
reference model). No critic network — the group mean is the baseline. That only works if the reward is
cheap and automatic, which is why it's these deterministic functions and not a human clicking approve.

Why a *band* reward: a monotone keyword reward is maximised by stuffing. Capping at the band top
makes stuffing strictly worse than landing inside the band.
"""
from __future__ import annotations

from .faithfulness import check
from .scorer import Keyword, score


def _text(c) -> str:
    if isinstance(c, list):            # conversational format: [{"role": "assistant", "content": ...}]
        return c[-1].get("content", "") if c else ""
    return str(c)


def _kws(raw) -> list[Keyword]:
    return [k if isinstance(k, Keyword) else Keyword(**k) for k in raw]


def band_reward(completions, keywords, band=(0.70, 0.85), **_) -> list[float]:
    lo, hi = band
    out = []
    for c, kw in zip(completions, keywords):
        s = score(_text(c), _kws(kw)).literal
        out.append(1.0 if lo <= s <= hi else -min(abs(s - lo), abs(s - hi)) * 4)
    return out


def faithfulness_reward(completions, source_text, evidence_terms, deny, keywords, **_) -> list[float]:
    out = []
    for c, src, ev, dn, kw in zip(completions, source_text, evidence_terms, deny, keywords):
        v = check(_text(c), src, ev, dn, _kws(kw)).violations
        out.append(0.5 if not v else -1.0 * min(len(v), 3))
    return out
