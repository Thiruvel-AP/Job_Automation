"""`careerops init` and `careerops doctor`: create the data layout and check the toolchain."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import requests

# An open item in the fact bank: a TODO, or a `# CONFIRM` marker. The header's prose about the marker is quoted
# ("# CONFIRM"), so a marker directly followed by a quote is not an item.
OPEN_ITEM = re.compile(r'TODO|#\s*CONFIRM\b(?!")')

# `applications_auto` is where the pipeline writes its archive: init creates it only if missing and never touches existing contents.
# `applications` is the user's own folder of past submissions: read-only, init never creates it.
DATA_DIRS = ("register_dir", "companies_house_dir", "outputs", "browser_profile", "logs", "applications_auto", "tmp",
             "calibration")
CALIBRATION_SUBDIRS = ("jds", "reference")      # inside paths.calibration; init adds them, never touches what is in them
FILE_PARENTS = ("db", "excel", "learning", "preferences")
DNC_FILE = "do_not_contact.csv"                 # inside paths.outreach: addresses and domains no outreach draft goes to (contacts.py)


def load_dotenv(path: Path) -> int:
    """Minimal .env loader (KEY=VALUE lines); never overrides variables already set. Returns keys loaded."""
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def _show(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def init(settings) -> list[str]:
    """Create every data folder and a .env from the example. Idempotent."""
    made = []
    for key in DATA_DIRS:
        p = settings.path(key)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            made.append(_show(p, settings.root))
    for sub in CALIBRATION_SUBDIRS:
        p = settings.path("calibration") / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            made.append(_show(p, settings.root))
    for key in FILE_PARENTS:
        p = settings.path(key).parent
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            made.append(_show(p, settings.root))
    if "outreach" in settings.raw["paths"]:
        dnc = settings.path("outreach") / DNC_FILE
        if not dnc.exists():
            dnc.parent.mkdir(parents=True, exist_ok=True)
            dnc.write_text("value,note\n", encoding="utf-8")
            made.append(_show(dnc, settings.root))
    env, example = settings.root / ".env", settings.root / ".env.example"
    if not env.exists() and example.exists():
        shutil.copy(example, env)
        made.append(".env (from .env.example — add your keys)")
    return made


def fact_bank_open_items(path: Path) -> int:
    """How many TODO / `# CONFIRM` markers the fact bank still holds; -1 if the file is missing."""
    return len(OPEN_ITEM.findall(path.read_text(encoding="utf-8"))) if path.exists() else -1


OLLAMA_DEFAULT_CTX = 4096      # what Ollama loads a model with when its Modelfile sets no num_ctx


def _ollama_num_ctx(show: dict) -> int:
    """The context a model is loaded with, from /api/show's `parameters` text ("num_ctx   8192" per line)."""
    m = re.search(r"^\s*num_ctx\s+(\d+)", show.get("parameters") or "", re.M)
    return int(m.group(1)) if m else OLLAMA_DEFAULT_CTX


def ollama_checks(prov: dict) -> list[tuple[str, bool, str]]:
    """Doctor lines for a local Ollama provider: the server answers, the configured model is built (listed in
    /api/tags) and its num_ctx reaches the provider's `context_tokens`. A later check is skipped when an earlier one
    failed, since it could not say anything."""
    name, model = prov["name"], prov["model"]
    host = prov["base_url"].rstrip("/").removesuffix("/v1")
    create = f"ollama create {model} -f config/ollama/Modelfile"
    try:
        tags = requests.get(host + "/api/tags", timeout=2)
        tags.raise_for_status()
        listed = [m.get("name") or m.get("model") or "" for m in tags.json().get("models", [])]
    except (requests.RequestException, ValueError):
        return [(f"{name} at {prov['base_url']}", False, f"start it (ollama serve), then: {create}")]
    out = [(f"{name} at {prov['base_url']}", True, "")]
    wanted = {model, model + ":latest"} if ":" not in model else {model}
    have = bool(wanted & set(listed))
    out.append((f"{name} model {model} is built", have, create))
    want_ctx = prov.get("context_tokens")
    if not have or not want_ctx:
        return out
    try:
        r = requests.post(host + "/api/show", json={"model": model}, timeout=5)
        r.raise_for_status()
        ctx = _ollama_num_ctx(r.json())
    except (requests.RequestException, ValueError):
        return out + [(f"{name} num_ctx of {model}", False, f"/api/show failed; rebuild it: {create}")]
    return out + [(f"{name} num_ctx {ctx} >= context_tokens {want_ctx}", ctx >= want_ctx,
                   f"the model loads with {ctx}, so a longer prompt is cut off silently; set `PARAMETER num_ctx "
                   f"{want_ctx}` in config/ollama/Modelfile, then: {create}")]


def core_skill_checks(path: Path) -> list[tuple[str, bool, str]]:
    """Doctor lines for `skills.core` in the fact bank (read only): one failing line per core skill that is on
    deny_terms or that no bullet, term or other skill row mentions; one passing line when all are fine."""
    from .config import load_yaml
    from .tailoring.skills import core_skill_problems, core_skills

    if not path.exists():
        return [("core skills", False, f"fact bank not found: {path}")]
    fb = load_yaml(path)
    problems = core_skill_problems(fb)
    if problems:
        return [(p, False, "fix skills.core, deny_terms or the evidence in fact_bank.yaml (the pipeline never edits it)")
                for p in problems]
    return [(f"core skills ({len(core_skills(fb))}) all evidenced and none denied", True, "")]


def skill_repeat_checks(path: Path) -> list[tuple[str, object, str]]:
    """Doctor lines for the family `skill_lines` of the fact bank (read only; the user's own verbatim text, never edited): one failing line per skill
    that two items share (`SKILLS repeat "Lambda" in <group A> and <group B>: fix in fact_bank.yaml families.<name>.skill_lines`), or one passing
    line."""
    from .config import load_yaml
    from .tailoring.skills import families, repeat_problems

    if not path.exists():
        return [("skill repeats", False, f"fact bank not found: {path}")]
    fb = load_yaml(path)
    fams = families(fb)
    found = [p for f in fams for p in repeat_problems({str(l["label"]): str(l["items"]) for l in f["skill_lines"]}, family=str(f["id"]))]
    if found:
        return [(p, False, "a skill appears once on the CV; edit the skill_lines in fact_bank.yaml (the pipeline never edits them)") for p in found]
    return [(f"no skill repeats in the fact bank's family skill lines ({len(fams)} families)", True, "")]


def plain_allow_checks(settings) -> list[tuple[str, object, str]]:
    """v3.4: the keyword allow-list of plain style (cv.style), and the part of it the fact bank evidences. Information only."""
    from .config import load_yaml
    from .tailoring import plain as plain_mod

    try:
        fb = load_yaml(settings.path("fact_bank"))
    except Exception:  # noqa: BLE001  (a missing bank is another check's problem)
        fb = {}
    have = plain_mod.evidenced_allow(fb, settings)
    return [(f"plain style ({plain_mod.style(settings)}): allow-list {', '.join(plain_mod.allow_list(settings))}; "
             f"your fact bank evidences {', '.join(have) or 'none'}", True, "")]


def profile_word_checks(path: Path) -> list[tuple[str, object, str]]:
    """Doctor lines for the CV profile's length (Document rules: 100 to 150 words), read only: for each role family, are there words enough in the
    family's `profile_body` and the bank's profile sentences (`profile.sentences`, and the older `profile_facts`; a deny-listed one does not count)
    to reach 100? A bank without families is judged on its sentences alone. A hook's profile line only adds to the total."""
    from .config import load_yaml
    from .tailoring.faithfulness import deny_hits
    from .tailoring.profile import MIN_WORDS, bank_sentences, nwords

    if not path.exists():
        return [("profile words", False, f"fact bank not found: {path}")]
    fb = load_yaml(path)
    deny = fb.get("deny_terms", [])
    own = sum(nwords(s) for s in bank_sentences(fb) if not deny_hits(s, deny))
    fams = [f for f in fb.get("families") or [] if isinstance(f, dict) and f.get("id")]
    if not fams:
        return [(f"profile sentences: {own} of {MIN_WORDS} words available", own >= MIN_WORDS,
                 "" if own >= MIN_WORDS else "add sentences to fact_bank.yaml → profile.sentences (the pipeline never edits it)")]
    out: list[tuple[str, object, str]] = []
    for f in fams:
        have = own + nwords(f.get("profile_body") or "")
        label = f"profile words for family {f['id']}: {have} of {MIN_WORDS} available"
        if have >= MIN_WORDS:
            out.append((label, True, ""))
        else:
            out.append((label + " (add profile sentences to fact_bank.yaml → profile.sentences)", False,
                        "the profile is the hook line, the family's profile_body, then the sentences under profile.sentences; it must reach 100 words and the pipeline never pads it"))
    return out


def ats_structure_checks(settings) -> list[tuple[str, object, str]]:
    """v3.3: the structural ATS check of the newest tailored CV under paths.outputs (read only): its score, grade and lints. A critical issue fails,
    lints or a score under 80 warn, no tailored CV yet says so."""
    import json as json_mod

    from . import ats_structure as AS
    from .config import load_yaml

    try:
        found = sorted(settings.path("outputs").glob("*/report.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        found = []
    if not found:
        return [("ATS structure: no tailored CV yet", "warn", "run `careerops live` (or `careerops run --mock --limit 1`) first")]
    try:
        from .pathstore import read_report

        rep = read_report(found[-1])
        fb = load_yaml(settings.path("fact_bank"))
    except (OSError, ValueError) as e:
        return [(f"ATS structure: {found[-1]} could not be read", "warn", str(e)[:80])]
    st = AS.for_report(rep, fb)
    where = found[-1].parent.name
    if st.cv is None:
        return [(f"ATS structure of the newest CV ({where}): not checked ({st.error})", False, "careerops ats-check for details")]
    codes = ", ".join(dict.fromkeys(f"{l.code} ({l.where})" for l in st.lints)) or "none"
    ok: object = True if st.ok and not st.lints else ("warn" if not st.cv.critical else False)
    crit = "; ".join(i.code for i in st.cv.critical)
    return [(f"ATS structure of the newest CV ({where}): {st.cv.score}/{st.cv.grade}, {len(st.cv.critical)} critical"
             + (f" ({crit})" if crit else "") + f"; lints: {codes}", ok, "careerops ats-check prints every issue and the decision each lint needs")]


def project_public_checks(path: Path) -> list[tuple[str, object, str]]:
    """Doctor warnings for projects (Document rules, A4): every project with `github:` should say `public: true` or `public: false` (a missing key
    is treated as public, so its URL is printed). Read only."""
    from .config import load_yaml

    if not path.exists():
        return [("project public flags", False, f"fact bank not found: {path}")]
    fb = load_yaml(path)
    out: list[tuple[str, object, str]] = []
    linked = 0
    for p in fb.get("projects") or []:
        if not isinstance(p, dict) or not p.get("github"):
            continue
        linked += 1
        name = p.get("name") or p.get("id")
        hint = "add `public: true` or `public: false` to the project in fact_bank.yaml (the pipeline never edits it)"
        if "public" not in p:
            out.append((f"project '{name}' has github: but no public: true/false (treated as public, its URL is printed)", WARN, hint))
        elif not isinstance(p["public"], bool):
            out.append((f"project '{name}': public: {p['public']!r} is neither true nor false (treated as public)", WARN, hint))
    return out or [(f"every project with a github link says whether it is public ({linked})", True, "")]


def hook_lint_checks(path: Path) -> list[tuple[str, object, str]]:
    """Doctor lines for the headlines of `hook_library` in the fact bank (read only): one warning per lint finding (hook.headline_lint:
    over 60 characters, more than one number, a hardware or resource unit), or one passing line. A warning never fails the run, and
    nothing is rewritten."""
    from .config import load_yaml
    from .hook import library, library_lint

    if not path.exists():
        return [("hook library headlines", False, f"fact bank not found: {path}")]
    fb = load_yaml(path)
    found = library_lint(fb)
    if found:
        return [(f"hook '{eid}' headline: {msg}", WARN, "the lint never rewrites; edit the headline in fact_bank.yaml if you agree")
                for eid, msg in found]
    return [(f"hook library headlines ({len(library(fb))}) pass the lint", True, "")]


def hook_claim_checks(path: Path) -> list[tuple[str, object, str]]:
    """v3.5.2: one warning per hook_library entry that claims what its proof bullets do not (tailoring/hook_claims.py; blocked from every CV and
    letter until the user fixes it), or one passing line. Read only."""
    from .config import load_yaml
    from .tailoring import hook_claims

    if not path.exists():
        return [("hook claims", False, f"fact bank not found: {path}")]
    results = hook_claims.check_all(load_yaml(path))
    blocked = {h: c for h, c in results.items() if c}
    if blocked:
        return [(f"hook '{h}' BLOCKED: {c[0].line()}", WARN, "careerops hook-check shows both quotes; fix the hook or its proof in fact_bank.yaml "
                 "(nothing is reworded for you)") for h, c in blocked.items()]
    return [(f"hook claims ({len(results)} hooks) match their proof bullets", True, "")]


PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net")
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)


WARN = "warn"          # a doctor result that is shown but does not fail the run


def visa_checks(a: dict) -> list[tuple[str, object, str]]:
    """The per-country visa answers (apply/visa.py): `visa.home` must be a country this tool knows and `visa.by_country[visa.home]` must
    exist (both fail: without them a remote role cannot be placed or answered), and `visa.by_country.default` should (warn: a job
    anywhere else would have no visa answers)."""
    from .discovery import countries

    visa = a.get("visa") or {}
    home = countries.code(visa.get("home"))
    blocks = visa.get("by_country") or {}
    have_home = bool(home) and any(isinstance(b, dict) and b and countries.place_code(k) == home for k, b in blocks.items() if k != "default")
    have_default = isinstance(blocks.get("default"), dict) and bool(blocks.get("default"))
    return [
        ("applicant visa.home is set to a country", bool(home),
         "write `home: GB` (a country code or name) under visa: in applicant.yaml; remote roles are answered as home"),
        ("applicant visa.by_country[visa.home] is present", have_home,
         "add the home country's block under visa.by_country in applicant.yaml (right_to_work, sponsorship_now, sponsorship_future, summary)"),
        ("applicant visa.by_country.default is present", True if have_default else WARN,
         "add visa.by_country.default (the answers for any place without its own block, summary may use {country}); without it a "
         "job anywhere else has no visa answers"),
    ]


def eo_checks(a: dict) -> list[tuple[str, object, str]]:
    """The structure of applicant.yaml `equal_opportunities` (apply/eo.py), when there is one: a mapping of questions, each with a
    non-empty `match_any` list of option texts, an optional `answer` text, an optional `question` list of regexes (ethnicity needs none),
    and no other keys. One failing line lists every problem. No block, no check: the assist then leaves every such question blank."""
    from .apply import eo

    if "equal_opportunities" not in a:
        return []
    found = eo.problems(a["equal_opportunities"])
    return [("applicant equal_opportunities is well formed" + (f" ({len(a['equal_opportunities'])} question(s))" if not found else ""),
             not found, "; ".join(found) + " (applicant.yaml)")]


def applicant_checks(path: Path) -> list[tuple[str, bool, str]]:
    """Doctor lines for applicant.yaml (read only): the email is not on a placeholder domain, a UK phone is not written
    "+44 0..." (the trunk 0 is dropped after +44), and `location` is a place, not an address with a postcode (the CV and
    forms must not carry a street address). One line per check, so each failure says what to change."""
    from .config import load_yaml

    if not path.exists():
        return [("applicant.yaml", False, f"not found: {path}")]
    a = load_yaml(path) or {}
    email, phone, loc = (str(a.get(k) or "").strip() for k in ("email", "phone", "location"))
    domain = email.rpartition("@")[2].lower()
    bad_domain = domain in PLACEHOLDER_DOMAINS or domain.endswith(tuple("." + d for d in PLACEHOLDER_DOMAINS))
    return [*visa_checks(a), *eo_checks(a),
        (f"applicant email is not on a placeholder domain ({email or 'empty'})", bool(email) and not bad_domain,
         "applicant.yaml `email` must be your real address, not @example.com/.org/.net"),
        (f"applicant phone has no 0 after +44 ({phone or 'empty'})", not re.match(r"\+44[\s\-().]*0", phone),
         "write the UK number as +44 7xxx xxxxxx (drop the leading 0), in applicant.yaml `phone`"),
        (f"applicant location is not a street address ({loc or 'empty'})", not UK_POSTCODE.search(loc),
         "use the city only (e.g. `Bristol, United Kingdom`) in applicant.yaml `location`; no postcode or street"),
    ]


def _feature_on(mode_of, settings) -> bool:
    """True unless the feature's mode reads as off (a mode that does not parse counts as on: its own check reports it)."""
    try:
        return mode_of(settings) != "off"
    except ValueError:
        return True


def model_checks(settings, ping: bool = True) -> list[tuple[str, bool, str]]:
    """Doctor lines for every model a task can be sent to: extract_keywords, rephrase_bullets, cover_letter,
    judge (and any other task the settings name), on each provider the router may try for it. Each model answers a tiny
    request (a few tokens, counted against that model's own daily budget), once however many tasks it serves; a failure is
    reported per task with its kind (model_not_found, auth, timeout, unreachable, rate_limited ...) and what to do. A task
    whose feature is off (letter.mode, review.judge) asks nothing. `ping=False` (doctor --no-ping) sends nothing and says so."""
    from .db import DB
    from .llm import check
    from .llm.router import Router
    from .review.judge import judge_mode
    from .tailoring.tailor import letter_mode

    off = {t: f"{feature} is off" for t, feature, on in
           (("cover_letter", "letter.mode", _feature_on(letter_mode, settings)), ("judge", "review.judge", _feature_on(judge_mode, settings)))
           if not on}
    try:
        router = Router(settings, DB(settings.path("db")) if ping else None)
    except ValueError as e:
        return [("llm.models", False, str(e))]
    out = [(m, False, "name a provider listed in llm.providers") for m in router.unknown_models]
    tasks = check.tasks_to_check(router)
    live = [t for t in tasks if t not in off]
    for task in [t for t in live if not router.serving(t)]:         # restricted to a provider that is not configured
        out.append((f"{task}: a provider can serve it", False,
                    f"llm.task_providers.{task} names {', '.join(router.task_providers.get(task) or [])}, "
                    f"but llm.providers has only {', '.join(p.name for p in router.providers)}"))
    live = [t for t in live if router.serving(t)]
    if not ping:
        out += [(f"{p.name}/{model} for {task}: not asked (--no-ping)", True, "") for task in live
                for p, model in router.serving(task)]
    else:
        for r in check.check_tasks(router, live):
            name = f"{r['provider']}/{r['configured_model']} answers for {r['task']}"
            if r["ok"]:
                out.append((f"{name} ({r['latency_ms']} ms, served by {r['model'] or '?'})", True, ""))
            else:
                kind = r["kind"] or "no usable answer"
                out.append((name, False, f"{kind}: {r['error'] or 'no usable answer'}. "
                                         + check.ADVICE.get(r["kind"], "check the model id and the key")))
    out += [(f"{task}: no model asked ({why})", True, "") for task, why in off.items()]
    return out


def judge_checks(settings) -> list[tuple[str, bool, str]]:
    """Doctor lines for review.judge: a valid mode, and a judge model of another family than the generator (no model call)."""
    from .review import judge

    try:
        mode = judge.judge_mode(settings)
    except ValueError as e:
        return [("review.judge", False, str(e))]
    if mode == "off":
        return [("review.judge: off (no judge)", True, "")]
    problems = judge.check_config(settings)
    if problems:
        return [(f"review.judge: {mode}", False, "; ".join(problems))]
    return [(f"review.judge: {mode}, judge model {judge.judge_model(settings)['model']} "
             f"(family {judge.model_family(judge.judge_model(settings)['model'])})", True, "")]


MSC_RESULTS_DAY = date(2026, 11, 24)        # the day the MSc results come out: "Distinction (Expected)" must be replaced by the real grade
GMAIL_TOKEN_WARN_DAYS = 6                   # a Gmail grant made while the OAuth app is in Testing lapses after about 7 days


def reminder_checks(settings, today: date | None = None, now: datetime | None = None) -> list[tuple[str, object, str]]:
    """Warnings only (they never fail the doctor), and only when one applies: (1) from 2026-11-24 on, the fact bank's MSc entry whose
    `note` still says "Expected"; (2) the Gmail sign-in (paths.secrets/gmail_token.json) more than 6 days old. Read only."""
    from .config import load_yaml
    from .discovery import gmail

    out: list[tuple[str, object, str]] = []
    today = today or date.today()
    path = settings.path("fact_bank")
    if today >= MSC_RESULTS_DAY and path.exists():
        for ed in (load_yaml(path) or {}).get("education") or []:
            label = " ".join(str(ed.get(k) or "") for k in ("degree", "title", "name"))
            if re.search(r"\bmsc\b", label, re.I) and re.search(r"\bexpected\b", str(ed.get("note") or ""), re.I):
                out.append((f"fact bank: the MSc note still says \"{ed['note']}\" ({today:%d %b %Y}; the results were due "
                            f"{MSC_RESULTS_DAY:%d %b %Y})", WARN,
                            "replace it with your actual grade in config/profile/fact_bank.yaml (you edit that file; the pipeline never does)"))
    age = gmail.token_age_days(settings, now)
    if age is not None and age > GMAIL_TOKEN_WARN_DAYS:
        out.append((f"Gmail sign-in is {age:.0f} days old (it lapses after about 7 while the OAuth app is in Testing)", WARN,
                    "run `careerops discover --gmail-auth` again before the next `discover --gmail-alerts`"))
    return out


def provider_checks(settings) -> list[tuple[str, object, str]]:
    """The v2.6 employer boards (discovery/more_ats.py). Offline; warnings only: the settings these providers read, the sources.yaml entries not there yet (the lines
    to paste are printed; this tool never edits that file) and any enabled VC board that this tool refuses by rule (robots.txt, a session cookie)."""
    from .discovery import more_ats, registry

    out: list[tuple[str, object, str]] = []
    bad = more_ats.config_problems(settings)
    out.append((f"employer-board settings ({len(bad)} problem(s))" if bad else "employer-board settings", True if not bad else WARN, "\n".join(bad)))
    try:
        reg = registry.load(settings)
    except registry.RegistryError:
        return out
    lines = more_ats.missing_source_lines(reg)
    if lines:
        out.append((f"{len(lines)} v2.6 employer-board source(s) are not in sources.yaml", WARN, "paste into the `sources:` list (the tool never edits sources.yaml):\n" + "\n".join(lines)))
    live = [s.id for s in reg.vc() if s.method == "consider" or "getro" in s.method]
    if live:
        out.append((f"VC board(s) enabled that this tool refuses by rule: {', '.join(live)}", WARN,
                    "Consider's search needs a session cookie and CSRF token and Getro's API disallows scripts in robots.txt (checked 3 Nov 2026): set enabled: false, "
                    "or check again with `careerops sources check`"))
    return out


def careers_checks(settings) -> list[tuple[str, object, str]]:
    """v3.0, offline: the addresses in careers_urls.yaml and any a reader does not understand, the public board list (licence and switch) and the Reed key."""
    from .discovery import careers, dataset

    out: list[tuple[str, object, str]] = []
    entries, _ = careers.load(settings)
    bad = careers.problems(settings)
    out.append((f"careers_urls.yaml: {len(entries)} address(es)" + (f", {len(bad)} problem(s)" if bad else ""), True if not bad else WARN,
                "\n".join(bad) if bad else f"companies whose board is read from the address you gave, not guessed ({careers.path(settings)}); `careerops search-plan` lists the ones without"))
    cfg = settings["discover"].get("dataset")
    on = not (isinstance(cfg, dict) and cfg.get("enabled") is False)
    out.append((f"public board list {'on' if on else 'off'} (discover.dataset.enabled)", True,
                f"{dataset.SOURCE}: personal, non-commercial use, attribution kept here; off with discover.dataset.enabled: false"))
    have = bool(os.environ.get("REED_API_KEY", "").strip())
    out.append((f"Reed API key (REED_API_KEY) {'present' if have else 'absent'}", True if have else WARN,
                "a free key from reed.co.uk/developers goes in .env as REED_API_KEY=...; then enable the `reed` source in sources.yaml and run `careerops discover --reed`"))
    return out


def region_size_checks(settings) -> list[tuple[str, object, str]]:
    """Warnings only: the Companies House key (present or absent), the sources in sources.yaml with no `regions:` tag (the lines to add are printed; this tool
    never edits that file) and the share of the inbox whose company size is not known yet (the overrides, the 90-day cache and the ad text only: no network)."""
    from .discovery import region, registry, size

    out: list[tuple[str, object, str]] = []
    have = bool(os.environ.get(size.KEY_ENV, "").strip())
    out.append((f"Companies House key ({size.KEY_ENV}) {'present' if have else 'absent'}", True if have else WARN,
                f"add {size.KEY_ENV}=... to .env (free key from developer.company-information.service.gov.uk); without it company sizes come only from "
                "data/company_size_overrides.yaml and the ad text, and the rest stay Unknown"))
    try:
        reg = registry.load(settings)
        lines = region.untagged_lines(reg)
    except registry.RegistryError:
        lines = []
    if lines:
        out.append((f"{len(lines)} source(s) in sources.yaml have no `regions:` tag (treated as [any])", WARN,
                    "add to the entries (the tool never edits sources.yaml):\n" + "\n".join(lines)))
    ads = size.inbox_companies(settings)
    if ads:
        r = size.Resolver(settings, key="", write_cache=False)
        unknown = sum(1 for company, text in ads if r.resolve(company, text).size == size.UNKNOWN)
        share = unknown / len(ads)
        out.append((f"company size unknown for {unknown} of {len(ads)} queued ad(s) ({share:.0%})", WARN if share > 0.5 else True,
                    "a Companies House key and `careerops shortlist` (or `discover --report`) fill them in; or list companies in data/company_size_overrides.yaml"))
    return out


def doctor(settings, ping: bool = True) -> list[tuple[str, bool, str]]:
    """(check, passed, hint) for each dependency the pipeline needs. `ping` = also send each llm.models model a tiny
    request (the only check that spends provider quota)."""
    checks: list[tuple[str, bool, str]] = []
    for tool, hint in (("xelatex", "sudo apt install texlive-xetex"),
                       ("pdftotext", "sudo apt install poppler-utils"),
                       ("pdfinfo", "sudo apt install poppler-utils")):
        checks.append((tool, shutil.which(tool) is not None, hint))
    fonts = subprocess.run(["fc-list"], capture_output=True, text=True).stdout if shutil.which("fc-list") else ""
    checks.append(("Carlito font", "Carlito" in fonts, "sudo apt install fonts-crosextra-carlito"))
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
        checks.append(("playwright chromium", exe.exists(), "playwright install chromium"))
    except Exception:  # noqa: BLE001
        checks.append(("playwright chromium", False, "pip install playwright && playwright install chromium"))
    for prov in settings["llm"]["providers"]:
        env = prov.get("api_key_env")
        if env:
            checks.append((f"{prov['name']} key ({env})", bool(os.environ.get(env)), f"add {env}=... to .env"))
        else:
            checks += ollama_checks(prov)
    checks += model_checks(settings, ping)
    checks += judge_checks(settings)
    from . import judge as claude_judge
    checks.append(claude_judge.Session(settings).doctor_line())      # v3.5: claude found / logged in / mode / model (no model call)
    checks += core_skill_checks(settings.path("fact_bank"))
    checks += hook_lint_checks(settings.path("fact_bank"))
    checks += hook_claim_checks(settings.path("fact_bank"))
    checks += skill_repeat_checks(settings.path("fact_bank"))
    checks += profile_word_checks(settings.path("fact_bank"))
    checks += plain_allow_checks(settings)
    from .tailoring import sections as sections_mod
    checks.append(sections_mod.doctor_line(settings))
    checks += project_public_checks(settings.path("fact_bank"))
    checks += ats_structure_checks(settings)
    checks += applicant_checks(settings.path("applicant"))
    checks += reminder_checks(settings)
    checks += region_size_checks(settings)
    checks += provider_checks(settings)
    checks += careers_checks(settings)
    todos = fact_bank_open_items(settings.path("fact_bank"))
    checks.append((f"fact bank open items ({todos})", todos == 0,
                   "answer every `# CONFIRM` and fill or delete every TODO in fact_bank.yaml"))
    return checks
