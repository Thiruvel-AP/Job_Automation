"""`careerops init` and `careerops doctor`: create the data layout and check the toolchain."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import requests

# An open item in the fact bank: a TODO, or a `# CONFIRM` marker. The header's prose about the marker is quoted
# ("# CONFIRM"), so a marker directly followed by a quote is not an item.
OPEN_ITEM = re.compile(r'TODO|#\s*CONFIRM\b(?!")')

# `applications` is the user's archive root: init creates it only if missing and never touches existing contents.
DATA_DIRS = ("register_dir", "companies_house_dir", "outputs", "browser_profile", "logs", "applications", "tmp",
             "calibration")
CALIBRATION_SUBDIRS = ("jds", "reference")      # inside paths.calibration; init adds them, never touches what is in them
FILE_PARENTS = ("db", "excel", "learning", "preferences")


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
    env, example = settings.root / ".env", settings.root / ".env.example"
    if not env.exists() and example.exists():
        shutil.copy(example, env)
        made.append(".env (from .env.example — add your keys)")
    return made


def fact_bank_open_items(path: Path) -> int:
    """How many TODO / `# CONFIRM` markers the fact bank still holds; -1 if the file is missing."""
    return len(OPEN_ITEM.findall(path.read_text(encoding="utf-8"))) if path.exists() else -1


def doctor(settings) -> list[tuple[str, bool, str]]:
    """(check, passed, hint) for each dependency the pipeline needs."""
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
            try:
                up = requests.get(prov["base_url"].replace("/v1", "") + "/api/tags", timeout=2).ok
            except requests.RequestException:
                up = False
            checks.append((f"{prov['name']} at {prov['base_url']}", up, f"start it, then: ollama pull {prov['model']}"))
    todos = fact_bank_open_items(settings.path("fact_bank"))
    checks.append((f"fact bank open items ({todos})", todos == 0,
                   "answer every `# CONFIRM` and fill or delete every TODO in fact_bank.yaml"))
    return checks
