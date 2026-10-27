"""CV layout rules of the Document rules (A4 to A6): at most three projects, each its own sub-heading with a URL only when the bank says it is
public; work experience fills first (each role to its maximum, then projects, then linespread) and a short page 2 says the bank has no more
experience bullets; certifications are a bulleted list, one item each. No network, no model; the template is rendered to a string."""
from pathlib import Path

import pytest

from careerops.tailoring import render, tailor
from careerops.tailoring.scorer import Keyword
from careerops.tailoring.selection import MAX_PROJECTS, Layout, fill_order, layout_from, project_cap, select_cv

LAYOUT = Layout((4, 6), (3, 5), 3, (2, 3), 2, 2)


def _b(bid, *words):
    return {"id": bid, "text": " ".join(words) + " work", "terms": []}


def bank(first=7, other=6, projects=5):
    def entry(eid, n):
        return {"id": eid, "bullets": [_b(f"{eid}b{i}", f"k{eid}{i}", f"w{eid}{i}") for i in range(n)]}

    return {"skills": {}, "experience": [entry("e1", first), entry("e2", other), entry("e3", other)],
            "projects": [{"id": f"p{j}", "bullets": [_b(f"p{j}b{i}", f"kp{j}{i}", f"wp{j}{i}") for i in range(4)]} for j in range(projects)]}


NOTHING = [Keyword("zzz-nothing", 1.0, True)]


# ------------------------------------------------------------------ settings
def test_settings_ask_for_three_projects_and_the_agreed_bullet_ranges(settings):
    sc = settings["scoring"]
    assert (sc["cv_projects"], sc["cv_projects_max"]) == (3, 3)
    assert sc["cv_bullets"] == {"most_recent": [4, 6], "other": [3, 5]}
    lay = layout_from(settings)
    assert (lay.first, lay.other, lay.projects) == ((4, 6), (3, 5), 3)


def test_the_code_never_shows_more_than_three_projects_whatever_the_settings_say(settings):
    settings.raw["scoring"]["cv_projects"] = 5
    settings.raw["scoring"]["cv_projects_max"] = 6
    assert MAX_PROJECTS == 3 and layout_from(settings).projects == 3 and project_cap(settings) == 3
    settings.raw["scoring"]["cv_projects_max"] = 2
    assert project_cap(settings) == 2                                           # it can be lowered, never raised


# ------------------------------------------------------------------ A5: the fill order
def test_a_spare_experience_bullet_is_placed_before_any_project_bullet():
    fb = bank()
    sel = select_cv(fb, NOTHING, layout=LAYOUT)
    assert [len(sel["experience"][e]) for e in ("e1", "e2", "e3")] == [4, 3, 3] and len(sel["projects"]) == 3
    order = fill_order(fb, NOTHING, sel, None, 0.6, 3, "", layout=LAYOUT)
    kinds = ["exp" if b.startswith("e") else "proj" for b in order]
    assert kinds[:6] == ["exp"] * 6 and "exp" not in kinds[6:]                  # every spare experience bullet first, then the projects


def test_each_role_is_raised_to_its_maximum_and_no_further():
    fb = bank(first=9, other=9)
    sel = select_cv(fb, NOTHING, layout=LAYOUT)
    order = fill_order(fb, NOTHING, sel, None, 0.6, 3, "", layout=LAYOUT)
    added = {e: [b for b in order if b.startswith(e)] for e in ("e1", "e2", "e3")}
    assert [len(sel["experience"][e]) + len(added[e]) for e in ("e1", "e2", "e3")] == [6, 5, 5]


def test_a_role_with_few_bullets_just_runs_out():
    fb = bank(first=5, other=4)
    sel = select_cv(fb, NOTHING, layout=LAYOUT)
    order = fill_order(fb, NOTHING, sel, None, 0.6, 3, "", layout=LAYOUT)
    assert [len(sel["experience"][e]) + sum(b.startswith(e) for b in order) for e in ("e1", "e2", "e3")] == [5, 4, 4]


def test_the_fill_never_adds_a_fourth_project_and_no_project_goes_past_three_bullets():
    fb = bank()
    sel = select_cv(fb, NOTHING, layout=LAYOUT)
    order = fill_order(fb, NOTHING, sel, None, 0.6, 3, "", layout=LAYOUT)
    assert not [b for b in order if b.startswith(("p3", "p4"))]
    assert all(len(sel["project_bullets"][p]) + sum(b.startswith(p + "b") for b in order) <= 3 for p in sel["projects"])


def test_fill_page2_refuses_a_bullet_that_would_start_a_fourth_project(monkeypatch, tmp_path):
    fb = bank(projects=5)
    ctx = {"experience": [{**e, "bullet_ids": [], "bullets": []} for e in fb["experience"]],
           "projects": [{**p, "bullet_ids": [p["bullets"][0]["id"]], "bullets": [p["bullets"][0]["text"]]} for p in fb["projects"][:3]]}
    seen = []

    def rerender():
        seen.append(sum(len(p["bullets"]) for p in ctx["projects"]))
        return tmp_path / "cv.pdf", 2

    monkeypatch.setattr(tailor, "_used", lambda pdf, pages: 1.2)                 # page 2 stays short, so the loop keeps trying
    monkeypatch.setattr(tailor, "page2_fill", lambda pdf, pages=None: 0.2)
    stats = tailor.fill_page2(ctx, fb, ["p3b0", "p4b0", "p0b1"], {}, rerender, 0.96, max_projects=3)
    assert [p["id"] for p in ctx["projects"]] == ["p0", "p1", "p2"] and stats["added"] == ["p0b1"]


def test_a_short_page_two_says_the_bank_has_no_more_experience_bullets():
    msg = tailor.fill_shortfall({"reached": False}, 0.31, 0.96)
    assert "fact bank has no more experience bullets" in msg and "31%" in msg and "invent" in msg
    assert tailor.fill_shortfall({"reached": True}, 0.97, 0.96) == ""


# ------------------------------------------------------------------ A4: the project URL and the sub-heading
def ctx_with(projects, certifications=(), achievements=()):
    return {"id": {"name": "Min Imal", "location": "Bristol, UK", "email": "min@example.org"}, "profile": "Data scientist.",
            "skills": {"Core": ["Python"]}, "experience": [{"org": "Acme", "dates": "2025", "title": "Analyst", "bullets": ["Built a report."]}],
            "projects": projects, "education": [{"degree": "MSc", "org": "Uni", "dates": "2026"}], "certifications": list(certifications),
            "achievements": list(achievements)}


OPEN = {"id": "o", "name": "Open Thing", "stack": "Python, SQL", "github": "https://github.com/u/open", "public": True, "bullets": ["Built it."]}
SHUT = {"id": "s", "name": "Private Thing", "stack": "Rust", "github": "https://github.com/u/private", "public": False, "bullets": ["Built that."]}
UNSET = {"id": "n", "name": "Unset Thing", "stack": "Go", "github": "https://github.com/u/unset", "bullets": ["Built this."]}


def test_a_project_with_public_false_prints_no_url_and_the_others_do():
    ctx = ctx_with([OPEN, SHUT, UNSET])
    tex = render.env().get_template("cv2.tex.j2").render(**render.with_public_links(ctx))
    assert "github.com/u/open" in tex and "github.com/u/unset" in tex           # a missing `public:` is treated as public
    assert "github.com/u/private" not in tex and "Private Thing" in tex and "Rust" in tex
    assert [render.url_shown(p) for p in (OPEN, SHUT, UNSET)] == ["https://github.com/u/open", "", "https://github.com/u/unset"]
    assert ctx["projects"][1]["github"] == "https://github.com/u/private"        # the caller's context is not changed


def test_each_project_has_its_own_sub_heading_with_name_stack_url_and_bullets():
    tex = render.env().get_template("cv2.tex.j2").render(**render.with_public_links(ctx_with([OPEN, UNSET])))
    block = tex.split("\\section{Projects}")[1].split("\\section{Education}")[0]
    assert block.count("\\begin{itemize}") == 2 and block.count("\\setbox0") == 2
    assert "Open Thing" in block and "Python, SQL" in block and "\\url{https://github.com/u/open}" in block


# ------------------------------------------------------------------ A6: certifications as a list
CERTS = ["Model Context Protocol: Advanced Topics — Anthropic, Jun 2026", "AWS Academy Cloud Foundations, Nov 2025", "Bristol PLUS Award, Mar 2026"]


def cert_block(tex: str) -> str:
    sec = tex.split("\\section{Certifications \\& Achievements}")[1]
    return sec.split("\\begin{itemize}")[1].split("\\end{itemize}")[0]


def test_the_tex_has_one_item_per_certification_and_no_middle_dot_joins():
    block = cert_block(render.env().get_template("cv2.tex.j2").render(**ctx_with([OPEN], CERTS)))
    assert block.count("\\item") == 3
    assert "textperiodcentered" not in block and "·" not in block
    assert "AWS Academy Cloud Foundations" in block and "Bristol PLUS Award" in block


def test_an_achievement_line_stays_beside_the_certification_items():
    ach = [{"id": "lloyds", "name": "Lloyd's Datathon", "award": "3rd place", "text": "Detected anomalies.", "result": ""}]
    block = cert_block(render.env().get_template("cv2.tex.j2").render(**ctx_with([OPEN], CERTS, ach)))
    assert block.count("\\item") == 4 and block.index("Lloyd") < block.index("AWS Academy")
