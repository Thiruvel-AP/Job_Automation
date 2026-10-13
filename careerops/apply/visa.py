"""Per-country visa answers: where the work is done (discovery/workcountry.py: W) decides what the forms are told.

applicant.yaml:
  visa:
    home: GB                              # required; remote work with no restriction is done "at home"
    remote:
      label: "Remote"                     # the only wording shown for a remote role at home (review screen, forms): never the country
      summary: "..."                      # optional: the form's free text for a remote role at home, instead of the home block's
    by_country:
      GB:      {right_to_work: true, sponsorship_now: false, sponsorship_future: true, summary: "..."}     # required: the home block
      IE:      {...}                      # optional: a block for exactly this country (or region key: EU, EMEA ...)
      default: {right_to_work: false, sponsorship_now: true, sponsorship_future: true, summary: "... {country} ..."}

Answers for W = `by_country[W]` when there is one, else `by_country.default`. `{country}` in a summary is replaced by what is shown for W:
the remote label when W is home and the role is remote, else the country's name. W unknown → no plan: the job's status is `needs_country`
and the apply assist refuses it. The assist fills the right-to-work / sponsorship questions from the three booleans only (a missing or
non-boolean value is left to you) and still prints CHECK VISA ANSWER for every visa field.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..discovery import countries, workcountry
from ..discovery.workcountry import Work
from .errors import ArchiveError

KEYS = ("right_to_work", "sponsorship_now", "sponsorship_future")
LABELS = {"right_to_work": "right to work", "sponsorship_now": "sponsorship now", "sponsorship_future": "sponsorship in the future"}


class WorkCountryError(ArchiveError):
    """The apply assist must not open a form: the work location is unknown, or applicant.yaml has no answers for it."""


@dataclass(frozen=True)
class VisaPlan:
    work: Work
    source: str                                   # "by_country.IE" | "by_country.default" | "the home block"
    answers: Mapping[str, bool | None] = field(default_factory=dict)
    summary: str = ""

    def answer(self, key: str) -> str | None:
        """'Yes' / 'No' for a boolean answer; None when it is not a boolean (the question is left to you)."""
        v = self.answers.get(key)
        return None if not isinstance(v, bool) else ("Yes" if v else "No")

    def describe(self) -> str:
        have = " · ".join(f"{LABELS[k]} {self.answer(k) or 'ASK'}" for k in KEYS)
        return f"Visa answers ({self.source}): {have}"


def by_country(applicant: Mapping[str, Any] | None) -> dict:
    return dict(((applicant or {}).get("visa") or {}).get("by_country") or {})


def plan_for(applicant: Mapping[str, Any] | None, work: Work) -> VisaPlan | None:
    """The answers for W, or None when W is unknown or applicant.yaml has neither a block for it nor a default."""
    if not work.country:
        return None
    blocks = by_country(applicant)
    block, key = next(((b, k) for k, b in blocks.items() if k != "default" and isinstance(b, dict)
                       and countries.place_code(k) == work.country), (None, ""))
    source = f"by_country.{key}"
    if block is None and isinstance(blocks.get("default"), dict):
        block, source = blocks["default"], "by_country.default"
    if block is None:
        return None
    if work.home_remote:
        source = "the home block" if source != "by_country.default" else source      # a remote role at home never names the home country
    summary = block.get("summary")
    if work.home_remote:                                                   # a remote role at home may have a summary of its own
        summary = (((applicant or {}).get("visa") or {}).get("remote") or {}).get("summary") or summary
    return VisaPlan(work, source, {k: block.get(k) for k in KEYS},
                    " ".join(str(summary or "").replace("{country}", work.shown).split()))


def work_for(job, applicant: Mapping[str, Any] | None) -> Work:
    return workcountry.for_job(job, applicant)


def require_plan(job, applicant: Mapping[str, Any] | None) -> VisaPlan:
    """The visa plan of a job about to be applied to; raises WorkCountryError (one line) when W is unknown or has no answers."""
    work = work_for(job, applicant)
    if not work.country:
        raise WorkCountryError(f"the work location of this job is unknown ({work.reason or 'not stated'}): give it with "
                               "`careerops set-country --id JOB --country XX`; nothing was opened")
    plan = plan_for(applicant, work)
    if plan is None:
        raise WorkCountryError(f"applicant.yaml has no visa.by_country block for {work.shown} and no visa.by_country.default; nothing was opened")
    return plan


def review_lines(job, applicant: Mapping[str, Any] | None) -> list[str]:
    """The lines the review screen shows: W with its evidence, and the answers that will be given."""
    work = work_for(job, applicant)
    lines = [work.line()]
    plan = plan_for(applicant, work)
    if plan is not None:
        lines.append(plan.describe())
    elif work.country:
        lines.append(f"Visa answers: NONE (no visa.by_country block for {work.shown} and no default in applicant.yaml)")
    return lines


def displayed_location(job, applicant: Mapping[str, Any] | None) -> str:
    """A job's location as it may be shown: the remote label for a remote role at home, else the location the ad gave."""
    return workcountry.shown_location(work_for(job, applicant), str(job["location"] if "location" in job.keys() else "") or "") or ""
