"""A fake `claude` binary for the v3.5 judge tests: a Python script named `claude`, put first on PATH. It never reaches a model. Its behaviour
comes from environment variables, and every call is logged (argv, cwd, the length of stdin) to FAKE_CLAUDE_LOG.

    FAKE_CLAUDE_MODE    ok | logged_out | timeout | exit1 | no_structured | bad_enum | limit | is_error
    FAKE_CLAUDE_ANSWER  the structured_output JSON for mode ok
    FAKE_CLAUDE_AUTH    the authMethod `auth status` reports (default claude.ai)

`review_job_tailored` builds a needs_review job with a report.json but no PDFs, so the queue tests need no xelatex."""
import json
import os
import stat
import sys
from pathlib import Path

SCRIPT = r'''#!PYTHON
import json, os, sys, time
args = sys.argv[1:]
stdin = "" if args[:1] == ["--version"] or args[:2] == ["auth", "status"] else sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "stdin_chars": len(stdin)}) + "\n")
if os.environ.get("FAKE_CLAUDE_STDIN"):
    with open(os.environ["FAKE_CLAUDE_STDIN"], "a", encoding="utf-8") as f:
        f.write(stdin)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if args[:1] == ["--version"]:
    print("2.1.322 (Claude Code)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    if mode == "logged_out":
        print(json.dumps({"loggedIn": False}))
        sys.exit(1)
    print(json.dumps({"loggedIn": True, "authMethod": os.environ.get("FAKE_CLAUDE_AUTH", "claude.ai")}))
    sys.exit(0)
if mode == "timeout":
    time.sleep(60)
if mode == "exit1":
    print("something broke", file=sys.stderr)
    sys.exit(1)
if mode == "limit":
    print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1700000000"}))
    sys.exit(1)
if mode == "is_error":
    print(json.dumps({"type": "result", "is_error": True, "result": "error_during_execution"}))
    sys.exit(0)
if mode == "no_structured":
    print(json.dumps({"type": "result", "is_error": False, "result": "send it", "total_cost_usd": 0.01}))
    sys.exit(0)
answer = json.loads(os.environ.get("FAKE_CLAUDE_ANSWER") or "{}")
if mode == "bad_enum":
    answer = dict(answer, decision="approve_now")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": answer,
                  "total_cost_usd": 0.0123, "duration_ms": 1500}))
'''

SEND = {"decision": "send", "confidence": "high", "reasons": ["claims match the bank"], "unsupported_claims": [], "retailor_hint": ""}


class FakeClaude:
    def __init__(self, folder: Path, monkeypatch):
        self.dir = folder / "fakebin"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bin = self.dir / "claude"
        self.bin.write_text(SCRIPT.replace("PYTHON", sys.executable), encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.log = folder / "fake_claude.jsonl"
        self.stdin = folder / "fake_claude_stdin.txt"
        self.mp = monkeypatch
        monkeypatch.setenv("PATH", str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(self.stdin))
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        self.set(mode="ok", answer=SEND)

    def set(self, mode: str = "ok", answer: dict | None = None, auth: str = "claude.ai") -> "FakeClaude":
        self.mp.setenv("FAKE_CLAUDE_MODE", mode)
        if answer is not None:
            self.mp.setenv("FAKE_CLAUDE_ANSWER", json.dumps(answer))
        self.mp.setenv("FAKE_CLAUDE_AUTH", auth)
        return self

    def calls(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def judge_calls(self) -> list[list[str]]:
        """argv of every `claude -p` call (not --version / auth status)."""
        return [c["argv"] for c in self.calls() if "-p" in c["argv"]]


def use_claude(settings, mode: str = "shadow") -> None:
    """The test settings with the Claude backend on (the shipped test copy says backend: human)."""
    settings.raw["judge"] = {"backend": "claude", "mode": mode, "model": "sonnet"}


def report_job(db, settings, job: dict, *, strict: float = 72.0, ceiling: float = 80.0, extra: dict | None = None) -> dict:
    """A needs_review job with an output folder holding report.json only (no PDFs): enough for the queue, the guards and `show`."""
    out = Path(settings.path("outputs")) / job["id"].replace(":", "_")
    out.mkdir(parents=True, exist_ok=True)
    rep = {"out_dir": str(out), "ats": {"strict": strict, "ceiling": ceiling}, "context": {"experience": [], "projects": []},
           "letter": {"status": "off"}, "attempts": [{"attempt": 1, "violations": [], "warnings": []}], "final": {"chosen_attempt": 1},
           "keywords": [], **(extra or {})}
    (out / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    db.upsert_job(job)
    db.update_job(job["id"], status="needs_review", out_dir=str(out))
    return rep
