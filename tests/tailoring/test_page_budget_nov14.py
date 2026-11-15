"""14 Nov round (Junior Data Scientist, EDITED): the best bullet leads the first role, zero-score bullets go before projects, three projects
stay each with its best (result:) bullet, "page full: N ranked bullets left out", the letter's template line, and two doctor warnings."""
import yaml

from careerops import setup
from careerops.tailoring import tailor
from careerops.tailoring.selection import ONE_PAGE, Layout
from careerops.tailoring.scorer import Keyword


def entry(id_, ids, texts=None):
    return {"id": id_, "bullet_ids": list(ids), "bullets": texts or [f"text {i}" for i in ids]}


def test_the_best_bullet_leads_the_most_relevant_role():
    ctx = {"experience": [entry("az", ["az6", "az2", "az1"]), entry("av", ["av7"])], "projects": []}
    scores = {"az6": 0.0, "az2": 3.0, "az1": 5.0, "av7": 1.0}
    assert tailor.lead_best(ctx, scores) == "az1" and ctx["experience"][0]["bullet_ids"] == ["az1", "az6", "az2"]
    assert ctx["experience"][0]["bullets"][0] == "text az1" and ctx["experience"][1]["bullet_ids"] == ["av7"]


def test_the_hook_proof_bullet_stays_first_in_its_role():
    from types import SimpleNamespace
    hk = SimpleNamespace(entry_kind="experience", entry_id="az", proof_bullet_id="az6")
    ctx = {"experience": [entry("az", ["az6", "az1"])], "projects": []}
    assert tailor.lead_best(ctx, {"az6": 0.0, "az1": 5.0}, hk) == "az6" and ctx["experience"][0]["bullet_ids"] == ["az6", "az1"]


def test_zero_score_bullets_go_before_any_project():
    scores = {"az1": 4.0, "az6": 0.0, "av7": 0.0, "av1": 3.0, "p1a": 2.0, "p2a": 1.0, "p3a": 1.0, "p4a": 0.5}
    ctx = {"experience": [entry("az", ["az1", "az6"]), entry("av", ["av7", "av1"])],
           "projects": [entry(p, [p + "a"]) for p in ("p1", "p2", "p3", "p4")], "results": {}}
    layout = Layout((1, 3), (1, 3), 3, (1, 3), 0, 0)
    while len(ctx["projects"]) == 4:
        assert tailor._shrink(ctx, [Keyword("python", 2, True)], layout, None, scores)
    left = {i for e in ctx["experience"] for i in e["bullet_ids"]}
    assert "az6" not in left and "av7" not in left and left == {"az1", "av1"}      # both zero-score bullets went before the 4th project
    assert [p["id"] for p in ctx["projects"]] == ["p1", "p2", "p3"]                # the least relevant project went


def test_three_projects_are_kept_and_the_result_bullet_stays():
    scores = {"g3": 2.0, "g5": 2.0}
    ctx = {"experience": [entry("az", ["az1"])], "results": {"g5": "x"},
           "projects": [entry("pg", ["g3", "g5"]), entry("pq", ["q2"]), entry("pr", ["r1"]), entry("ps", ["s1"])]}
    scores.update(az1=5.0, q2=3.0, r1=2.0, s1=0.5)
    while tailor._shrink(ctx, [Keyword("python", 2, True)], ONE_PAGE, None, scores):
        if len(ctx["projects"]) <= 3 and all(len(p["bullets"]) == 1 for p in ctx["projects"]):
            break
    assert len(ctx["projects"]) >= 3 and [p["bullet_ids"] for p in ctx["projects"] if p["id"] == "pg"] == [["g5"]]


def test_page_full_message_lists_the_ranked_bullets_left_out():
    left = [{"id": "p_ql2", "score": 3.0}, {"id": "az4", "score": 1.5}]
    assert tailor.left_out_text(left) == "page full: 2 ranked bullets left out (p_ql2 3, az4 1.5)"
    msg = tailor.fill_shortfall({"reached": False}, 0.7, 0.96, left)
    assert "page full: 2 ranked bullets left out" in msg and "no more" not in msg
    assert tailor.left_out_text([]) == "" and "no more experience bullets" in tailor.fill_shortfall({"reached": False}, 0.7, 0.96, [])


def test_left_out_bullets_are_the_ranked_ones_not_on_the_cv():
    fb = {"experience": [{"id": "a", "bullets": [{"id": "a1", "text": "Python models", "terms": []}, {"id": "a2", "text": "React front end", "terms": []}]}],
          "projects": [{"id": "p", "bullets": [{"id": "p1", "text": "Python analysis", "terms": []}]}]}
    out = tailor.left_out_bullets(fb, [Keyword("python", 2, True)], "", {"a1"})
    assert [x["id"] for x in out] == ["p1"]            # a2 scores 0, a1 is on the CV


def test_letter_template_line_names_the_template_and_the_first_60_characters():
    letter = {"compose": "template", "template_family": "data_scientist", "paragraphs": ["x" * 50, "y" * 50]}
    line = tailor.letter_template_line(letter)
    assert line.startswith("letter template: data_scientist") and '"' + "x" * 50 + " " + "y" * 9 + '"' in line
    assert "none" in tailor.letter_template_line({"paragraphs": ["Hello"]})


def _bank(tmp_path, fb):
    p = tmp_path / "fb.yaml"
    p.write_text(yaml.safe_dump(fb), encoding="utf-8")
    return p


def test_doctor_warns_when_two_families_share_a_letter_template(tmp_path):
    same = "Hi {company} team,\n\nP1 text.\n\nRegards,\n\nName"
    fb = {"families": [{"id": "ai_engineer", "letter_template": same}, {"id": "data_scientist", "letter_template": same}]}
    rows = setup.letter_template_checks(_bank(tmp_path, fb))
    assert rows[0][1] is False and "data_scientist" in rows[0][0] and "ai_engineer" in rows[0][0]
    fb["families"][1]["letter_template"] = same + " different"
    assert setup.letter_template_checks(_bank(tmp_path, fb))[0][1] is True


def test_doctor_warns_when_profile_opening_and_body_repeat_four_words(tmp_path):
    fb = {"families": [{"id": "ds", "profile_opening": "Builds models end to end in Python.", "profile_body": "Also builds models end to end with SQL."},
                       {"id": "ai", "profile_opening": "Takes agents to deployment.", "profile_body": "Led a team of five."}]}
    rows = setup.profile_repeat_checks(_bank(tmp_path, fb))
    assert [ok for _, ok, _ in rows] == [False, True] and "builds models end to end" in rows[0][0]
