"""Stand-ins for everything the tracker, contacts and outreach tests would otherwise reach over the network: an in-memory Google Sheet
tab (same four methods as tracker.SheetsClient), Google's HTTP API (the `request` function GoogleAPI takes), DNS, and the Fetcher.
`make_applied` builds an applied job with a report.json and two fake PDFs, so no test here needs xelatex."""
import base64
import json
import re
from email import message_from_bytes, policy
from pathlib import Path

import dns.resolver

from careerops.db import DB


class Resp:
    """A response as GoogleAPI / Fetcher callers read it: status_code, ok, content, text, json()."""

    def __init__(self, body="", status=200, headers=None):
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        self.content = raw.encode() if isinstance(raw, str) else raw
        self.text = self.content.decode("utf-8")
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

APPLIED_AT = "2026-10-17T09:00:00+00:00"
HOOK_LINE = "At Northwind Research Lab I built a tool-calling LLM agent with a local 19,213-gene fallback, tested by ~65 tests."


# ------------------------------------------------------------------ the Sheet
def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheet:
    """One tab as a list of rows. `get_values` answers like the Sheets API (trailing empty cells and rows are left off);
    `update_cells` / `append_row` keep what they were given in `writes` / `appends` (RAW values, as sent)."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]
        self.writes: list[dict] = []
        self.appends: list[list] = []
        self.grown: list[int] = []
        self.reads: list[str] = []

    @staticmethod
    def _text(v) -> str:
        return f"{v:g}" if isinstance(v, float) else str(v)

    def _span(self, a1: str):
        a1 = a1.split("!", 1)[1]
        if m := re.fullmatch(r"(\d+):(\d+)", a1):
            return ("rows", int(m[1]) - 1, int(m[2]) - 1)
        if m := re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)", a1):
            return ("col", _col(m[1]), int(m[2]) - 1)
        if m := re.fullmatch(r"([A-Z]+)(\d+)", a1):
            return ("cell", _col(m[1]), int(m[2]) - 1)
        raise AssertionError(f"FakeSheet cannot read {a1}")

    def get_values(self, a1: str) -> list[list[str]]:
        self.reads.append(a1)
        kind, x, y = self._span(a1)
        if kind == "rows":
            out = [[self._text(c) for c in r] for r in self.rows[x:y + 1]]
        elif kind == "col":
            out = [[self._text(r[x])] if len(r) > x and self._text(r[x]) != "" else [] for r in self.rows[y:]]
        else:
            out = [[self._text(self.rows[y][x])]] if y < len(self.rows) and len(self.rows[y]) > x else []
        out = [list(r) for r in out]
        for r in out:
            while r and r[-1] == "":
                r.pop()
        while out and not out[-1]:
            out.pop()
        return out

    def update_cells(self, cells) -> None:
        if not cells:
            return
        self.writes.append(dict(cells))
        for a1, v in cells.items():
            kind, x, y = self._span(a1)
            assert kind == "cell", a1
            while len(self.rows) <= y:
                self.rows.append([])
            while len(self.rows[y]) <= x:
                self.rows[y].append("")
            self.rows[y][x] = v

    def append_row(self, tab: str, row: list) -> None:
        self.appends.append(list(row))
        self.rows.append(list(row))

    def ensure_columns(self, tab: str, count: int) -> None:
        self.grown.append(count)

    def table(self) -> list[dict]:
        header = [self._text(h) for h in self.rows[0]]
        return [{h: self._text(r[i]) if i < len(r) else "" for i, h in enumerate(header)} for r in self.rows[1:]]


# ------------------------------------------------------------------ Google over HTTP
class FakeGoogle:
    """The `request` function of GoogleAPI. Answers a draft create with an id, keeps the parsed message of every draft in `drafts`, and
    fails the test on anything else (so a call to a send endpoint, or any other write, cannot slip through)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.drafts: list = []

    def __call__(self, method, url, headers=None, params=None, json=None):
        self.calls.append((method, url, headers, params, json))
        if method == "POST" and url == "https://gmail.googleapis.com/gmail/v1/users/me/drafts":
            msg = message_from_bytes(base64.urlsafe_b64decode(json["message"]["raw"]), policy=policy.default)
            self.drafts.append(msg)
            return Resp({"id": f"draft-{len(self.drafts)}", "message": {"id": "m"}})
        raise AssertionError(f"unexpected Google call: {method} {url}")


# ------------------------------------------------------------------ DNS and the Fetcher
class Rec:
    def __init__(self, exchange: str):
        self.exchange = exchange


class FakeResolver:
    """table: {(domain, "MX"|"A"): [records] | an exception instance}; anything not listed is NXDOMAIN."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.asked: list[tuple[str, str]] = []

    def resolve(self, domain, rdtype, tcp=False):
        self.asked.append((domain, rdtype))
        v = self.table.get((domain, rdtype), dns.resolver.NXDOMAIN())
        if isinstance(v, Exception):
            raise v
        return v


def mail_domains(*domains) -> FakeResolver:
    return FakeResolver({(d, "MX"): [Rec(f"mx.{d}.")] for d in domains})


class FakeFetcher:
    """pages: {url: html | Exception | callable(url, kw) -> reply | Resp}; an unlisted URL is a 404. `requested` keeps every URL and kw."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.requested: list[tuple[str, dict]] = []

    def request(self, url, **kw):
        self.requested.append((url, kw))
        v = self.pages.get(url)
        if callable(v):
            v = v(url, kw)
        if isinstance(v, Exception):
            raise v
        if isinstance(v, Resp):
            return v
        return Resp("", 404) if v is None else Resp(v)


# ------------------------------------------------------------------ an applied job
def make_applied(settings, db: DB, job: dict, tmp_path: Path, *, hook: bool = True, letter: bool = True, applied_at: str = APPLIED_AT,
                 **job_fields) -> dict:
    """The `job` fixture as an applied job: a report.json (hook, ATS, keywords, context), a CV PDF and (letter=True) a letter PDF in its own
    output folder. Returns the job row as a dict."""
    out = tmp_path / "out" / job["id"].replace(":", "_")
    out.mkdir(parents=True)
    cv, lt = out / "Ada Lovelace CV.pdf", out / "Ada Lovelace Cover Letter.pdf"
    cv.write_bytes(b"%PDF-1.4 fake cv")
    lt.write_bytes(b"%PDF-1.4 fake letter")
    rep = {
        "out_dir": str(out), "cv_pdf": str(cv), "letter_pdf": str(lt) if letter else None, "team": "",
        "ats": {"strict": 78.46, "ceiling": 91.04, "combined": 80.0, "letter": 50.0, "gate": "pass"}, "stretch": False,
        "claimable": ["Python", "LangChain"],
        "keywords": [{"term": "LangChain", "weight": 3, "required": True}, {"term": "Python", "weight": 2, "required": True},
                     {"term": "Kubernetes", "weight": 5, "required": True}, {"term": "CI/CD", "weight": 1, "required": False}],
        "context": {"experience": [{"bullet_ids": ["az1", "az2"]}],
                    "projects": [{"name": "Tool Agent", "bullet_ids": ["p1"]}, {"name": "Forecaster", "bullet_ids": ["p2", "p3"]}]},
        "hook": ({"problem": "agents that recover", "proof_bullet_id": "az2", "number": "19,213", "headline": "agents that recover from tool failure",
                  "profile_line": "p", "letter_sentence": HOOK_LINE, "org": "Northwind Research Lab", "source": "library",
                  "hook_id": "h_agent_harness", "flags": []} if hook else {}),
    }
    (out / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    row = {"posted_at": "2026-10-16T09:00:00+00:00", "posted_at_source": "greenhouse.first_published", **job}
    db.upsert_job(row)
    db.update_job(job["id"], status="applied", applied_at=applied_at, out_dir=str(out), family="ai_engineer", sponsor_match="exact",
                  sponsor_route="Skilled Worker", sponsor_rating="A", **job_fields)
    return dict(db.job(job["id"]))
