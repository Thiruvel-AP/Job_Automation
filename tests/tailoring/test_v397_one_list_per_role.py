r"""v3.9.7: every bullet of a role is one \item inside one \begin{itemize} ... \end{itemize} (the LSEG CV printed dc7 outside it)."""
import re

from careerops.tailoring import layout, typography
from careerops.tailoring.render import env


def _tex(ctx):
    ctx = layout.with_openers(layout.with_splits(ctx))
    ctx = typography.typeset(ctx, keep=())
    return env().get_template("cv2.tex.j2").render(**ctx).split(r"\begin{document}")[1]


def test_a_role_with_four_bullets_the_fourth_a_plain_line_is_one_list():
    fourth = "Designed how a user's question reaches the AI server; checked it with 12 tests.\n\nSecond paragraph."
    ctx = dict(id={"name": "A B", "location": "X", "email": "a@b.c"}, profile="p", skills={"Core": ["Python"]},
               experience=[{"org": "180 Degrees Consulting", "dates": "Jan 2025 - Feb 2025", "title": "Consultant", "situation": "Charity needed help",
                            "bullets": ["One.", "Two.", "Three.", fourth], "bullet_ids": ["dc1", "dc2", "dc3", "dc7"]}],
               projects=[], education=[], certifications=[], plain=["dc7"], splits={})
    body = _tex(ctx)
    role = body.split("Work Experience")[1].split(r"\section")[0]
    assert role.count(r"\begin{itemize}") == 1 and role.count(r"\end{itemize}") == 1
    inside = role.split(r"\begin{itemize}")[1].split(r"\end{itemize}")[0]
    assert len(re.findall(r"\\item\b", inside)) == 4
    assert "\n\n" not in inside, "a blank line inside a bullet ends the item's paragraph"
