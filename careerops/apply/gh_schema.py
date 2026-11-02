"""The public Greenhouse question schema of a job (v2.5): the plan of a Greenhouse form is made from it in the terminal, before any browser opens.

Greenhouse renders its drop-downs as react-select widgets whose options live in a portal: a DOM scan cannot see them. The board API publishes every
question of a job, with its options, at

    https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true          (job-boards.greenhouse.io, boards.greenhouse.io)
    https://boards-api.eu.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true       (job-boards.eu.greenhouse.io, boards.eu.greenhouse.io)

and each field's `name` is the id of its element on the form ("first_name", "question_123"). The request goes through discovery.http.Fetcher: the
host's robots.txt first, at most one request a second per host, the tool's own User-Agent. A disallow, no answer or anything that is not the schema
gives None, and the plan comes from the DOM scan instead (apply/formscan.py). The answer is cached as `form_schema.json` in the job's output folder,
so a second run asks nothing. Idea from career-ops-hq/career-ops `web/src/lib/apply/greenhouse.ts` (20b9fd3); this is our own Python.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

CACHE = "form_schema.json"
TYPES = {"input_text": "text", "textarea": "textarea", "input_file": "file", "input_hidden": "hidden", "multi_value_single_select": "select",
         "multi_value_multi_select": "multiselect", "boolean": "select"}


@dataclass(frozen=True)
class Board:
    token: str
    job_id: str
    eu: bool

    @property
    def api_url(self) -> str:
        host = "boards-api.eu.greenhouse.io" if self.eu else "boards-api.greenhouse.io"
        return f"https://{host}/v1/boards/{self.token}/jobs/{self.job_id}?questions=true"

    @property
    def key(self) -> str:
        return f"ats:greenhouse:{self.token}:{self.job_id}"


@dataclass
class SchemaField:
    name: str                       # == the DOM id of the element
    label: str                      # the question, without its star
    type: str                       # text | textarea | file | select | multiselect | hidden
    required: bool
    options: list[str] = field(default_factory=list)
    section: str = ""               # "questions" | "location" | "compliance" | "demographic"

    def as_dict(self) -> dict:
        return asdict(self)


def parse(url: str | None) -> Board | None:
    """The board token and job id of a Greenhouse job-board URL (US or EU host, `.../{token}/jobs/{id}` or the embed form `?for=&token=`); None for
    any other URL."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if not (host == "greenhouse.io" or host.endswith(".greenhouse.io")) or host.startswith("boards-api"):
        return None
    eu = ".eu." in f".{host}"
    m = re.search(r"/([^/]+)/jobs/(\d+)", u.path)
    if m and m.group(1) != "embed":
        return Board(m.group(1), m.group(2), eu)
    q = parse_qs(u.query)
    if q.get("for") and q.get("token") and q["token"][0].isdigit():
        return Board(q["for"][0], q["token"][0], eu)
    return None


def _clean(label: str) -> str:
    return re.sub(r"\s*[*✱]+\s*$", "", " ".join((label or "").split()))


def fields_of(data: Any) -> list[SchemaField]:
    """Every field of a schema answer, in the form's order: `questions`, then `location_questions`, `compliance` and `demographic_questions`."""
    if not isinstance(data, dict):
        return []
    out: list[SchemaField] = []
    blocks = [("questions", data.get("questions")), ("location", data.get("location_questions"))]
    for c in data.get("compliance") or []:
        if isinstance(c, dict):
            blocks.append(("compliance", c.get("questions")))
    demo = data.get("demographic_questions")
    if isinstance(demo, dict):
        blocks.append(("demographic", demo.get("questions")))
    for section, qs in blocks:
        for q in qs or []:
            if not isinstance(q, dict):
                continue
            label = _clean(str(q.get("label") or ""))
            for f in q.get("fields") or []:
                if not isinstance(f, dict) or not f.get("name"):
                    continue
                opts = [" ".join(str(v.get("label") or "").split()) for v in f.get("values") or [] if isinstance(v, dict)]
                out.append(SchemaField(str(f["name"]), label, TYPES.get(str(f.get("type") or ""), "text"), bool(q.get("required")),
                                       [o for o in opts if o], section))
    return out


def load(folder: Path | None, board: Board, fetch_json: Callable[[str], Any] | None, out: Callable[[str], None] = print) -> list[SchemaField] | None:
    """The schema's fields: the cached `form_schema.json` of the job's folder, else one robots-checked request (`fetch_json(url)`, normally
    `Fetcher.json(url, what=...)`), cached when it holds at least one field. None when it cannot be had (the DOM scan plans the form instead)."""
    cache = folder / CACHE if folder else None
    if cache is not None and cache.is_file():
        try:
            got = fields_of(json.loads(cache.read_text(encoding="utf-8")))
            if got:
                return got
        except (OSError, ValueError):
            pass
    if fetch_json is None:
        return None
    try:
        data = fetch_json(board.api_url)
    except Exception as e:  # noqa: BLE001  (robots.txt disallows, no answer, 401/403/429, not JSON: the DOM scan plans the form)
        out(f"  Greenhouse question schema not used ({str(e).splitlines()[0][:140]}): the form is planned from a page scan instead")
        return None
    got = fields_of(data)
    if not got:
        out("  Greenhouse question schema: no questions in the answer; the form is planned from a page scan instead")
        return None
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return got
