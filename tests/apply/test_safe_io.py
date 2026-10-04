"""Rule 0: the guarded writer, and a scan proving nothing destructive can reach the archive root."""
import ast
import hashlib
from pathlib import Path

import pytest

from careerops.apply import safe_io
from careerops.apply.safe_io import safe_copy, safe_mkdir

PKG = Path(__file__).resolve().parents[2] / "careerops"


def test_safe_copy_refuses_existing_destination_and_leaves_it_alone(tmp_path):
    src = tmp_path / "src.pdf"; src.write_bytes(b"new bytes")
    dst = tmp_path / "dst.pdf"; dst.write_bytes(b"precious")
    mtime = dst.stat().st_mtime_ns
    with pytest.raises(FileExistsError):
        safe_copy(src, dst)
    assert dst.read_bytes() == b"precious" and dst.stat().st_mtime_ns == mtime


def test_safe_copy_returns_sha256_and_writes_exact_bytes(tmp_path):
    data = bytes(range(256)) * 1000
    src = tmp_path / "a.bin"; src.write_bytes(data)
    assert safe_copy(src, tmp_path / "b.bin") == hashlib.sha256(data).hexdigest()
    assert (tmp_path / "b.bin").read_bytes() == data


def test_safe_copy_refuses_a_dangling_symlink_destination(tmp_path):
    src = tmp_path / "a"; src.write_bytes(b"x")
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "nowhere")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(FileExistsError):
        safe_copy(src, link)


def test_safe_mkdir_refuses_existing_folder(tmp_path):
    (tmp_path / "Lendable").mkdir()
    (tmp_path / "Lendable" / "hand-made.pdf").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        safe_mkdir(tmp_path / "Lendable")
    assert (tmp_path / "Lendable" / "hand-made.pdf").read_bytes() == b"x"


def test_safe_mkdir_never_creates_missing_parents(tmp_path):
    with pytest.raises(FileNotFoundError):
        safe_mkdir(tmp_path / "missing" / "child")
    assert not (tmp_path / "missing").exists()


def test_safe_io_exposes_no_delete_rename_or_replace():
    public = {n for n in dir(safe_io) if not n.startswith("_") and callable(getattr(safe_io, n))}
    assert {"safe_mkdir", "safe_copy"} <= public
    assert not [n for n in public if any(w in n for w in ("remove", "delete", "unlink", "rename", "replace", "move", "rmtree"))]


DESTRUCTIVE_ATTRS = {"unlink", "rmdir", "rmtree", "remove", "removedirs", "rename", "renames", "move", "truncate"}
# (file, function) pairs that delete or rename — each one works on repo-internal paths, never the archive root.
ALLOWED = {
    ("tailoring/render.py", "compile_tex"): "xelatex aux/log files in data/outputs/<job>",
    ("tailoring/render.py", "_finalise"): "rename cv.pdf -> '<name> CV.pdf' inside data/outputs/<job>",
    ("tailoring/tailor.py", "tailor_job"): "builds data/outputs/<job>.partial, swaps it in on success, removes "
                                           ".partial/.old; out_root is paths.outputs, never paths.applications",
    ("tailoring/tailor.py", "_discard"): "the only rmtree in tailoring: <job>.partial/.old/.failed directly inside the "
                                         "outputs folder, anything else raises",
    ("apply/archive.py", "_remove_staging"): "deletes data/tmp/archive-*; refuses anything outside paths.tmp",
}


def _destructive_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    stack: list[str] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name); self.generic_visit(node); stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            os_replace = name == "replace" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in ("os", "shutil")
            if name in DESTRUCTIVE_ATTRS or os_replace:
                out.append((stack[-1] if stack else "<module>", name, node.lineno))
            self.generic_visit(node)

    V().visit(tree)
    return out


def test_no_destructive_call_in_package_outside_the_allowlist():
    """The spec's grep test (unlink, rmtree, os.remove, os.rename, os.replace, shutil.move ...), done on the AST."""
    offenders = []
    for py in PKG.rglob("*.py"):
        rel = py.relative_to(PKG).as_posix()
        for func, name, line in _destructive_calls(py):
            if (rel, func) not in ALLOWED:
                offenders.append(f"{rel}:{line} {func}() calls {name}")
    assert offenders == []


def test_safe_io_has_no_destructive_call_and_only_exclusive_writes():
    src = (PKG / "apply" / "safe_io.py").read_text(encoding="utf-8")
    assert _destructive_calls(PKG / "apply" / "safe_io.py") == []
    assert '"xb"' in src and '"wb"' not in src and '"ab"' not in src and "'wb'" not in src
