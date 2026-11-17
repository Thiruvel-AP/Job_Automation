"""The Gmail label source (v3.8): `careerops discover --gmail-label [NAME] [--since 7d]`.

A Gmail filter puts every LinkedIn job email into a label (settings.yaml: discover.gmail.labels). This module sorts each email of the label by type,
reads the job cards of the alerts and recommendations, and keeps the bookkeeping in SQLite (db.py: gmail_label_messages, linkedin_cards,
linkedin_applied). The pipeline around it lives in discover.py (`Discover.from_gmail_label`).

What was already there (v3.7): gmail.py read LinkedIn alerts found by SENDER (`discover --gmail-alerts`, a Gmail `q=` search on the sender domains) with
one parser (`gmail.parse_alert`). This module reuses that parser (extended with job id, work type, salary and the Easy Apply marker) and adds only what
a label needs: the label looked up by name, the type of each email, the "applied through LinkedIn" memory and the report.

Hard limits, all tested: Gmail is read only (labels.list, messages.list, messages.get under gmail.readonly: nothing is labelled, archived, marked read or
deleted); a LinkedIn link is stored as text and NEVER requested; the emails carry no job ad, so the full ad comes from the employer's own board or from
you (`add-jd`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

from . import candidate as cand
from . import gmail
from . import register as sponsors

SOURCE = "linkedin_email"             # the `source` every job found through the label keeps
JOB_ALERT, JOB_RECOMMENDATION, APPLICATION_SENT, APPLICATION_UPDATE, OTHER = (
    "job_alert", "job_recommendation", "application_sent", "application_update", "other")
KINDS = (JOB_ALERT, JOB_RECOMMENDATION, APPLICATION_SENT, APPLICATION_UPDATE, OTHER)
CARD_KINDS = (JOB_ALERT, JOB_RECOMMENDATION)
NEEDS_JD = "needs_jd"
APPLIED = "already applied (LinkedIn)"

_SENT = re.compile(r"your application was sent to", re.I)
_UPDATE = re.compile(r"viewed your application|your application (?:to|at|for)\b|application (?:update|status)|your application was (?:viewed|downloaded)", re.I)
_ALERT = re.compile(r"\bnew jobs?\b|\bjob alert\b|\bnew job (?:match|for)", re.I)
_RECOMMENDATION = re.compile(r"jobs? you may be interested in|\bis hiring\b|top job picks|recommended for you|jobs? (?:picked|recommended)|jobs? similar to", re.I)


# ------------------------------------------------------------------ the type of an email
def classify(from_header: str, subject: str) -> str:
    """The type of one email by its sender and subject. Only a sender at linkedin.com is anything but `other` (a recruiter's InMail, a newsletter,
    a message from a person all end up there too: counted and logged, never parsed as jobs)."""
    if gmail.sender_kind(from_header) != "linkedin":
        return OTHER
    sender = (from_header or "").lower()
    subject = " ".join((subject or "").split())
    if _SENT.search(subject):
        return APPLICATION_SENT
    if _UPDATE.search(subject):
        return APPLICATION_UPDATE
    if "jobalerts-noreply" in sender or _ALERT.search(subject):
        return JOB_ALERT
    if "jobs-noreply" in sender or _RECOMMENDATION.search(subject):
        return JOB_RECOMMENDATION
    return OTHER


def parse_since(text: str | None, default: str = "30d") -> timedelta:
    """'30d' / '7d' / '12h' / '2w' -> a timedelta. ValueError for anything else."""
    m = re.fullmatch(r"\s*(\d+)\s*([hdw])\s*", str(text or default).lower())
    if not m:
        raise ValueError(f"--since {text!r}: use a number and h, d or w (for example 7d)")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"h": "hours", "d": "days", "w": "weeks"}[unit]: n})


# ------------------------------------------------------------------ "your application ..." emails
@dataclass
class Application:
    kind: str
    company: str = ""
    title: str = ""
    job_id: str = ""
    status: str = "applied"          # applied | viewed | rejected | other


def _first_job(html: str) -> tuple[str, str]:
    """(title, linkedin job id) of the first job link of an email body, '' when there is none."""
    p = gmail._Flow()
    p.feed(html or "")
    p.close()
    for it in p.items:
        if it[0] == "a":
            link = gmail.job_link("linkedin", it[1])
            if link and not gmail.NOISE.match(it[2]):
                return it[2], re.search(r"(\d{6,})$", link).group(1)
    return "", ""


def parse_application(kind: str, subject: str, html: str, text: str = "") -> Application:
    """Company, title and job id of an `application_sent` / `application_update` email. A part that cannot be read is left empty, never guessed."""
    subject = " ".join((subject or "").split())
    title, job_id = _first_job(html)
    app = Application(kind, title=title, job_id=job_id)
    if kind == APPLICATION_SENT:
        m = re.search(r"sent to\s+(.+?)\s*$", subject, re.I)
        app.company = (m.group(1) if m else "").strip(" .!")
        return app
    m = re.match(r"(.+?)\s+viewed your application", subject, re.I)
    if m:
        app.company, app.status = m.group(1).strip(), "viewed"
        return app
    m = re.search(r"your application (?:to|for)\s+(.+?)\s+at\s+(.+?)(?:\s+(?:was|has|is)\b.*)?$", subject, re.I)
    if m:
        app.title, app.company = app.title or m.group(1).strip(), m.group(2).strip(" .!")
    else:
        m = re.search(r"your application (?:to|at)\s+(.+?)(?:\s+(?:was|has|is)\b.*)?$", subject, re.I)
        app.company = (m.group(1) if m else "").strip(" .!")
    body = f"{subject} {text}".lower()
    app.status = ("rejected" if re.search(r"unfortunately|not (?:been )?selected|not moving forward|decided to (?:move|pursue)|no longer (?:being )?consider", body)
                  else "viewed" if re.search(r"\bviewed\b|downloaded", body) else "other")
    return app


# ------------------------------------------------------------------ matching
def key_of(company: str, title: str) -> str:
    """The duplicate key of company + title: normalised, so 'Acme Ltd' / 'ACME Limited' and 'Data Scientist (m/f/d)' / 'Data Scientist' agree."""
    return f"{sponsors.normalise(company)}|{cand.norm_title(title)}"


def token_set_ratio(a: str, b: str) -> float:
    """Similarity 0-1 of two titles ignoring word order and repeated words: the common words, then each side's own, compared the way
    fuzzywuzzy's token_set_ratio does. 'Machine Learning Engineer' and 'Engineer, Machine Learning' are 1.0."""
    ta, tb = set(cand.norm_title(a).split()), set(cand.norm_title(b).split())
    if not ta or not tb:
        return 0.0
    common = " ".join(sorted(ta & tb))
    one = (common + " " + " ".join(sorted(ta - tb))).strip()
    two = (common + " " + " ".join(sorted(tb - ta))).strip()
    return max(SequenceMatcher(None, x, y).ratio() for x, y in ((common, one), (common, two), (one, two)))


# ------------------------------------------------------------------ the store (SQLite; an in-memory copy for a dry run)
@dataclass
class Store:
    """What was read before: message ids, cards (job id + key + time) and the jobs applied to through LinkedIn. Reads are from memory (loaded once);
    every write goes to memory and, unless `persist` is False (a dry run), to the database."""
    db: object
    persist: bool = True
    reread: bool = False                       # --reread: ignore every read mark (the marks are still written)
    messages: set[str] = field(default_factory=set)
    cards: list[dict] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)

    def __post_init__(self):
        c = self.db.conn
        self.messages = {r[0] for r in c.execute(
            "SELECT m.message_id, m.kind, COALESCE(m.cards_parsed, (SELECT COUNT(*) FROM linkedin_cards k WHERE k.message_id = m.message_id)) "
            "FROM gmail_label_messages m") if counts_as_read(r[1], r[2])}
        self.cards = [dict(r) for r in c.execute("SELECT linkedin_job_id, key, received_at, id, message_id FROM linkedin_cards")]
        self.applied = [dict(r) for r in c.execute("SELECT id, company, title, key, linkedin_job_id, status FROM linkedin_applied")]

    # ---- messages (v3.9: a message is READ only once it was classified and parsed; see `counts_as_read`)
    def has_message(self, mid: str) -> bool:
        """True when the message was read before and this run does not re-read it (`--reread` ignores every read mark)."""
        return not self.reread and mid in self.messages

    def add_message(self, mid: str, label: str, kind: str, received: str, subject: str, cards_parsed: int = 0) -> bool:
        """Record (message_id, source, type, cards_parsed, parsed_at). Returns True when the message now counts as read; a card email (job_alert /
        job_recommendation) with 0 cards is stored but stays unread, so the next run fetches it again."""
        read = counts_as_read(kind, cards_parsed)
        if read:
            self.messages.add(mid)
        else:
            self.messages.discard(mid)
        if self.persist:
            with self.db.conn:
                self.db.conn.execute("INSERT OR REPLACE INTO gmail_label_messages (message_id, label, kind, received_at, subject, processed_at, cards_parsed, parsed_at) "
                                     "VALUES (?,?,?,?,?,?,?,?)", (mid, label, kind, received, subject[:200], _now(), int(cards_parsed), _now()))
        return read

    def repair(self) -> int:
        """One-time repair of marks written before v3.9 (they were set whatever the parse gave): a mark with no stored type is cleared, and a card
        email whose stored cards are none is cleared too (it is fetched and parsed again). Marks that hold cards get their `cards_parsed` filled in.
        Returns the number of marks cleared; the database is copied first when any is."""
        c = self.db.conn
        bad = [r[0] for r in c.execute(
            "SELECT m.message_id FROM gmail_label_messages m WHERE (m.kind IS NULL OR m.kind = '') "
            "OR (m.cards_parsed IS NULL AND m.kind IN (?,?) AND NOT EXISTS (SELECT 1 FROM linkedin_cards k WHERE k.message_id = m.message_id))", CARD_KINDS)]
        if bad and self.persist:
            from .. import pathstore
            pathstore.backup_db(self.db, "pre-label-repair")
        if self.persist:
            with c:
                c.executemany("DELETE FROM gmail_label_messages WHERE message_id = ?", [(m,) for m in bad])
                c.execute("UPDATE gmail_label_messages SET cards_parsed = (SELECT COUNT(*) FROM linkedin_cards k WHERE k.message_id = gmail_label_messages.message_id), "
                          "parsed_at = COALESCE(parsed_at, processed_at) WHERE cards_parsed IS NULL")
        self.messages -= set(bad)
        return len(bad)

    # ---- cards
    def card_before(self, job_id: str, key: str, received: str, days: int, message_id: str = "") -> str | None:
        """The earlier card this one repeats: the same LinkedIn job id (any age), else the same company + title seen within `days` days of this one.
        Returns that card's job id (or `card <n>` when it has none)."""
        cards = [c for c in self.cards if not (message_id and c.get("message_id") == message_id)]       # a re-read email is not a duplicate of itself
        if job_id:
            hit = next((c for c in cards if c.get("linkedin_job_id") == job_id), None)
            if hit:
                return job_id
        when = _parse(received)
        for c in cards:
            if c.get("key") == key and key != "|":
                then = _parse(c.get("received_at"))
                if when is None or then is None or abs((when - then).total_seconds()) <= days * 86400:
                    return c.get("linkedin_job_id") or f"card {c.get('id')}"
        return None

    def add_card(self, row: dict) -> None:
        mem = {"linkedin_job_id": row.get("job_id", ""), "key": row["key"], "received_at": row["received"], "id": len(self.cards) + 1,
               "message_id": row.get("message_id", "")}
        same = next((c for c in self.cards if c.get("message_id") and c.get("message_id") == mem["message_id"]
                     and c.get("key") == row["key"] and (c.get("linkedin_job_id") or "") == (row.get("job_id") or "")), None)
        if same is not None:                               # a re-read email: its card is updated, not added twice
            same["received_at"] = row["received"]
            if self.persist:
                with self.db.conn:
                    self.db.conn.execute("UPDATE linkedin_cards SET result=?, job_file=?, location=?, work_type=?, salary=?, easy_apply=? WHERE id=?",
                                         (row["result"], row.get("job_file", ""), row.get("location", ""), row.get("work_type", ""), row.get("salary", ""),
                                          1 if row.get("easy_apply") else 0, same["id"]))
            return
        self.cards.append(mem)
        if self.persist:
            with self.db.conn:
                cur = self.db.conn.execute(
                    "INSERT INTO linkedin_cards (source, linkedin_job_id, company, title, location, work_type, salary, easy_apply, link, key, message_id, "
                    "received_at, result, job_file, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (SOURCE, row.get("job_id") or None, row["company"], row["title"], row.get("location", ""), row.get("work_type", ""),
                     row.get("salary", ""), 1 if row.get("easy_apply") else 0, row.get("link", ""), row["key"], row.get("message_id", ""),
                     row["received"], row["result"], row.get("job_file", ""), _now()))
                mem["id"] = cur.lastrowid

    # ---- applied through LinkedIn
    def applied_hit(self, job_id: str, key: str) -> dict | None:
        for a in self.applied:
            if (job_id and a.get("linkedin_job_id") == job_id) or (a.get("title") and a.get("key") == key and key != "|"):
                return a
        return None

    def add_applied(self, app: Application, received: str, mid: str) -> None:
        key = key_of(app.company, app.title)
        row = {"id": len(self.applied) + 1, "company": app.company, "title": app.title, "key": key, "linkedin_job_id": app.job_id, "status": "applied"}
        self.applied.append(row)
        if self.persist:
            with self.db.conn:
                cur = self.db.conn.execute(
                    "INSERT INTO linkedin_applied (company, title, key, linkedin_job_id, applied_at, status, channel, message_id, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (app.company, app.title, key, app.job_id or None, received, "applied", "LinkedIn", mid, _now()))
                row["id"] = cur.lastrowid

    def update_applied(self, app: Application) -> bool:
        """Set the status of the application an update email is about (matched by job id, else company + title, else the company's latest). False when
        none is known: an update never creates a record."""
        norm_c = sponsors.normalise(app.company)
        match = None
        for a in reversed(self.applied):
            if app.job_id and a.get("linkedin_job_id") == app.job_id:
                match = a
            elif app.title and a.get("key") == key_of(app.company, app.title):
                match = a
            elif not app.title and norm_c and sponsors.normalise(a["company"]) == norm_c:
                match = a
            if match:
                break
        if match is None:
            return False
        match["status"] = app.status
        if self.persist:
            with self.db.conn:
                self.db.conn.execute("UPDATE linkedin_applied SET status=?, updated_at=? WHERE id=?", (app.status, _now(), match["id"]))
        return True


def counts_as_read(kind: str | None, cards_parsed: int | None) -> bool:
    """A message is read once it was classified (it has a type) and, if it is a job email, at least one card came out of it. NULL cards_parsed
    (a mark from before v3.9) counts as read for a type that carries no cards; `Store.repair` settles the card emails."""
    if not kind:
        return False
    if kind in CARD_KINDS:
        return bool(cards_parsed)
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def applied_pairs(db) -> list[tuple[str, str]]:
    """(company, title) of every job applied to through LinkedIn: what `discover` loads as 'already applied (LinkedIn)'."""
    try:
        return [(r["company"], r["title"] or "") for r in db.conn.execute("SELECT company, title FROM linkedin_applied WHERE title <> ''")]
    except Exception:  # noqa: BLE001  (a database from before v3.8 that was opened without the schema)
        return []


def needs_jd_cards(db) -> list[dict]:
    """The cards that still need their ad pasted: newest first, one per job id / company + title."""
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM linkedin_cards WHERE result = ? ORDER BY received_at DESC, id DESC", (NEEDS_JD,))]
    seen, out = set(), []
    for r in rows:
        k = r.get("linkedin_job_id") or r["key"]
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ------------------------------------------------------------------ sanitised samples (data/samples/linkedin/, git-ignored)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w/])\+?\d[\d\s().-]{8,}\d(?![\w/])")
URL = re.compile(r"https?://[^\s\"'<>)]+")


def sanitise(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """A copy of an email with the user's name, every email address and phone number replaced by placeholders, and every link cut at its '?'
    (tracking tokens in a LinkedIn link identify the reader)."""
    out = URL.sub(lambda m: re.split(r"[?#]", m.group(0), maxsplit=1)[0], text or "")
    out = EMAIL.sub("<email>", out)
    out = PHONE.sub("<phone>", out)
    for n in sorted({x.strip() for x in names if x and len(x.strip()) >= 2}, key=len, reverse=True):
        out = re.sub(rf"(?<!\w){re.escape(n)}(?!\w)", "<name>", out, flags=re.I)
    return out


def sample_path(settings, kind: str) -> Path:
    return settings.path("samples") / "linkedin" / f"{kind}.txt"


def save_sample(settings, kind: str, from_header: str, subject: str, html: str, text: str, names: list[str] | tuple[str, ...] = ()) -> Path | None:
    """Keep the latest sanitised email of this type for debugging the parser. Returns the path, None when it could not be written."""
    body = "\n".join(["From: <sender>@" + (re.search(r"@([\w.-]+)", from_header or "") or [None, "?"])[1], f"Subject: {sanitise(subject, names)}", "",
                      "--- html ---", sanitise(html, names)[:60000], "--- text ---", sanitise(text, names)[:20000]])
    path = sample_path(settings, kind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        return None
    return path


def user_names(settings) -> list[str]:
    """The user's first and last name from applicant.yaml (read only), for sanitising samples. [] when the file cannot be read."""
    try:
        from ..config import load_yaml
        a = load_yaml(settings.path("applicant")) or {}
    except Exception:  # noqa: BLE001
        return []
    names = [str(a.get(k) or "") for k in ("first_name", "last_name", "full_name", "name")]
    return [x for n in names for x in ([n, *n.split()] if n else [])]


# ------------------------------------------------------------------ the report
def result_group(result: str) -> str:
    for prefix in ("resolved", "duplicate", "filtered"):
        if result.startswith(prefix):
            return prefix
    return result


def format_report(rows: list[dict], kinds: dict[str, int], unparsed: int, tiers: list[int], *, unparsed_mails: list[tuple[str, str]] = (),
                  examples: list[tuple[str, str]] = (), already_read: int = 0) -> list[str]:
    """The one table, newest first (received | title | company | location | sponsor | result), then the counts per email type, per result and per age tier."""
    from . import freshness

    out: list[str] = []
    rows = sorted(rows, key=lambda r: r["received"], reverse=True)
    if rows:
        cols = ("received", "title", "company", "location", "sponsor", "result")
        cells = [[(r["received"][:16].replace("T", " ")), r["title"], r["company"], r["location"], r["sponsor"], r["result"]] for r in rows]
        width = [min(44, max(len(c), *(len(x[i]) for x in cells))) for i, c in enumerate(cols)]
        fmt = lambda xs: " | ".join(str(x)[:width[i]].ljust(width[i]) for i, x in enumerate(xs)).rstrip()      # noqa: E731
        out += [fmt(cols), "-+-".join("-" * w for w in width), *(fmt(x) for x in cells)]
    else:
        out.append("no new job cards")
        if examples:                                  # what the label actually holds, so the cause of "0 cards" can be seen
            out.append("the first emails of the label (sender | subject):")
            out += [f"  {frm[:60]} | {subj}" for frm, subj in examples[:5]]
        elif already_read:
            out.append(f"{already_read} email(s) of the window are already read: `--reread` reads them again")
    by_result: dict[str, int] = {}
    for r in rows:
        by_result[result_group(r["result"])] = by_result.get(result_group(r["result"]), 0) + 1
    out += ["", "emails by type: " + (", ".join(f"{k} {kinds.get(k, 0)}" for k in KINDS) if any(kinds.values()) else "none new"),
            "cards by result: " + (", ".join(f"{k} {v}" for k, v in sorted(by_result.items())) or "none") + (f", unparsed {unparsed}" if unparsed else ""),
            "cards by age: " + ", ".join(f"{n} {tiers[i]}" for i, n in enumerate(freshness.TIER_NAMES))]
    if unparsed_mails:
        out.append(f"unparsed mails (left unread, retried next run): {len(unparsed_mails)}")
        out += [f"  {frm[:60]} | {subj}" for frm, subj in unparsed_mails[:5]]
    return out
