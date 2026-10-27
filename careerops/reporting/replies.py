"""Reply watch (Phase D1): what the companies you applied to have written back, proposed to you one line at a time. READ-ONLY towards Gmail.

  * Which mail: Gmail messages (the read-only scope, GET only) from the employer's own domain of an applied job, or from an applicant-tracking mailer
    (greenhouse-mail.io, hire.lever.co ...) when the sender's name or the subject names the company. A job with a final outcome is not watched.
  * What it says: `classify` (rules, no model, no network) answers interview | assessment | rejection | info | noise. The text of a mail is DATA:
    only these rules read it, nothing in it is ever obeyed, and a mail that talks to this tool ("ignore previous instructions ...") is flagged.
  * What happens: an interview, assessment or rejection is PRINTED as a proposal, `Trainline: rejection (22 Oct mail) -> set Status "Rejected"? [y/N]`.
    Only a `y` writes: the Status cell of the job's row in the tracker Sheet (tracker.set_status, that cell alone) and the outcome in the database
    (so the funnel and `careerops followups` know there was a reply). Anything else, and a prompt that is cut short, writes nothing. A mail you
    answered is not asked about again (data/discover/replies_seen.json); one you were not asked about (no terminal) is.
  * It never replies, never sends, never labels or deletes a mail.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping

from .. import contacts as contacts_mod
from .. import tracker
from ..db import DB
from ..discovery import gmail
from ..discovery.http import Blocked, FetchError, Fetcher
from ..discovery.register import normalise
from . import outcomes

KINDS = ("interview", "assessment", "rejection", "info", "noise")
STATUS_FOR = {"interview": "Interview", "assessment": "Assessment", "rejection": "Rejected"}      # what the Sheet's Status cell is set to
EVENT_FOR = {"interview": "interview", "assessment": "screen", "rejection": "rejected"}            # the outcome logged (reporting/outcomes.py)
DEFAULT_DAYS, MAX_MESSAGES, CHUNK = 30, 80, 25            # window of the Gmail search, mails read per run, `from:` terms per search
BODY_CHARS = 4000                                         # the verdict is at the top of a mail; the quoted history below it is not read
SEEN_FILE = "replies_seen.json"
FINAL = ("rejected", "withdrawn", "offer")                # a job with one of these outcomes is not watched
# Mailers of applicant-tracking systems: a mail from one of them is yours only if it names the company.
ATS_MAILERS = ("greenhouse-mail.io", "greenhouse.io", "hire.lever.co", "lever.co", "ashbyhq.com", "smartrecruiters.com", "myworkday.com",
               "workday.com", "icims.com", "workablemail.com", "workable.com", "bamboohr.com", "teamtailor.com", "recruitee.com", "personio.de",
               "personio.com", "jobvite.com", "pinpointhq.com", "successfactors.com", "oraclecloud.com", "taleo.net")
GENERIC_FIRST_WORDS = frozenset({"global", "digital", "group", "technologies", "technology", "solutions", "systems", "international", "financial",
                                 "capital", "health", "healthcare", "analytics", "software", "consulting", "services", "partners", "ventures"})
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ------------------------------------------------------------------ reading a mail
def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\b(won|can|isn|aren|doesn|didn|haven|hasn|wouldn|shouldn|couldn)'t\b", lambda m: ("will" if m[1] == "won" else m[1]) + " not", t, flags=re.I)
    t = re.sub(r"\b(\w+)n't\b", r"\1 not", t, flags=re.I)
    t = re.sub(r"\b(we|i|you|they)'ve\b", r"\1 have", t, flags=re.I)
    return " ".join(t.lower().split())


def body_text(html: str, plain: str) -> str:
    """The plain-text part of a mail when it has one, else the HTML with its tags, styles and scripts taken out."""
    if plain and len(plain.strip()) >= 20:
        return plain
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    return " ".join(htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", h)).split())


def _rx(*alts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{a})" for a in alts), re.I)


REJECTION = _rx(
    r"\bregret to (?:inform|tell|let) you\b",
    r"\bnot be (?:moving|going|proceeding|progressing|taking|pursuing)\b[^.]{0,50}\b(?:forward|ahead|further|application|candidacy)\b",
    r"\bnot (?:to )?(?:move|go|proceed|progress) (?:forward|ahead|further)\b",
    r"\b(?:decided|chosen|opted) to (?:move|go|proceed|progress|continue)(?: forward)? with (?:other|another|different|a different) (?:candidates?|applicants?|profiles?)\b",
    r"\b(?:application|candidacy)\b[^.]{0,60}\b(?:unsuccessful|not successful|declined|rejected)\b",
    r"\b(?:unsuccessful|not successful) (?:on this occasion|this time)\b",
    r"\bnot been successful\b",
    r"\bnot (?:been )?(?:selected|shortlisted)\b",
    r"\b(?:position|role|vacancy) (?:has|have) (?:now |already )?(?:been )?(?:filled|closed)\b",
    r"\bunfortunately\b[^.]{0,80}\b(?:your application|other candidates)\b",
)
_INVITE_CUE = (r"(?:invite|invitation|invited|schedule|scheduling|arrange|arranging|availability|available|book|booking|calendly|"
               r"(?:select|pick|choose) a (?:time|slot)|would like to (?:speak|talk|meet|chat)|set up|slot)")
_INTERVIEW_WORD = r"(?:interview|phone screen|screening call|video call|intro(?:ductory)? call|introductory chat|meet the team|chat with)"
INTERVIEW = _rx(rf"\b{_INVITE_CUE}\b[^.]{{0,80}}\b{_INTERVIEW_WORD}\b", rf"\b{_INTERVIEW_WORD}\b[^.]{{0,80}}\b{_INVITE_CUE}\b")
_TEST_WORD = (r"(?:assessments?|coding (?:challenge|test|exercise)|take-?home|technical (?:test|task|exercise)|online test|hackerrank|codility|"
              r"codesignal|testgorilla|case study|skills test|psychometric)")
_TEST_CUE = r"(?:complete|completing|invite|invited|link|deadline|within|attached|please|to take|submit)"
ASSESSMENT = _rx(rf"\b{_TEST_CUE}\b[^.]{{0,80}}\b{_TEST_WORD}\b", rf"\b{_TEST_WORD}\b[^.]{{0,80}}\b{_TEST_CUE}\b")
INFO = _rx(r"\bthank you for (?:applying|your application|your interest)\b", r"\bwe have received your application\b",
           r"\byour application (?:has been |was )?(?:received|submitted)\b", r"\bapplication (?:received|confirmation)\b",
           r"\bconfirmation of your application\b")
TO_THE_TOOL = _rx(r"\bignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|messages?)\b", r"\bset (?:the )?status\b",
                  r"\byou are now\b", r"\bsystem prompt\b", r"\bdisregard (?:all|any|the) (?:previous|prior)\b", r"\bas an ai\b")


@dataclass(frozen=True)
class Reply:
    kind: str                  # one of KINDS
    why: str = ""              # the phrase of the mail the rule matched (short)
    anomaly: bool = False      # the mail talks to this tool; it was not obeyed


def _hit(rx: re.Pattern, text: str) -> tuple[int, str] | None:
    m = rx.search(text)
    return (m.start(), " ".join(m.group(0).split())[:80]) if m else None


def classify(subject: str, body: str) -> Reply:
    """interview | assessment | rejection | info | noise, from the subject and the first BODY_CHARS of the body, by rules. A strict rejection
    phrase wins over everything (a rejection may mention a future interview); between an interview and an assessment the one that comes first."""
    text = _plain(f"{subject or ''}. {(body or '')[:BODY_CHARS]}")
    anomaly = bool(TO_THE_TOOL.search(text))
    if hit := _hit(REJECTION, text):
        return Reply("rejection", hit[1], anomaly)
    found = [(h[0], kind, h[1]) for kind, rx in (("interview", INTERVIEW), ("assessment", ASSESSMENT)) if (h := _hit(rx, text))]
    if found:
        _, kind, why = min(found)
        return Reply(kind, why, anomaly)
    if hit := _hit(INFO, text):
        return Reply("info", hit[1], anomaly)
    return Reply("noise", "", anomaly)


# ------------------------------------------------------------------ which mail is yours
@dataclass(frozen=True)
class Watched:
    job_id: str
    company: str
    title: str
    domain: str                # the employer's own domain, "" when it cannot be told
    applied_on: str            # YYYY-MM-DD


def watched(db: DB, settings) -> list[Watched]:
    """Every applied job without a final outcome (rejected, withdrawn, offer), with the employer's domain."""
    final = {r[0] for r in db.conn.execute(f"SELECT job_id FROM outcomes WHERE event IN ({','.join('?' * len(FINAL))})", FINAL)}
    out = []
    for j in db.jobs("applied"):
        if not j["applied_at"] or j["id"] in final:
            continue
        out.append(Watched(j["id"], j["company"], j["title"], contacts_mod.company_domain(db, j), str(j["applied_at"])[:10]))
    return out


def sender_parts(from_header: str) -> tuple[str, str]:
    """(the display name, the domain in lower case) of a From header."""
    m = re.search(r"@([\w.-]+)", from_header or "")
    name = re.sub(r"<[^>]*>", " ", from_header or "").strip(' "')
    return name, (m.group(1).lower().strip(".") if m else "")


def _on(domain: str, root: str) -> bool:
    return bool(root) and (domain == root or domain.endswith("." + root))


def _words(text: str) -> list[str]:
    return normalise(text).split()


def _names(company: str, text: str) -> bool:
    """Is the company named in `text` (its whole normalised name, or its first word when that word is distinctive)?"""
    words, mine = _words(text), _words(company)
    if not mine:
        return False
    n = len(mine)
    if any(words[i:i + n] == mine for i in range(len(words) - n + 1)):
        return True
    first = mine[0]
    return n > 1 and len(first) >= 5 and first not in GENERIC_FIRST_WORDS and first in words


def _title_hits(title: str, text: str) -> int:
    t = set(_words(title))
    return len(t & set(_words(text))) if t else 0


def match(from_header: str, subject: str, body: str, jobs: list[Watched]) -> Watched | None:
    """The watched job a mail belongs to: the sender is the employer's domain (exactly, or a subdomain; never a lookalike), or an applicant-tracking
    mailer whose sender name or subject names the company. Several jobs of one company: the one whose title is in the subject, else the latest."""
    name, domain = sender_parts(from_header)
    if not domain:
        return None
    cands = [w for w in jobs if _on(domain, w.domain)]
    if not cands and any(_on(domain, m) for m in ATS_MAILERS):
        cands = [w for w in jobs if _names(w.company, f"{name} {subject}")]
    if not cands:
        return None

    def score(w: Watched) -> tuple[bool, int, str]:
        hits = _title_hits(w.title, subject)
        return hits > 0 and hits == len(set(_words(w.title))), hits, w.applied_on
    return max(cands, key=score)


def queries(jobs: list[Watched], days: int = DEFAULT_DAYS) -> list[str]:
    """The Gmail searches: the employers' domains, CHUNK `from:` terms to a search, then the applicant-tracking mailers."""
    domains = sorted({w.domain for w in jobs if w.domain})
    groups = [[f"from:{d}" for d in domains[i:i + CHUNK]] for i in range(0, len(domains), CHUNK)]
    groups.append([f"from:{d}" for d in ATS_MAILERS])
    return [f"({' OR '.join(g)}) newer_than:{int(days)}d" for g in groups if g]


# ------------------------------------------------------------------ what was found
@dataclass
class Found:
    mid: str
    job: Watched
    reply: Reply
    day: str                   # YYYY-MM-DD the mail reached the mailbox
    subject: str

    @property
    def status(self) -> str:
        return STATUS_FOR.get(self.reply.kind, "")


@dataclass
class Summary:
    matched: int = 0
    proposed: int = 0
    written: int = 0
    info: int = 0
    noise: int = 0


def _short_day(day: str) -> str:
    try:
        d = date.fromisoformat(day)
        return f"{d.day} {MONTHS[d.month - 1]}"
    except ValueError:
        return day or "undated"


def proposal(f: Found, several: bool = False) -> str:
    """`Trainline: rejection (22 Oct mail) -> set Status "Rejected"? [y/N]` (the role is named when the company has several applications)."""
    who = f.job.company + (f" ({f.job.title})" if several else "")
    return f'{who}: {f.reply.kind} ({_short_day(f.day)} mail) → set Status "{f.status}"? [y/N] '


def _seen(settings) -> tuple[set[str], Any]:
    path = settings.path("discover") / SEEN_FILE
    try:
        return set(json.loads(path.read_text(encoding="utf-8"))), path
    except (OSError, ValueError):
        return set(), path


def _remember(path, ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids)[-2000:]), encoding="utf-8")
    except OSError:
        pass


def collect(db: DB, settings, fetcher: Fetcher, token: str, jobs: list[Watched], seen: set[str], *, days: int = DEFAULT_DAYS,
            limit: int = MAX_MESSAGES) -> tuple[list[Found], set[str], Summary]:
    """Read the mails of the watched companies that were not read before. -> (the proposals and infos, the ids read, the counts)."""
    ids: list[str] = []
    for q in queries(jobs, days):
        for mid in gmail.list_message_ids(fetcher, token, q, limit):
            if mid not in ids:
                ids.append(mid)
    out: list[Found] = []
    read: set[str] = set()
    count = Summary()
    for mid in [m for m in ids if m not in seen][:limit]:
        msg = gmail.get_message(fetcher, token, mid)
        frm, subject, html, plain = gmail.message_text(msg)
        read.add(mid)
        body = body_text(html, plain)
        job = match(frm, subject, body, jobs)
        if job is None:
            continue
        count.matched += 1
        reply = classify(subject, body)
        if reply.kind == "noise":
            count.noise += 1
            continue
        out.append(Found(mid, job, reply, gmail.message_date(msg), " ".join((subject or "").split())[:120]))
    return out, read, count


def _logged(db: DB, f: Found) -> bool:
    return bool(db.conn.execute("SELECT 1 FROM outcomes WHERE job_id=? AND event=?", (f.job.job_id, EVENT_FOR.get(f.reply.kind, "-"))).fetchone())


def apply_yes(db: DB, settings, f: Found, *, sheet: tracker.Sheet | None, tab: str | None, say: Callable[[str], None]) -> None:
    """What a `y` does: the outcome in the database, then the Status cell of the Sheet. The Sheet failing is printed and never undoes the outcome."""
    outcomes.record(db, f.job.job_id, EVENT_FOR[f.reply.kind], f.day or None, note=f"reply mail: {f.subject}"[:200], source="reply-watch")
    db.log(f.job.job_id, "reply-watch", {"kind": f.reply.kind, "status": f.status, "mail_day": f.day})
    try:
        if sheet is None:
            if not tracker.configured(settings):
                raise tracker.TrackerError("tracker.sheet_id is not set in config/settings.yaml")
            if (why := tracker.scope_problem(settings)):
                raise tracker.TrackerError(why)
            sheet, tab = tracker.client(settings)
        done = tracker.set_status(sheet, tab or tracker.settings_for(settings)[1], f.job.job_id, f.status)
        say(f"  {f.job.company}: Status {f.status!r} " + ("written to the Sheet." if done == "updated" else "was already in the Sheet."))
    except Exception as e:  # noqa: BLE001  (Google, a missing row, no sign-in: the outcome is logged, the Sheet can be fixed by hand)
        say(f"  {f.job.company}: logged as {EVENT_FOR[f.reply.kind]}; Sheet not updated ({e})")


def run(db: DB, settings, fetcher: Fetcher, token: str, *, ask: Callable[[str], str] | None, say: Callable[[str], None] = print,
        sheet: tracker.Sheet | None = None, tab: str | None = None, days: int = DEFAULT_DAYS, limit: int = MAX_MESSAGES) -> Summary:
    """The whole watch. `ask` is `input` on a terminal and None otherwise (the proposals are printed, nothing is asked or remembered)."""
    jobs = watched(db, settings)
    if not jobs:
        return Summary()
    seen, seen_path = _seen(settings)
    found, read, summary = collect(db, settings, fetcher, token, jobs, seen, days=days, limit=limit)
    several = {w.company for w in jobs if sum(x.company == w.company for x in jobs) > 1}
    done = read - {f.mid for f in found if f.status}                    # read, and nothing to ask about: not read again next time
    for f in found:
        if not f.status:
            summary.info += 1
            say(f"{f.job.company}: info ({_short_day(f.day)} mail) — {f.reply.why or f.subject}; nothing to change.")
            continue
        if _logged(db, f):
            done.add(f.mid)
            continue
        summary.proposed += 1
        line = proposal(f, f.job.company in several)
        if f.reply.anomaly:
            say(f"  note: the mail from {f.job.company} addresses this tool; that text was ignored (only the rules above read it).")
        if ask is None:
            say(line.rstrip() + "   (no terminal: run `careerops discover --gmail-alerts` in one to answer)")
            continue
        try:
            answer = ask(line)
        except (EOFError, KeyboardInterrupt):
            say("(cut short: nothing was written for this mail; it is asked about again next time)")
            break
        done.add(f.mid)
        if answer.strip().lower() in ("y", "yes"):
            apply_yes(db, settings, f, sheet=sheet, tab=tab, say=say)
            summary.written += 1
    if done - seen:
        _remember(seen_path, seen | done)
    return summary
