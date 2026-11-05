"""Every place the package can delete or move a file is listed here, with why it is safe. A new one fails this test until
someone reads it and adds it, so a delete-by-glob over a shared folder (the way curated inputs get lost) cannot slip in.

The rule: a delete's target is a path the same code created in this run under a staging root, never a folder of the user's
own files (the archive, config/, data/calibration, the frozen keywords)."""
import ast
from pathlib import Path

import pytest

from careerops.tailoring import tailor

PKG = Path(__file__).resolve().parents[1] / "careerops"
DESTRUCTIVE = {"rmtree", "unlink", "remove", "rmdir", "removedirs", "move", "rename", "renames", "replace", "truncate",
               "copyfile", "copy", "copy2", "copytree"}
# (module, call) -> (how many, why it is safe). `replace` only counts as os.replace; str.replace is not a file operation.
ALLOWED = {
    ("apply/archive.py", "shutil.rmtree"): (1, "_remove_staging: a folder under paths.tmp that this run staged; refuses "
                                               "anything not strictly inside it and anything in or under the archive"),
    ("apply/live.py", "shutil.move"): (1, "_move: a JD .txt between the folders under paths.jds (inbox -> done / rejected) to a "
                                          "name that does not exist yet; refuses any path in or under the archive root"),
    ("apply/decided.py", "shutil.move"): (1, "move_file: a JD .txt between inbox/ and paths.jds_done/skipped|applied (a skip or an application "
                                               "covers every copy of the job; `unskip` moves it back) to a name that does not exist yet; "
                                               "refuses any path in or under the archive roots"),
    ("apply/decided.py", "os.replace"): (1, "_reopen: swaps in paths.live/state.json.tmp, a file this call just wrote"),
    ("factbank_append.py", "os.replace"): (1, "_write_atomic: swaps in fact_bank.yaml.tmp, a file this call just wrote, over the fact bank file (fact_bank.yaml): "
                                              "the one writer of the fact bank, after the user's y, a dated backup and a validation (a failed one writes the backup back)"),
    ("apply/handoff.py", "os.replace"): (1, "Handoff.record: swaps in paths.live/handoff_hosts.json.tmp, a file this call just wrote"),
    ("apply/live.py", "os.replace"): (1, "State.save: swaps in paths.live/state.json.tmp, a file this run just wrote"),
    ("tailoring/render.py", ".unlink"): (1, "xelatex's own .aux/.log/.out next to the PDF it just built in this run's work folder"),
    ("tailoring/render.py", "os.replace"): (1, "renames this run's cv.pdf/cv.tex to their final names inside the same work folder"),
    ("tailoring/tailor.py", "shutil.rmtree"): (1, "_discard: <job>.partial/.old/.failed directly inside the outputs folder, "
                                                    "refuses any other path"),
    ("tailoring/tailor.py", "os.replace"): (1, "_rename: swaps this run's finished <job>.partial folder in (the previous output goes "
                                                "to <job>.old first, then _discard), retried when Windows holds a file"),
    ("tailoring/tailor.py", "shutil.copy2"): (1, "_copy_over: when Windows will not rename the folder, this run's finished files are copied into "
                                                 "<job>/ (new, or the previous output being replaced); the .partial is then _discard-ed"),
    ("apply/archive.py", "shutil.copyfile"): (1, "copies the files to archive into a fresh tempfile.mkdtemp folder under paths.tmp: "
                                                  "the destination is new, nothing is overwritten"),
    ("setup.py", "shutil.copy"): (1, "`careerops init` copies .env.example to .env only when .env does not exist"),
    ("tailoring/tailor.py", "shutil.copytree"): (1, "copies this run's llm_raw replies into <job>.failed (creates, never overwrites)"),
}


def _calls() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr, owner = node.func.attr, node.func.value
            if attr not in DESTRUCTIVE:
                continue
            if isinstance(owner, ast.Name) and owner.id in ("os", "shutil"):
                name = f"{owner.id}.{attr}"
            elif attr in ("unlink", "rmdir", "rename", "truncate") and not (isinstance(owner, ast.Name) and owner.id == "str"):
                name = f".{attr}"
            else:
                continue                                                  # str.replace, dict.copy, list.remove ...
            found[(rel, name)] = found.get((rel, name), 0) + 1
    return found


def test_every_delete_or_move_in_the_package_is_on_the_reviewed_list():
    found = _calls()
    unreviewed = {k: n for k, n in found.items() if k not in ALLOWED}
    assert not unreviewed, f"new delete/move calls, add them to ALLOWED with a reason if they are safe: {unreviewed}"
    assert {k: n for k, (n, _) in ALLOWED.items()} == found, "ALLOWED is out of date (a listed call is gone or repeated)"


def test_nothing_in_the_calibration_or_review_or_discovery_code_deletes_anything():
    touched = {m for m, _ in _calls() if m.startswith(("reporting/", "review/", "discovery/", "llm/", "training/"))}
    assert touched == set()


# ---------------------------------------------------------------- the one folder deletion in tailoring
def test_discard_removes_only_a_staging_folder_directly_inside_the_outputs_folder(tmp_path):
    out = tmp_path / "outputs"
    (out / "Acme_DS_1.partial").mkdir(parents=True)
    (out / "Acme_DS_1.partial" / "cv.tex").write_text("x")
    tailor._discard(out / "Acme_DS_1.partial", out)
    assert not (out / "Acme_DS_1.partial").exists()
    tailor._discard(out / "Acme_DS_1.old", out)                             # absent: nothing to do, no error


@pytest.mark.parametrize("rel", ["Acme_DS_1", "keywords", "x.yaml", "sub/Acme.partial", "../elsewhere.partial", "."])
def test_discard_refuses_everything_else(tmp_path, rel):
    out = tmp_path / "outputs"
    (out / "Acme_DS_1").mkdir(parents=True)
    (out / "keywords").mkdir()
    (tmp_path / "elsewhere.partial").mkdir()
    (out / "sub").mkdir()
    (out / "sub" / "Acme.partial").mkdir()
    with pytest.raises(RuntimeError, match="refusing to delete"):
        tailor._discard(out / rel, out)
    assert (out / "Acme_DS_1").is_dir() and (out / "keywords").is_dir() and (tmp_path / "elsewhere.partial").is_dir()
    assert (out / "sub" / "Acme.partial").is_dir()


@pytest.mark.slow
def test_a_tailoring_run_leaves_every_other_folder_in_the_outputs_alone_and_replaces_only_its_own(settings, fb_fixture,
                                                                                                   job, spy_llm):
    out = settings.path("outputs")
    out.mkdir(parents=True, exist_ok=True)
    (out / "keywords").mkdir()
    (out / "keywords" / "cal-x.yaml").write_text("curated: true\n")
    (out / "Other_Job_1").mkdir()
    (out / "Other_Job_1" / "CV.pdf").write_bytes(b"another job")
    first = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, out)
    second = tailor.tailor_job(job, fb_fixture, spy_llm(), settings, out)           # re-tailoring the same job replaces it
    assert first["out_dir"] == second["out_dir"] and Path(second["cv_pdf"]).is_file()
    assert (out / "keywords" / "cal-x.yaml").read_text() == "curated: true\n"
    assert (out / "Other_Job_1" / "CV.pdf").read_bytes() == b"another job"
    assert not [p for p in out.iterdir() if p.suffix in tailor.STAGING_SUFFIXES]    # and no staging folder is left
