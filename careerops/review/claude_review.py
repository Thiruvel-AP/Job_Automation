"""Claude on the review screen (v3.5 + v3.5a): the review row of a version is queued, Claude is asked once per row (careerops/judge.py),
its answer goes through the same `decision_queue.decide` as `careerops decide --by claude`, and the screen says who decided.

In judge.mode `live`, `careerops live` carries out a final decision (yours from `careerops decide --by user`, else Claude's), unless it
must come to you: Claude said needs_human or answered with low confidence, a code guard turned its send into park, it disagrees with the
second-opinion judge (review.judge: one says send, the other would not send), or a send needs a typed confirmation (a fallback letter,
no letter, a failed document check). Then the usual prompt follows, with Claude's reasons at the top.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import decision_queue as dq


def ask(settings, session, job: Mapping[str, Any], rep: Mapping[str, Any], fb: Mapping[str, Any], row: dq.Row) -> tuple[dict | None, str]:
    """Claude's decision line on `row`, or (None, why Claude gave none). Asked only when the row has no Claude decision yet."""
    if session is None:
        return None, "no judge session in this run"
    blockers = dq.send_blockers(settings, job, rep, fb)
    payload = dq.review_context(settings, job, rep, fb, row, blockers=blockers)
    res = session.decide(dq.REVIEW, payload, job_id=row.job_id, qid=row.qid)
    if not res.ok:
        return None, res.reason
    a = res.answer or {}
    if a.get("decision") == "needs_human":
        line = dq.decide(settings, row, "park", by="claude", confidence="low", reasons=["needs a human: " + "; ".join(a.get("reasons") or [])],
                         source="claude -p")
        return line, ""
    line = dq.decide(settings, row, a["decision"], by="claude", confidence=a["confidence"], reasons=a.get("reasons") or [],
                     blockers=blockers if a["decision"] == "send" else (), unsupported=a.get("unsupported_claims") or [],
                     source="claude -p")
    return line, ""


def escalation(d: Mapping[str, Any], verdict) -> str:
    """Why a final decision must still come to you ('' = it may be carried out). Only Claude's decisions are escalated: yours stand."""
    if d.get("by") != "claude":
        return ""
    if verdict is not None and getattr(verdict, "would_send", None) is not None:
        claude_send = d.get("option") == "send"
        if bool(verdict.would_send) != claude_send:
            other = "would send" if verdict.would_send else "would not send"
            return f"Claude says {d.get('option')}, the second-opinion judge {other}"
    return ""


def screen_lines(st: dq.State, why_none: str = "") -> list[str]:
    """The lines at the top of the review screen: who decided this version, or why Claude did not."""
    out = []
    u, c = st.latest("user"), st.latest("claude")
    if c is not None:
        out.append("  " + dq.line_of(c))
    elif why_none:
        out.append(f"  decided by: you (claude unavailable: {why_none})")
    if u is not None:
        out.append("  " + dq.line_of(u) + "  (careerops decide)")
    return out
