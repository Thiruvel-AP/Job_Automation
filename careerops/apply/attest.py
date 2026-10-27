"""Attestations: the check boxes and radio buttons that make a statement in the applicant's name ("I acknowledge the privacy notice", "I confirm all
answers are true", "I agree to the terms"). Code never answers one, whatever answers.yaml says: the filler, the assist, the auto-apply and the form
agent leave it untouched, outline it magenta and list it for the human, and the auto-apply does not submit until it is set (the required box is still
empty, so `autoapply.still_empty` stops the run).
"""
from __future__ import annotations

import re

MAGENTA, RESET = "\033[35m", "\033[0m"
OUTLINE = "3px solid magenta"

ATTEST = re.compile(
    r"acknowledg|privacy\s+(?:notice|policy|statement)|data\s+protection|terms\s+(?:and|&)\s+conditions|terms\s+of\s+(?:use|service)|"
    r"\bi\s+(?:hereby\s+)?(?:confirm|certify|declare|agree|consent|accept|understand|have\s+read|authori[sz]e)\b|"
    r"\b(?:answers?|information|statements?|details)\b.{0,50}\b(?:true|accurate|correct|complete|truthful)\b|\bconsent\s+to\b", re.I)


def is_attestation(text: str) -> bool:
    return bool(ATTEST.search(text or ""))


def mark(group: list, question: str, found: list[str]) -> None:
    """Outline the elements of an attestation magenta and note its question once in `found`. Nothing is ticked."""
    for el in group:
        el.evaluate(f"e => e.style.outline = '{OUTLINE}'")
    if question not in found:
        found.append(question)


def lines(attestations: list[str]) -> list[str]:
    """The block the assist prints under the filled / YOU ANSWER lists; nothing when there are none."""
    if not attestations:
        return []
    return ["", f"{MAGENTA}ATTESTATIONS (magenta){RESET}: never answered by this program; you set them yourself, and auto-submit waits until they are set:"] + \
        [f"  {MAGENTA}-{RESET} {a}" for a in attestations]
