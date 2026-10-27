"""The one question "could this control submit the application?", asked by the form agent's snapshot (a submit control gets no ref) and again by its
executor just before it acts (apply/formagent.py). It reads the facts of an element, never a browser:

  tag, type (the AUTHORED type attribute, '' when there is none), role, in_form (inside a <form>), and every name source on its own:
  aria_label, labelledby (the text of the elements aria-labelledby points to), placeholder, text, value, name, title, alt, id, data
  (data-testid / data-automation-id / data-qa).

A control is a submit when ANY of these holds:
  * its shape submits: `<input type=submit>`, `<input type=image>`, `<button type=submit>`, or a `<button>` with no type inside a `<form>`;
  * any name source says submit / send / finish / confirm ... in any of the languages in SUBMIT_WORDS (the display label is not the only source: an
    icon-only `<button aria-label="Submit application">` or `<input type=button value="Absenden">` is caught);
  * it is an "Apply" control (APPLY_WORDS) inside a <form>: the final "Apply" of an application. Outside a form the same word is the link that
    opens the application, the one click `reach` needs.
A field (text box, select, check box, file) is never a control, so its name does not matter. Nothing here clicks.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping

BUTTON_INPUTS = ("submit", "button", "image", "reset")
CONTROL_ROLES = ("button", "link", "menuitem")
SOURCES = ("aria_label", "labelledby", "title", "alt", "text", "value", "placeholder", "name", "id", "data")     # the order a human reads them in
HIDDEN_SOURCES = ("name", "id", "data")                    # attributes a person does not see: a word there is a submit hint, never an "Apply" way in

# submit / send / finish / confirm in the languages job sites come in (compared on text folded to lower case, camelCase and punctuation split into words)
SUBMIT_WORDS = re.compile(
    r"submit|\b(?:re)?send\w*|\bfinish\w*|\bfinali[sz]\w*|\bcomplete (?:your |my |the )?application|\bconfirm\w*|"
    r"\babsenden|\babschicken|\beinreichen|\bsenden|\bversenden|\b[üu]bermitteln|"                                              # German
    r"\benvoyer|\bsoumettre|\btransmettre|\bvalider|"                                                                          # French
    r"\benviar|\binviare|\binvia\w*|\binoltra\w*|\bverzenden|\bversturen|\bindienen|\bskicka|\bsende\b|\bl[äa]het[äa]|"        # Spanish, Italian, Dutch, Nordic
    r"\bwy[śs]lij|\bodeslat|\bg[öo]nder|\btrimite|\bотправ\w*|"                                                              # Polish, Czech, Turkish, Romanian, Russian
    r"提交|送信|送出|发送|發送|送付|제출|전송|보내기", re.I)
APPLY_WORDS = re.compile(
    r"\bapply\w*|\bbewerben|\bbewerbung|\bpostul\w*|\bcandidature|\bsolicitar|\bsolicitud|\bcandidar\w*|\bcandidatura|\bsolliciteren|"
    r"\bans[öo]k\w*|\bansøg\w*|\bhakemus|\baplikuj|\baplicar|\bba[sş]vur\w*|\bim interested|\bi am interested|\bподать|应募|應募|応募|申请|申請|지원", re.I)


def _fold(text: Any) -> str:
    """Lower-case words: accents kept, camelCase split ("btnSubmitApplication" -> "btn submit application"), punctuation and underscores to spaces."""
    t = unicodedata.normalize("NFKC", str(text or "")).replace("'", "").replace("’", "")
    t = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", t)
    return " ".join(re.sub(r"[\W_]+", " ", t.lower()).split())


def is_control(facts: Mapping[str, Any]) -> bool:
    """Is it something one clicks (a button, a button-like input, a link, a role=button element) and not a field?"""
    tag, typ, role = str(facts.get("tag") or "").lower(), str(facts.get("type") or "").lower(), str(facts.get("role") or "").lower()
    if tag == "input":
        return typ in BUTTON_INPUTS
    return tag in ("button", "a", "summary") or role in CONTROL_ROLES


def submit_reason(facts: Mapping[str, Any]) -> str | None:
    """Why the element could submit the application, or None when it cannot: the shape, else the first name source with a submit word, else an
    Apply word inside a form."""
    if not is_control(facts):
        return None
    tag, typ = str(facts.get("tag") or "").lower(), str(facts.get("type") or "").lower()
    if typ == "submit":
        return f"type=submit ({tag})"
    if tag == "input" and typ == "image":
        return "input type=image"
    if tag == "button" and not typ and facts.get("in_form"):
        return "a <button> with no type inside a <form> submits it"
    for source in SOURCES:
        text = _fold(facts.get(source))
        m = SUBMIT_WORDS.search(text) if text else None
        if m:
            return f"{source} {str(facts.get(source))[:60]!r} says {m.group(0)!r}"
    if facts.get("in_form"):
        for source in SOURCES:
            m = APPLY_WORDS.search(_fold(facts.get(source))) if source not in HIDDEN_SOURCES else None
            if m:
                return f"{source} {str(facts.get(source))[:60]!r} says {m.group(0)!r} inside a <form>"
    return None


def is_submit(facts: Mapping[str, Any]) -> bool:
    return submit_reason(facts) is not None


def label_of(facts: Mapping[str, Any]) -> str:
    """The name a person sees for the element: the first name source in reading order, else its authored type ('submit' for an unnamed submit)."""
    for source in SOURCES[:7]:
        text = " ".join(str(facts.get(source) or "").split())
        if text:
            return text
    return str(facts.get("type") or "") if str(facts.get("type") or "").lower() in ("submit", "image") else ""
