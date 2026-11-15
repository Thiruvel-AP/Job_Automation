"""ATS bands (settings.yaml -> apply.bands): what happens to a job after tailoring, decided on the STRICT CV score S (the CV's own text,
negation-aware: ats.AtsScores.strict) and the honest, claimable ceiling C (ats.AtsScores.ceiling), both in percent.

    S >= auto                       auto      the auto-apply gates run (apply/autogate.py); the letter is drafted
    review <= S < auto              review    the review prompt; the letter is drafted
    S < review and C >= rescue      rescue    the review prompt with the red flag "CV under-uses the fact bank (strict S, ceiling C)"; letter drafted
    S < review and C < rescue       skip      status `skipped (ats)`: no letter call, no form, no prompt; the JD is filed in jds_done/skipped/

The letter is drafted only AFTER this check, so a skipped job costs no letter tokens. Only the user edits the bands; nothing here lowers them.
`careerops retriage --ats` scores the `skipped (ats)` jobs again after the fact bank changes.
"""
from __future__ import annotations

from dataclasses import dataclass

ATS_SKIP = "skipped (ats)"                    # job status (and live's result status) of a job under the band
UNDER_USE = "CV under-uses the fact bank"     # the job flag of a rescued job
EPS = 1e-9                                    # a score of 49.9999999 from float arithmetic is 50


class BandsError(ValueError):
    pass


@dataclass(frozen=True)
class Bands:
    auto: float = 70.0
    review: float = 50.0
    rescue_ceiling: float = 60.0


def config(settings) -> Bands:
    """settings.yaml -> apply.bands. Absent: the defaults 70 / 50 / 60. A value that is not a number from 0 to 100, or a `review` above `auto`, is
    refused with its name."""
    raw = ((settings.raw.get("apply") or {}) if hasattr(settings, "raw") else {}).get("bands")
    if raw is None:
        return Bands()
    if not isinstance(raw, dict):
        raise BandsError(f"apply.bands is {raw!r}; it must be a mapping such as {{auto: 70, review: 50, rescue_ceiling: 60}}")

    def number(key: str, default: float) -> float:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
            raise BandsError(f"apply.bands.{key} is {v!r}; it must be a number from 0 to 100")
        return float(v)

    out = Bands(number("auto", Bands.auto), number("review", Bands.review), number("rescue_ceiling", Bands.rescue_ceiling))
    if out.review > out.auto:
        raise BandsError(f"apply.bands: review ({_n(out.review)}) must not be above auto ({_n(out.auto)})")
    return out


def num(x: float) -> str:
    """A score as it is printed: 32, 49.6."""
    return f"{round(float(x), 1):g}"


_n = num


def skip_line(strict: float, review: float, company: str, title: str) -> str:
    """`SKIPPED (ATS 32 < 50): Valarian | MLOps Engineer`: the one line live prints for a job under the band."""
    return f"SKIPPED (ATS {_n(strict)} < {_n(review)}): {company} | {title}"


@dataclass(frozen=True)
class Verdict:
    outcome: str              # auto | review | rescue | skip
    strict: float
    ceiling: float
    auto: float = Bands.auto
    review: float = Bands.review
    rescue_ceiling: float = Bands.rescue_ceiling

    @property
    def letter(self) -> bool:
        """A letter is drafted only for a job in review or auto."""
        return self.outcome != "skip"

    @property
    def flag(self) -> str:
        return f"{UNDER_USE} (strict {_n(self.strict)}, ceiling {_n(self.ceiling)})" if self.outcome == "rescue" else ""

    @property
    def reason(self) -> str:
        return f"ATS S < {_n(self.review)}"

    def skip_line(self, company: str, title: str) -> str:
        return skip_line(self.strict, self.review, company, title)

    def line(self) -> str:
        head = f"ATS band: strict {_n(self.strict)}, ceiling {_n(self.ceiling)} -> "
        if self.outcome == "auto":
            return head + f"auto ({_n(self.auto)} or more: the auto-apply gates run)"
        if self.outcome == "review":
            return head + f"review ({_n(self.review)} to under {_n(self.auto)})"
        if self.outcome == "rescue":
            return head + f"review, flagged '{UNDER_USE}' (strict under {_n(self.review)}, ceiling {_n(self.rescue_ceiling)} or more)"
        return head + f"skipped (under {_n(self.review)}, ceiling under {_n(self.rescue_ceiling)}): no letter, no form"

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "strict": self.strict, "ceiling": self.ceiling, "auto": self.auto, "review": self.review,
                "rescue_ceiling": self.rescue_ceiling, "flag": self.flag, "reason": self.reason if self.outcome == "skip" else ""}

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        return cls(d["outcome"], float(d["strict"]), float(d["ceiling"]), float(d["auto"]), float(d["review"]), float(d["rescue_ceiling"]))


def classify(strict: float, ceiling: float, b: Bands) -> Verdict:
    """The band of a job with strict CV score `strict` and honest ceiling `ceiling` (both percent)."""
    if strict + EPS >= b.auto:
        outcome = "auto"
    elif strict + EPS >= b.review:
        outcome = "review"
    elif ceiling + EPS >= b.rescue_ceiling:
        outcome = "rescue"
    else:
        outcome = "skip"
    return Verdict(outcome, strict, ceiling, b.auto, b.review, b.rescue_ceiling)


def demote(v: Verdict, warn: bool) -> Verdict:
    """v3.7: a warn lint (tailoring/numberlint.py) moves a job in the auto band into the review band; every other outcome is unchanged."""
    if warn and v.outcome == "auto":
        return Verdict("review", v.strict, v.ceiling, v.auto, v.review, v.rescue_ceiling)
    return v
