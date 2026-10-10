"""LLM judge for the review screen: would a careful applicant send this CV and letter? (`review.judge: off | shadow | gate`)

The judge is a second opinion from a model of a DIFFERENT family than the one that wrote the documents. It reads the JD, the
CV text, the letter (if any) and the fact-bank bullets the documents rest on, and answers {"would_send": bool, "score": 1-5,
"issues": [...]}. It is read-only: it never edits a file, a report or a database row.

  off     no judge.
  shadow  (default) the verdict is shown on the review screen and logged next to YOUR decision in paths.judge/log.csv.
          Nothing else changes; `careerops judge-report` says how often the judge and you agree.
  gate    a judge "yes" approves the job without the review prompt. Refused in code (GateRefused) until the log holds
          GATE_MIN_LABELS human-labelled reviews and none of them is a false approve (the judge said yes, you said no).
          A judge "no" (or no verdict at all) still comes to you. A fallback letter or a missing letter is never
          auto-approved either: those keep their deliberate confirmation. The judge never touches visa answers or submit.

The model is `llm.models.judge` ({provider, model}) and `llm.task_providers.judge` pins it to that provider, so a failed call
cannot be answered by another model. `check_config` and `check_family` refuse a judge of the generator's family. Shipped: the
local Ollama `qwen2.5-7b-8k` (a different family from the gpt-oss generator; nothing leaves the machine).

A local model has a small context (num_ctx 8192), and Ollama cuts a longer prompt silently. So the input is fitted to the
provider's `context_tokens` minus the answer's `max_tokens` before it is sent: the JD is cut to its role / requirements sections
(`jdtext.role_sections`), then to the room left; if the CV, letter and facts alone do not fit, the judge gives no verdict. The
tokens sent are shown on the review screen, kept in the verdict, the log and the events table.

The judge never blocks a review: the router tries the call twice (an unreachable server, or an answer that is not JSON, is
retried once), then the verdict is "no verdict" with the reason, which is logged beside your decision like any other.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..db import now
from ..llm.pacing import CHARS_PER_TOKEN, estimate_tokens
from ..llm.router import DEFAULT_MAX_TOKENS, LLMError, TaskUnavailable
from ..reporting.outcomes import wilson
from ..tailoring.jdtext import role_sections
from ..tailoring.tailor import bullet_index

MODES = ("off", "shadow", "gate")
GATE_MIN_LABELS = 60               # human-labelled reviews the log needs before gate mode may run (not a setting: a config
                                   # file cannot loosen it)
JD_CHARS = 9000                    # a longer JD is cut here before it is sent, whatever the model's context
CTX_MARGIN = 300                   # tokens of a provider's context kept free: the estimate is a character count, not a tokenizer
MIN_JD_CHARS = 1750                # facts are dropped before the JD is cut below this (~500 tokens)
JD_CUT = "\n[JD cut here]"
LOG_COLUMNS = ["ts", "job_id", "company", "title", "mode", "judge_model", "judge_would_send", "judge_score",
               "judge_issues", "judge_error", "human", "reason", "letter_status", "verdict_key", "judge_input_tokens"]
HUMAN = ("approve", "reject")      # a labelled decision; "auto" (gate) is logged but is not a label


class GateRefused(RuntimeError):
    """review.judge is `gate` but the log does not yet earn it. The message says what is missing."""


# ------------------------------------------------------------------ configuration
def judge_mode(settings) -> str:
    raw = (settings.raw.get("review") or {}).get("judge", "shadow")
    mode = "off" if raw is False else str(raw).strip().lower()                       # YAML reads an unquoted `off` as False
    if mode not in MODES:
        raise ValueError(f"review.judge must be one of {', '.join(MODES)}, got {mode!r}")
    return mode


def log_path(settings) -> Path:
    return settings.path("judge") / "log.csv"


_FAMILIES = (("openai", r"^(gpt|chatgpt|o\d)"), ("llama", r"llama"), ("qwen", r"qwen|qwq"), ("mistral", r"mistral|mixtral|ministral"),
             ("google", r"gemma|gemini"), ("deepseek", r"deepseek"), ("anthropic", r"claude"), ("phi", r"^phi"))


def model_family(model: str) -> str:
    """A coarse family for a model id ('openai/gpt-oss-20b' -> 'openai', 'llama-3.3-70b-versatile' -> 'llama'). The provider
    prefix is ignored, so Groq's 'openai/gpt-oss-120b' is a gpt-oss model, not a different vendor's."""
    name = str(model).lower().rsplit("/", 1)[-1].strip()
    for fam, pattern in _FAMILIES:
        if re.search(pattern, name):
            return fam
    return re.split(r"[-:_.\d]", name, maxsplit=1)[0] or name


def judge_model(settings) -> dict | None:
    """llm.models.judge as {provider, model}, or None when it is not set."""
    m = (settings["llm"].get("models") or {}).get("judge")
    return {"provider": str(m["provider"]), "model": str(m["model"])} if isinstance(m, dict) and m.get("provider") and m.get("model") else None


def check_config(settings) -> list[str]:
    """What is wrong with the judge's configuration (empty = fine): no llm.models.judge, no provider pin, or a model of the
    generator's family (the first provider's model and every other llm.models entry)."""
    if judge_mode(settings) == "off":
        return []
    j = judge_model(settings)
    if j is None:
        return ["llm.models.judge is not set: name a model of another family than the generator, e.g. "
                "{provider: ollama, model: qwen2.5-7b-8k}"]
    out = []
    llm = settings["llm"]
    if (llm.get("task_providers") or {}).get("judge") != [j["provider"]]:
        out.append(f"set llm.task_providers.judge: [{j['provider']}] so that no other provider's model can answer for the judge")
    generators = {llm["providers"][0]["model"]} | {m["model"] for t, m in (llm.get("models") or {}).items()
                                                     if t != "judge" and isinstance(m, dict) and m.get("model")}
    clash = sorted(g for g in generators if model_family(g) == model_family(j["model"]))
    if clash:
        out.append(f"the judge model {j['model']} is the same family as the generator ({', '.join(clash)}): "
                   "pick a different family")
    return out


def check_family(judge_model_id: str, rep: dict) -> str | None:
    """Why this judge cannot judge THIS job: a model of its family wrote part of it (report.json -> llm_usage, the models
    that really answered, so a Groq fall-through to Ollama counts). None when it can."""
    fam = model_family(judge_model_id)
    used = sorted({str(c.get("model")) for c in (rep.get("llm_usage") or {}).get("calls") or [] if c.get("model")
                   and model_family(str(c["model"])) == fam})
    return (f"the judge ({judge_model_id}) is the same family as a model that wrote this job ({', '.join(used)})"
            if used else None)


# ------------------------------------------------------------------ the question
SYSTEM = """You are the last reader before a job application is sent. Judge whether the applicant should send this CV and
cover letter for this job. You only read; you never rewrite.

Check, in this order:
1. Honesty. Every tool, number, employer, outcome or responsibility in the CV and letter must be supported by SOURCE FACTS
   (the applicant's own record). A claim the facts do not support is a serious issue. A reworded bullet may change the wording,
   not the substance.
2. Fit. The CV leads with what this JD asks for, and does not hide or overstate gaps.
3. Letter (if there is one). It is the applicant's own plain first-person prose: not a pasted bullet, no vague praise
   ("robust", "cutting-edge"), no claim beyond SOURCE FACTS, specific to this company.
4. Anything a hiring manager would wince at: wrong company or role, leftover template text, repetition, broken sentences.

Return JSON only: {"would_send": true or false, "score": 1-5, "issues": ["short, specific, one problem each"]}
score 5 = send as it is, 4 = send, a nit or two, 3 = needs edits first, 2 = clear problems, 1 = do not send.
would_send is true only for a 4 or 5. issues is empty only when there is nothing to fix."""

USER = """JOB: {title} at {company}

JOB DESCRIPTION:
{jd}

CV (text; the contact block is left out):
{cv}

COVER LETTER:
{letter}

SOURCE FACTS (the applicant's own bullets the CV and letter rest on):
{facts}"""


def cv_text(ctx: dict) -> str:
    """The CV as plain text from its render context: profile, skills, experience, projects, education. The contact block
    and the name are not part of it (they never leave the machine)."""
    lines = [str(ctx.get("profile") or "").strip(), ""]
    skills = ctx.get("skills") or {}
    if skills:
        lines.append("SKILLS")
        lines += [f"{group}: {', '.join(map(str, items))}" for group, items in skills.items()]
        lines.append("")
    for title, section in (("EXPERIENCE", "experience"), ("PROJECTS", "projects")):
        entries = ctx.get(section) or []
        if entries:
            lines.append(title)
        for e in entries:
            lines.append(" | ".join(str(e[k]) for k in ("title", "name", "org", "dates") if e.get(k)))
            lines += [f"- {b}" for b in e.get("bullets") or []]
    if ctx.get("education"):
        lines += ["", "EDUCATION"]
        lines += [", ".join(str(v) for v in e.values() if isinstance(v, (str, int))) for e in ctx["education"]]
    return "\n".join(lines).strip()


def build_input(job, rep: dict, fb: dict) -> dict:
    """Everything the judge is shown: {title, company, jd, cv, letter, facts}. `facts` are the fact-bank bullets on the CV
    plus the letter's evidence bullets (with the result, when the bank has one)."""
    ctx, letter = rep["context"], rep.get("letter") or {}
    bidx = bullet_index(fb)
    ids = [i for sec in ("experience", "projects") for e in ctx[sec] for i in e.get("bullet_ids", [])]
    ids += [e["id"] for e in letter.get("evidence") or [] if e["id"] not in ids]
    facts = []
    for i in ids:
        b = bidx.get(i)
        if b:
            facts.append(f"[{i}] {b['text']}" + (f" Result: {b['result']}" if str(b.get("result") or "").strip() else ""))
    paragraphs = letter.get("paragraphs") or [] if letter.get("status") in ("ok", "fallback") else []
    jd = str(job["description"] or "")
    return {"title": job["title"], "company": job["company"],
            "jd": jd if len(jd) <= JD_CHARS else jd[:JD_CHARS] + JD_CUT,
            "cv": cv_text(ctx), "letter": "\n\n".join(paragraphs) if paragraphs else "(no cover letter)",
            "facts": "\n".join(facts) or "(none)"}


def judge_limits(settings) -> tuple[int | None, int]:
    """(context_tokens of the judge's provider or None when it names none, the answer's max_tokens)."""
    llm = settings["llm"]
    j = judge_model(settings)
    prov = next((p for p in llm["providers"] if j and p["name"] == j["provider"]), {})
    mt = llm.get("max_tokens") or {}
    return prov.get("context_tokens"), int(mt.get("judge") or mt.get("default") or DEFAULT_MAX_TOKENS)


def input_budget(settings) -> int | None:
    """Tokens the prompt may take on the judge's provider: its context minus the answer and a margin. None = no limit known."""
    ctx, answer = judge_limits(settings)
    return max(int(ctx) - answer - CTX_MARGIN, 0) if ctx else None


def prompt_tokens(inp: dict) -> int:
    return estimate_tokens(SYSTEM, USER.format(**inp))


def prepare_input(job, rep: dict, fb: dict, budget: int | None) -> tuple[dict, dict]:
    """(the judge's input fitted to `budget` tokens, what was done to it). The JD is cut to its role / requirements sections
    first, then to the room the CV, letter and facts leave; when even a short JD does not fit, the facts are dropped from the
    end (those of the last bullets) before the JD goes below MIN_JD_CHARS. info = {tokens, jd_chars, jd_sent, notes, error}:
    `error` is set, and the input is not to be sent, when the CV, letter and system prompt alone exceed the budget."""
    full = str(job["description"] or "")
    jd, trimmed_note = role_sections(full)
    inp = build_input({"title": job["title"], "company": job["company"], "description": jd}, rep, fb)
    info = {"jd_chars": len(full), "notes": [trimmed_note] if trimmed_note else [], "error": ""}
    facts = inp["facts"].split("\n") if inp["facts"] != "(none)" else []
    dropped = 0
    if budget is not None:
        while prompt_tokens(inp) > budget:
            over = int((prompt_tokens(inp) - budget) * CHARS_PER_TOKEN) + len(JD_CUT)
            body = inp["jd"].removesuffix(JD_CUT)
            if len(body) - over >= MIN_JD_CHARS:
                inp["jd"] = body[: len(body) - over].rstrip() + JD_CUT
            elif len(facts) > 1:
                facts.pop()
                dropped += 1
                inp["facts"] = "\n".join(facts)
                continue
            elif len(body) > MIN_JD_CHARS:
                inp["jd"] = body[:MIN_JD_CHARS].rstrip() + JD_CUT
            else:
                info["error"] = (f"the CV, letter and facts need ~{prompt_tokens(inp):,} tokens but the judge's context leaves "
                                 f"{budget:,}: too long to judge on this model")
            break
    if inp["jd"].endswith(JD_CUT):
        info["notes"].append(f"JD cut to {len(inp['jd']):,} characters")
    if dropped:
        info["notes"].append(f"{dropped} source fact(s) left out")
    info["tokens"], info["jd_sent"] = prompt_tokens(inp), len(inp["jd"])
    return inp, info


@dataclass
class Verdict:
    would_send: bool | None = None         # None = no verdict (see `error`)
    score: int | None = None
    issues: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""
    key: str = ""
    tokens: int | None = None              # prompt tokens: what the provider reported, else the estimate
    note: str = ""                         # what was cut from the input to fit the model's context

    @property
    def yes(self) -> bool:
        return self.would_send is True

    def line(self) -> str:
        if self.would_send is None:
            return f"Judge: no verdict ({self.error or 'not run'})"
        return f"Judge ({self.model}): {'WOULD SEND' if self.would_send else 'WOULD NOT SEND'} {self.score}/5"

    def input_line(self) -> str:
        """The tokens the judge was sent and what was cut to fit, or "" when nothing is known."""
        if not self.tokens:
            return ""
        return f"Judge input: {self.tokens:,} prompt tokens" + (f" ({self.note})" if self.note else "")


def parse_verdict(out, model: str = "", key: str = "") -> Verdict:
    """A Verdict from the model's JSON. An answer that is not {would_send, score 1-5, issues} gives no verdict (error set).
    would_send true with a score under 4 contradicts itself: it counts as a no, and says so in `issues`."""
    if not isinstance(out, dict):
        return Verdict(model=model, key=key, error="the answer was not a JSON object")
    ws = out.get("would_send")
    if isinstance(ws, str):
        ws = {"true": True, "yes": True, "false": False, "no": False}.get(ws.strip().lower())
    score = out.get("score")
    if isinstance(score, str) and score.strip().isdigit():
        score = int(score)
    if not isinstance(ws, bool) or isinstance(score, bool) or not isinstance(score, (int, float)) or not 1 <= score <= 5:
        return Verdict(model=model, key=key, error=f"the answer lacks a boolean would_send and a score of 1-5: {str(out)[:120]}")
    issues = out.get("issues")
    issues = [str(i).strip() for i in issues if str(i).strip()] if isinstance(issues, list) else ([str(issues)] if issues else [])
    score = int(round(score))
    if ws and score < 4:
        ws = False
        issues.append(f"(judge said would_send with a score of {score}: counted as a no)")
    return Verdict(would_send=ws, score=score, issues=issues[:8], model=model, key=key)


def verdict_key(inp: dict, model: str) -> str:
    return hashlib.sha256(json.dumps([inp, model], sort_keys=True).encode("utf-8")).hexdigest()[:24]


def run_judge(llm, settings, job, rep: dict, fb: dict) -> Verdict:
    """Ask the judge about this job. Never raises: a judge that cannot answer is a Verdict with `error` and no opinion. A
    successful verdict is cached under paths.judge/verdicts/ by a hash of exactly what it was shown, so reopening a review
    does not spend a second call, and an edited CV or a regenerated letter is judged afresh."""
    problems = check_config(settings)
    if problems:
        return Verdict(error="; ".join(problems))
    j = judge_model(settings)
    model = f"{j['provider']}/{j['model']}"
    if llm is None:
        return Verdict(model=model, error="no LLM for this run")
    mock = not hasattr(llm, "providers")                # MockLLM (--mock): no real model, no family to compare
    if not mock:
        clash = check_family(j["model"], rep)
        if clash:
            return Verdict(model=model, error=clash)
    inp, info = prepare_input(job, rep, fb, input_budget(settings))
    note = "; ".join(n for n in info["notes"] if n)
    key = verdict_key(inp, "mock" if mock else model)
    cache = settings.path("judge") / "verdicts" / f"{key}.json"
    try:
        if cache.exists():
            return Verdict(**json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    db = getattr(llm, "db", None)

    def record(detail: dict) -> None:
        if db is not None:                              # the events table: tokens and failures stay inspectable
            try:
                db.log(job["id"], "judge", {"model": model, "input_tokens_est": info["tokens"], "jd_chars": info["jd_chars"],
                                            "jd_sent": info["jd_sent"], "note": note, **detail})
            except Exception:  # noqa: BLE001 — logging must never be the reason a review stops
                pass

    if info["error"]:
        record({"error": info["error"]})
        return Verdict(model=model, key=key, error=info["error"], tokens=info["tokens"], note=note)
    try:
        out = llm.json("judge", SYSTEM, USER.format(**inp), {"task": "judge", **inp})
    except TaskUnavailable as e:
        record({"error": f"unavailable: {str(e)[:200]}"})
        return Verdict(model=model, key=key, error=f"the judge model cannot take the call now: {str(e)[:200]}",
                       tokens=info["tokens"], note=note)
    except (LLMError, OSError, ValueError) as e:        # the router has already tried twice: no verdict, the review goes on
        record({"error": f"failed: {str(e)[:200]}"})
        return Verdict(model=model, key=key, error=f"the judge call failed: {str(e)[:200]}", tokens=info["tokens"], note=note)
    used = (getattr(llm, "usage", None) or [{}])[-1]
    v = parse_verdict(out, model="mock" if mock else f"{used.get('provider') or j['provider']}/{used.get('model') or j['model']}",
                      key=key)
    v.tokens, v.note = used.get("prompt_tokens") or info["tokens"], note
    record({"prompt_tokens": used.get("prompt_tokens"), "completion_tokens": used.get("completion_tokens"),
            "would_send": v.would_send, "error": v.error})
    if v.would_send is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(asdict(v)), encoding="utf-8")
        except OSError:
            pass
    return v


# ------------------------------------------------------------------ the log (append-only)
def log_decision(settings, job, rep: dict, verdict: Verdict | None, human: str, reason: str = "") -> None:
    """Append one row: the judge's verdict next to the decision made on that very version. `human` is approve | reject |
    auto (a gate approval, not a label). A review where the judge was never asked (no LLM, mode off) writes nothing."""
    if verdict is None:
        return
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    row = {"ts": now(), "job_id": job["id"], "company": job["company"], "title": job["title"], "mode": judge_mode(settings),
           "judge_model": verdict.model, "judge_would_send": "" if verdict.would_send is None else str(verdict.would_send).lower(),
           "judge_score": "" if verdict.score is None else verdict.score, "judge_issues": " | ".join(verdict.issues),
           "judge_error": verdict.error, "human": human, "reason": reason,
           "letter_status": (rep.get("letter") or {}).get("status", ""), "verdict_key": verdict.key,
           "judge_input_tokens": verdict.tokens or ""}
    fields = LOG_COLUMNS
    if not new:                                          # a log started before a column existed keeps its own header
        with open(path, encoding="utf-8", newline="") as f:
            fields = next(csv.reader(f), None) or LOG_COLUMNS
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ statistics
def stats(rows: list[dict]) -> dict:
    """Judge vs human over the labelled, judged rows. n, agreement, Cohen's kappa (None when chance agreement is 1, i.e. one
    side never varies), false approves (judge yes, human no), false rejects (judge no, human yes), plus the counts of rows that
    are not in n: a judge with no verdict, and gate auto-approvals (not human-labelled)."""
    labelled = [r for r in rows if r.get("human") in HUMAN]
    judged = [r for r in labelled if r.get("judge_would_send") in ("true", "false")]
    tp = sum(r["judge_would_send"] == "true" and r["human"] == "approve" for r in judged)
    fa = sum(r["judge_would_send"] == "true" and r["human"] == "reject" for r in judged)
    fr = sum(r["judge_would_send"] == "false" and r["human"] == "approve" for r in judged)
    tn = sum(r["judge_would_send"] == "false" and r["human"] == "reject" for r in judged)
    n = len(judged)
    agree = (tp + tn) / n if n else None
    kappa = None
    if n:
        pe = ((tp + fa) / n) * ((tp + fr) / n) + ((fr + tn) / n) * ((fa + tn) / n)
        kappa = (agree - pe) / (1 - pe) if pe < 1 else None
    return {"n": n, "agreement": agree, "kappa": kappa, "tp": tp, "fa": fa, "fr": fr, "tn": tn,
            "judge_yes": tp + fa, "unjudged": len(labelled) - n, "auto": sum(r.get("human") == "auto" for r in rows)}


def gate_status(rows: list[dict]) -> tuple[bool, str]:
    """(allowed, why not). Gate needs GATE_MIN_LABELS human-labelled, judged reviews and no false approve among them."""
    s = stats(rows)
    if s["fa"]:
        return False, (f"the log has {s['fa']} false approve(s) (judge yes, you no) in {s['n']} labelled reviews: gate needs none")
    if s["n"] < GATE_MIN_LABELS:
        return False, f"the log has {s['n']} human-labelled reviews, gate needs {GATE_MIN_LABELS} (and no false approve)"
    return True, ""


def require_gate(settings) -> None:
    """Raises GateRefused unless review.judge is `gate` AND the log earns it."""
    ok, why = gate_status(read_log(log_path(settings)))
    if not ok:
        raise GateRefused(f"review.judge is 'gate' but {why}. Set review.judge: shadow in config/settings.yaml and keep "
                          "reviewing; `careerops judge-report` shows the numbers.")


def format_report(rows: list[dict], path: Path) -> str:
    s = stats(rows)
    pct = lambda x: "n/a" if x is None else f"{x:.0%}"           # noqa: E731
    lines = [f"Judge vs your decisions ({path})", f"  labelled and judged reviews (n): {s['n']}"]
    if s["unjudged"] or s["auto"]:
        lines.append(f"  not in n: {s['unjudged']} labelled with no judge verdict, {s['auto']} gate auto-approval(s)")
    if not s["n"]:
        lines.append("  nothing to compare yet: review some jobs with review.judge: shadow.")
        return "\n".join(lines)
    lines += [f"  agreement: {pct(s['agreement'])}  ({s['tp'] + s['tn']} of {s['n']})",
              "  Cohen's kappa: " + ("n/a (the judge or you gave one answer every time)" if s["kappa"] is None else f"{s['kappa']:.2f}"),
              f"  judge yes / you yes: {s['tp']}   judge yes / you no (FALSE APPROVE): {s['fa']}",
              f"  judge no  / you yes (false reject): {s['fr']}   judge no / you no: {s['tn']}"]
    if s["fa"] == 0:
        lines.append(f"  false approves: 0 of {s['n']}. Rule of three: with 95% confidence the false-approve rate is below "
                     f"3/n = {min(3 / s['n'], 1):.1%}" + (f" (below {min(3 / s['judge_yes'], 1):.1%} of the judge's {s['judge_yes']} yes verdicts)"
                                                  if s["judge_yes"] else ""))
    else:
        lo, hi = wilson(s["fa"], s["n"])
        lines.append(f"  false approves: {s['fa']} of {s['n']} = {s['fa'] / s['n']:.1%} (95% Wilson interval {lo:.1%}-{hi:.1%})")
    ok, why = gate_status(rows)
    lines.append("  gate: " + ("allowed (review.judge: gate would run)" if ok else f"refused: {why}"))
    return "\n".join(lines)
