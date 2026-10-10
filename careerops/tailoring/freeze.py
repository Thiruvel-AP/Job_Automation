"""Keywords frozen per job. The first extraction of a job (keywords, context terms, company hook, team) is stored in the job
record (`jobs.analysis`, JSON) and every later tailoring of that job reuses it with no model call, so re-tailoring a job
(`live --retailor`, a retry after an error) cannot change the keyword list, the ceiling or the ATS score by luck of the
extraction. `--re-extract` redoes the extraction. A stored analysis is not used when the job's JD has changed since it was
frozen (its terms may no longer be in the JD).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Mapping

from .scorer import Keyword

VERSION = 1


def jd_digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def record(kws: list[Keyword], hook: str, team: str, context: list[dict], notes: list[str], jd: str) -> dict[str, Any]:
    """The analysis as stored in `jobs.analysis`: what `analyse` returned for this JD, and the digest of that JD."""
    return {"version": VERSION, "frozen": date.today().isoformat(), "jd_sha1": jd_digest(jd),
            "keywords": [{"term": k.term, "weight": k.weight, "required": k.required, **({"alts": k.alts} if k.alts else {})}
                         for k in kws],
            "context_terms": list(context), "company_hook": hook, "team": team, "notes": list(notes)}


def frozen_for(job: Mapping[str, Any]) -> tuple[dict | None, str]:
    """(frozen analysis for `tailor_job(frozen=...)`, why-not). The first is None when the job has no stored analysis
    (why-not is "") or the stored one cannot be used (why-not says why: the JD changed, or the record is unreadable)."""
    raw = job["analysis"] if "analysis" in job.keys() else None
    if not raw:
        return None, ""
    try:
        data = json.loads(raw)
        kws = [Keyword(str(k["term"]), float(k["weight"]), bool(k["required"]), [str(a) for a in k.get("alts") or []])
               for k in data["keywords"]]
        digest = data.get("jd_sha1")
        frozen = {"keywords": kws, "company_hook": data.get("company_hook") or "", "team": data.get("team") or "",
                  "notes": list(data.get("notes") or []), "context_terms": list(data.get("context_terms") or []),
                  "date": data.get("frozen") or "?"}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return None, f"the stored keywords are unreadable ({type(e).__name__})"
    if digest != jd_digest(job["description"] or ""):
        return None, "the JD changed since its keywords were frozen"
    return frozen, ""
