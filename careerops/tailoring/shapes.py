"""Read the model's JSON tolerantly. Models answer in shapes the prompt did not ask for (bullet objects where ids were
expected, keyword objects instead of strings, paragraph objects), and a pipeline that trusts the shape crashes on the
first one. These normalisers turn what the model sent into the shape the pipeline uses, drop what cannot be used, and
say what they dropped (the notes end up in report.json). None of them raises.
"""
from __future__ import annotations

import re

_ID_KEYS = ("id", "bullet_id", "project_id", "experience_id", "ref")
_REPHRASE_KEYS = ("rephrase", "rephrased", "reworded", "new_text", "text")     # an object's reworded bullet
_TERM_KEYS = ("term", "keyword", "name", "text")
_PARA_KEYS = ("text", "paragraph", "content", "body")


def _short(x) -> str:
    return repr(x)[:40]


def as_id(x) -> str | None:
    """An id as the model may write it: the id string, or an object carrying it ({"id": "a1", "text": ...})."""
    if isinstance(x, str):
        return x.strip() or None
    if isinstance(x, dict):
        for k in _ID_KEYS:
            if isinstance(x.get(k), str) and x[k].strip():
                return x[k].strip()
    return None


def _first_text(obj: dict, keys: tuple[str, ...]) -> str | None:
    return next((obj[k].strip() for k in keys if isinstance(obj.get(k), str) and obj[k].strip()), None)


def _id_keyed(d: dict) -> bool:
    """A dict whose keys are the ids ({"az1": [...]}), as opposed to one object describing a single bullet
    ({"id": "az1", "text": ...}): it names no id/text field itself and has at least one key."""
    return bool(d) and all(isinstance(k, str) and k.strip() for k in d) and not any(
        k in d for k in (*_ID_KEYS, *_REPHRASE_KEYS))


def _entry_ids(items, notes: list[str], what: str, texts: dict[str, str] | None = None) -> list[str]:
    """Ids from a list (or a single value) of strings/objects, or from the keys of an object ({"az1": [...]}). An
    object's reworded text (or an id-keyed object's string value, when it is not just the id again) goes into
    `texts[id]`. Anything without an id is dropped, with a note."""
    if isinstance(items, dict) and _id_keyed(items):             # {"az1": [...], "az3": [...]}: the ids are the keys
        items = [{"id": k, **({"text": v} if isinstance(v, str) and v.strip() and v.strip() != k else {})}
                 for k, v in items.items()]
        notes.append(f"{what}: ids were the keys of an object; read them")
    if isinstance(items, (str, dict)):
        items = [items]
    if not isinstance(items, list):
        if items:
            notes.append(f"{what}: expected a list of ids, got {type(items).__name__}; ignored")
        return []
    out, objects = [], False
    for it in items:
        i = as_id(it)
        if not i:
            notes.append(f"{what}: dropped {_short(it)} (no id)")
            continue
        if isinstance(it, dict):
            objects = True
            text = _first_text(it, _REPHRASE_KEYS) if texts is not None else None
            if text:
                texts[i] = text
        out.append(i)
    if objects:
        notes.append(f"{what}: entries were objects; read their ids")
    return out


def clean_proposal(prop, fb: dict | None = None) -> tuple[dict, list[str]]:
    """The model's tailor_cv answer as {"profile": str, "experience": {entry id: [bullet ids]}, "projects": [ids],
    "rephrase": {bullet id: text}}. Entries and bullets may be id strings or objects with id / bullet_id (and a
    rephrase / text, which moves to "rephrase"; it still goes through the faithfulness check). With the fact bank
    `fb`, unknown experience, bullet and project ids are dropped. Returns (proposal, notes), one note per drop."""
    notes: list[str] = []
    empty = {"profile": "", "experience": {}, "projects": [], "rephrase": {}}
    if not isinstance(prop, dict):
        return empty, ["answer was not an object"]
    exp, experience, texts = prop.get("experience"), {}, {}
    if isinstance(exp, list):                                   # [{"id": "ex1", "bullets": [...]}, ...]
        pairs = []
        for e in exp:
            if isinstance(e, dict) and as_id(e):
                pairs.append((as_id(e), e.get("bullets") or e.get("bullet_ids") or e.get("ids")
                              or e.get("selected") or []))
            else:
                notes.append(f"experience: dropped {_short(e)} (no id)")
    elif isinstance(exp, dict):
        pairs = list(exp.items())
    else:
        pairs = []
        if exp:
            notes.append(f"experience: expected a map of entry id to bullet ids, got {type(exp).__name__}; ignored")
    for eid, ids in pairs:
        if isinstance(eid, str):
            experience[eid] = _entry_ids(ids, notes, f"experience {eid}", texts)
        else:
            notes.append(f"experience: dropped entry {_short(eid)} (no id)")
    projects = _entry_ids(prop.get("projects") or [], notes, "projects")
    reph, rephrase = prop.get("rephrase"), {}
    if isinstance(reph, list):                                  # [{"id": "a1", "text": "..."}, ...]
        reph = {as_id(r): _first_text(r, _REPHRASE_KEYS) for r in reph if isinstance(r, dict) and as_id(r)}
    for bid, text in (reph.items() if isinstance(reph, dict) else []):
        if isinstance(bid, str) and isinstance(text, str):
            rephrase[bid] = text
        else:
            notes.append(f"rephrase {_short(bid)}: not text; dropped")
    for bid, text in texts.items():
        rephrase.setdefault(bid, text)                          # an explicit "rephrase" entry wins over an object's
    profile = prop.get("profile")
    if profile is not None and not isinstance(profile, str):
        notes.append("profile: not text; dropped")
        profile = ""
    out = {"profile": profile or "", "experience": experience, "projects": projects, "rephrase": rephrase}
    if fb is not None:
        _validate(out, fb, notes)
    return out, notes


def _validate(out: dict, fb: dict, notes: list[str]) -> None:
    """Keep only ids the fact bank has (and bullets under the entry they belong to); note each drop."""
    own = {e["id"]: {b["id"] for b in e["bullets"]} for e in fb["experience"]}
    projects = {p["id"]: {b["id"] for b in p["bullets"]} for p in fb["projects"]}
    experience = {}
    for eid, ids in out["experience"].items():
        if eid not in own:
            notes.append(f"dropped unknown experience id '{eid}'")
            continue
        keep = []
        for i in ids:
            if i in own[eid]:
                keep.append(i)
            else:
                notes.append(f"dropped unknown bullet id '{i}' for experience {eid}")
        experience[eid] = list(dict.fromkeys(keep))
    out["experience"] = experience
    keep = []
    for p in out["projects"]:
        if p in projects:
            keep.append(p)
        else:
            notes.append(f"dropped unknown project id '{p}'")
    out["projects"] = list(dict.fromkeys(keep))
    known = set().union(*own.values(), *projects.values())
    for bid in list(out["rephrase"]):
        if bid not in known:
            notes.append(f"dropped rephrase for unknown bullet id '{bid}'")
            del out["rephrase"][bid]


def proposal_usable(prop: dict) -> bool:
    """A cleaned proposal is usable if it selects at least one valid bullet or project."""
    return any(prop["experience"].values()) or bool(prop["projects"])


def normalise_keywords(out) -> tuple[list[str], list[str], list[str]]:
    """(required, preferred, notes) from an extract_keywords answer. Keywords may be strings or objects
    {"term", "weight", "required"}; an object's `required` flag decides its list, its weight is ignored (weights come
    from settings). A flat "keywords" list is accepted too."""
    notes: list[str] = []
    if not isinstance(out, dict):
        return [], [], ["answer was not an object"]
    req: list[str] = []
    pref: list[str] = []
    for key, default in (("required", True), ("preferred", False), ("keywords", None)):
        items = out.get(key)
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            if items:
                notes.append(f"{key}: expected a list, got {type(items).__name__}; ignored")
            continue
        for it in items:
            term, flag = (it.strip(), default) if isinstance(it, str) else (None, default)
            if isinstance(it, dict):
                term = _first_text(it, _TERM_KEYS)
                if isinstance(it.get("required"), bool):
                    flag = it["required"]
            if not term:
                notes.append(f"{key}: dropped {_short(it)} (no term)")
                continue
            (req if flag in (True, None) else pref).append(term)
    return req, pref, notes


def normalise_letter(out) -> tuple[list[str], list[str]]:
    """(paragraphs, notes) from a cover_letter answer. Paragraphs may be strings or {"text"} objects, or the whole
    letter one string (split on blank lines). Anything else is dropped, with a note."""
    notes: list[str] = []
    if not isinstance(out, dict):
        return [], ["answer was not an object"]
    items = out.get("paragraphs")
    if items is None:
        items = out.get("letter") or out.get("body")
    if isinstance(items, str):
        items = re.split(r"\n\s*\n", items)
    if not isinstance(items, list):
        return [], [f"paragraphs: expected a list, got {type(items).__name__}"] if items else []
    paras = []
    for it in items:
        text = it if isinstance(it, str) else _first_text(it, _PARA_KEYS) if isinstance(it, dict) else None
        if text and text.strip():
            paras.append(text.strip())
        else:
            notes.append(f"paragraphs: dropped {_short(it)} (no text)")
    return paras, notes


def normalise_rephrase(out, allowed: set[str]) -> tuple[dict[str, str], list[str]]:
    """({bullet id: reworded text}, notes) from a rephrase_bullets answer. The asked-for shape is the flat object
    {"az1": "Built ..."}; also read: the same inside a wrapper key ("rephrase", "rephrased", "bullets"), a list of
    {"id", "text"} objects, and an id mapped to an object carrying the text. Ids outside `allowed` (the ids that were
    sent) and anything that is not text are dropped, with a note."""
    notes: list[str] = []
    if not isinstance(out, dict) and not isinstance(out, list):
        return {}, ["answer was not an object"]
    if isinstance(out, dict) and len(out) <= 2:
        for key in ("rephrase", "rephrased", "bullets", "results", "rewrites"):
            if isinstance(out.get(key), (dict, list)):
                out = out[key]
                notes.append(f"answer was wrapped in '{key}'; read it")
                break
    if isinstance(out, list):
        out = {as_id(r): _first_text(r, _REPHRASE_KEYS) for r in out if isinstance(r, dict) and as_id(r)}
    res: dict[str, str] = {}
    for bid, text in out.items():
        if isinstance(text, dict):
            text = _first_text(text, _REPHRASE_KEYS)
        if not isinstance(bid, str) or bid not in allowed:
            notes.append(f"dropped rewrite for '{str(bid)[:30]}' (not a bullet that was sent)")
        elif not isinstance(text, str) or not text.strip():
            notes.append(f"{bid}: not text; dropped")
        else:
            res[bid] = text.strip()
    return res, notes


_FACT_REF = re.compile(r"^\s*(?:f|fact)?\s*#?(\d+)\s*[.:)]?\s*$", re.I)


def normalise_profile(out, facts: list[str]) -> tuple[list[int], str, list[str]]:
    """(picked fact indexes (0-based, at most 2), profile text, notes) from a tailor_profile answer. Facts may be named
    by id ("F2", "2", 2) or by their text; anything else is dropped, with a note. Fewer than two valid picks is the
    caller's to fill."""
    notes: list[str] = []
    if not isinstance(out, dict):
        return [], "", ["answer was not an object"]
    picks = out.get("facts")
    if picks is None:
        picks = out.get("picked") or out.get("profile_facts")
    if isinstance(picks, (str, int)):
        picks = re.split(r"\s*[,;]\s*", picks) if isinstance(picks, str) else [picks]
    idx: list[int] = []
    for p in picks if isinstance(picks, list) else []:
        i = None
        if isinstance(p, dict):
            p = p.get("id") or p.get("fact") or p.get("text")
        if isinstance(p, int) and not isinstance(p, bool):
            i = p - 1
        elif isinstance(p, str):
            m = _FACT_REF.match(p)
            i = int(m.group(1)) - 1 if m else next((n for n, f in enumerate(facts) if f.strip() == p.strip()), None)
        if i is None or not 0 <= i < len(facts):
            notes.append(f"facts: dropped {_short(p)} (not one of the facts)")
        elif i not in idx:
            idx.append(i)
    if len(idx) > 2:
        notes.append("facts: more than two picked; kept the first two")
    profile = out.get("profile")
    if profile is not None and not isinstance(profile, str):
        notes.append("profile: not text; dropped")
        profile = ""
    return idx[:2], (profile or "").strip(), notes
