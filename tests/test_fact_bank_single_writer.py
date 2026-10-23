"""The only writer of the fact bank file (fact_bank.yaml) is careerops/factbank_append.py (CLAUDE.md, hard rules). A grep over the package: no other
function that names the fact bank's path (the settings key "fact_bank", or fact_bank.yaml) also writes, copies, moves or deletes a file."""
import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "careerops"
WRITER = "factbank_append.py"
NAMES_THE_BANK = re.compile(r"""["']fact_bank["']|fact_bank\.yaml['"]|\bfact_bank_path\b""")
WRITES = re.compile(r"""\.write_text\(|\.write_bytes\(|\bopen\([^)]*["'][wax]b?\+?["']|os\.(?:replace|rename|remove|unlink|truncate)\(|"""
                    r"""shutil\.(?:copy\w*|move|rmtree)\(|yaml\.(?:safe_)?dump\(|\.unlink\(|\.truncate\(|\.rename\(""")


def _functions(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, ast.get_source_segment(src, node) or ""


def test_no_other_function_that_names_the_fact_bank_also_writes_a_file():
    offenders = []
    for py in sorted(PKG.rglob("*.py")):
        if py.name == WRITER:
            continue
        for name, body in _functions(py):
            if NAMES_THE_BANK.search(body) and WRITES.search(body):
                offenders.append(f"{py.relative_to(PKG).as_posix()}::{name}")
    assert offenders == [], f"only careerops/{WRITER} may write the fact bank file (fact_bank.yaml); these name it and write: {offenders}"


def test_the_one_writer_does_write_it():
    names = {n for n, body in _functions(PKG / WRITER) if NAMES_THE_BANK.search(body) or WRITES.search(body)}
    assert "append_fact" in names or "_write_atomic" in names


def test_the_writer_is_called_only_with_a_confirmation_argument():
    """Every call of append_fact in the package passes `confirmed=...` (no positional route around the check)."""
    calls = []
    for py in sorted(PKG.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "append_fact":
                calls.append((py.relative_to(PKG).as_posix(), [k.arg for k in node.keywords]))
    assert calls and all("confirmed" in kw for _, kw in calls), calls
    assert {m for m, _ in calls} <= {"review/factsave.py"}                         # review/factsave.py is its one caller
