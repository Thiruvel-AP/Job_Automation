"""Names for the archive: folder keys, NTFS-safe components, recruiter-visible file names. Pure functions."""
from __future__ import annotations

import hashlib
import re

# The archive lives on D: (NTFS), reached from WSL, so every component must be legal there.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Trailing legal suffixes only. NOT "co": folder_key keeps it so `Zone & Co` == `Zone and Co`, `Co-op` == `COOP`.
_SUFFIX = re.compile(r"[\s,]+(ltd|limited|plc|llp|lp|inc|incorporated|llc|corp|corporation|gmbh|sa|bv|ag)\.?$")
FILE_NAME_MAX = 80
STYLES = ("full", "short")


def safe_name(s: str, limit: int = 80) -> str:
    """One legal NTFS path component: illegal chars and control chars -> '-', whitespace collapsed, no trailing
    dots/spaces, reserved device names get a '_', capped at `limit` characters."""
    s = re.sub(r"\s+", " ", _ILLEGAL.sub("-", str(s))).strip()
    s = s[:limit].rstrip(" .")
    stem, dot, rest = s.partition(".")
    if stem.upper() in _RESERVED:
        s = f"{stem}_{dot}{rest}"[:limit].rstrip(" .")
    return s or "untitled"


def folder_key(name: str) -> str:
    """Key for matching company folders: lower-case, `&` -> ` and `, drop `(...)`, strip trailing legal suffixes,
    keep [a-z0-9]. Deliberately not discovery.register.normalise() (that one strips `co` anywhere)."""
    s = re.sub(r"\([^)]*\)", " ", name.lower().replace("&", " and ")).strip()
    while True:
        t = _SUFFIX.sub("", s).strip()
        if t == s or not t:
            break
        s = t
    return re.sub(r"[^a-z0-9]", "", s)


def id6(job_id: str) -> str:
    """First 6 hex chars of sha1(job_id). Job ids contain colons (greenhouse:acme:7), which NTFS forbids."""
    return hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:6]


def file_stem(identity_name: str, kind: str, style: str = "full") -> str:
    """'<name> CV' / '<name> Cover Letter'. No company, role or date: the folder carries those.
    The whole file name with a 3-letter extension stays within 80 characters."""
    if style not in STYLES:
        raise ValueError(f"files.name_style must be one of {STYLES}, got {style!r}")
    parts = identity_name.split()
    name = parts[0] if style == "short" and parts else identity_name
    room = FILE_NAME_MAX - 4 - len(kind) - 1
    return safe_name(f"{safe_name(name, room)} {kind}", FILE_NAME_MAX - 4)


# ---- v3.9.6: "<Name> CV - <Role> - <Company>.pdf" for the files a recruiter sees (the folder layout and the .tex stems are unchanged)
OUT_NAME_MAX = 150                                  # the whole file name, extension included
_OUT_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def load_file_naming(path=None) -> dict[str, str]:
    """{role family: label} from `config/file_naming.yaml` (code-owned): `roles:` plus `default:` under the key `_default`. {} + default AI Engineer when absent."""
    import yaml

    from ..config import ROOT

    p = path or ROOT / "config" / "file_naming.yaml"
    try:
        raw = yaml.safe_load(open(p, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    out = {str(k): str(v) for k, v in (raw.get("roles") or {}).items()}
    out["_default"] = str(raw.get("default") or "AI Engineer")
    return out


def role_label(family: str | None, warn=print, path=None) -> str:
    """The role family's file-name label; a family without an entry gets the default (AI Engineer) and a printed warning."""
    cfg = load_file_naming(path)
    if family and family in cfg and family != "_default":
        return cfg[family]
    if family:
        warn(f"warning: role family '{family}' has no entry in config/file_naming.yaml; its file names say '{cfg['_default']}'")
    return cfg["_default"]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _OUT_ILLEGAL.sub(" ", str(s or ""))).strip().rstrip(" .")


def output_stem(name: str, kind: str, role: str, company: str, limit: int = OUT_NAME_MAX) -> str:
    """'<Name> <kind> - <Role> - <Company>' (kind = CV | CoverLetter), Windows- and WSL-safe, with ".pdf" still inside `limit` characters.
    The company is shortened first, then the role."""
    head, role_s, comp = _clean(f"{name} {kind}"), _clean(role), _clean(company)
    room = limit - 4 - len(head) - len(" - ") * 2
    comp_room = max(1, room - len(role_s))
    if len(comp) > comp_room:
        comp = comp[:comp_room].rstrip(" .")
    if len(role_s) + len(comp) > room:
        role_s = role_s[:max(1, room - len(comp))].rstrip(" .")
    stem = " - ".join(x for x in (head, role_s, comp) if x)
    return safe_name(stem, limit - 4)
