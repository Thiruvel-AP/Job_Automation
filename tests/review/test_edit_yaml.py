"""F5 (v2.4.1): after the editor closes on edit.yaml the file is parsed; a broken file (a stray `x` line) is never passed on: the line and column are printed
and the person chooses [r]eopen or [d]iscard."""
from careerops.review import workflow

GOOD = "profile: A profile.\nbullets:\n  az1: A bullet.\n"
BROKEN = "profile: A profile.\nbullets:\n  az1: A bullet.\nx\n"


class Script:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def test_a_good_file_is_returned_without_a_question(tmp_path, capsys):
    p = tmp_path / "edit.yaml"
    p.write_text(GOOD, encoding="utf-8")
    ask = Script()
    assert workflow.read_edit_file(p, ask, lambda path: None)["bullets"] == {"az1": "A bullet."} and ask.prompts == []


def test_a_stray_x_line_prints_the_line_and_column_and_offers_reopen(tmp_path, capsys):
    p = tmp_path / "edit.yaml"
    p.write_text(BROKEN, encoding="utf-8")
    ask, opened = Script("r"), []

    def fix(path):
        opened.append(path)
        path.write_text(GOOD, encoding="utf-8")
    got = workflow.read_edit_file(p, ask, fix)
    out = capsys.readouterr().out
    assert got["profile"] == "A profile." and opened == [p]
    assert "line " in out and "column " in out and "[r]eopen" in ask.prompts[0] and "[d]iscard" in ask.prompts[0]


def test_discard_returns_none_and_the_broken_file_is_never_passed_on(tmp_path, capsys):
    p = tmp_path / "edit.yaml"
    p.write_text(BROKEN, encoding="utf-8")
    assert workflow.read_edit_file(p, Script("d"), lambda path: None) is None
    assert "discarded" in capsys.readouterr().out


def test_anything_else_asks_again_and_a_still_broken_file_after_reopen_asks_again(tmp_path, capsys):
    p = tmp_path / "edit.yaml"
    p.write_text(BROKEN, encoding="utf-8")
    ask = Script("what", "r", "d")
    assert workflow.read_edit_file(p, ask, lambda path: None) is None          # reopened, still broken, then discarded
    assert len(ask.prompts) == 3


def test_a_file_that_is_not_a_mapping_is_refused_like_a_broken_one(tmp_path, capsys):
    p = tmp_path / "edit.yaml"
    p.write_text("- just\n- a list\n", encoding="utf-8")
    assert workflow.read_edit_file(p, Script("d"), lambda path: None) is None
    assert "must be a mapping" in capsys.readouterr().out
