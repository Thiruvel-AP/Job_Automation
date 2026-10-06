"""Clean the frozen calibration keyword lists (config/calibration/keywords/*.yaml).
Dry run by default: prints what would change. Add --apply to write. Commit before and after.
Removes non-skills (phrases, perks, generic adjectives); restores JD skills the 25-term cap cut.
"""
from __future__ import annotations
import sys, yaml
from pathlib import Path

DIR = Path("config/calibration/keywords")
EDITS = {
    "cal-chattermill": {"remove": ["real-world text data", "custom models", "search", "metrics"],
                        "add": ["llm", "machine learning", "evaluation", "data pipelines"]},
    "cal-creditspring": {"remove": ["SME", "acquisition", "alt-datasources", "checking data", "commercial awareness",
                                    "infrastructure", "external API feeds", "machine learning models",
                                    "credit reference agencies"],
                         "add": ["scikit-learn", "sql", "stakeholders", "statistical inference", "production",
                                 "testing", "aws", "fintech"]},
    "cal-lendable": {"remove": [], "add": []},
    "cal-zoneco": {"remove": ["problem-solving", "end-to-end", "scalable"], "add": []},
    "cal-heathrow": {"remove": ["airport operations", "complex organisation", "specialist suppliers", "expected benefits",
                                "measurable outcomes", "measures of success", "business decisions",
                                "data science solutions", "data science methods"], "add": []},
    "cal-quantcast": {"remove": ["scalable", "research"], "add": []},
    "cal-trainline": {"remove": ["healthcare", "insurance", "structure analytical work"], "add": []},
}

def find_list(doc):
    """Return (container, key) holding the list of keyword dicts, whatever the file's top-level shape."""
    if isinstance(doc, list):
        return None, None
    for k, v in doc.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "term" in v[0]:
            return doc, k
    raise SystemExit("no list of {term: ...} entries found")

apply = "--apply" in sys.argv
for jd, ed in EDITS.items():
    p = DIR / f"{jd}.yaml"
    if not p.exists():
        print(f"!! missing {p}"); continue
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    cont, key = find_list(doc)
    kws = doc if cont is None else cont[key]
    have = {k["term"].lower() for k in kws}
    rm = {t.lower() for t in ed["remove"]}
    missing_rm = sorted(rm - have)
    kept = [k for k in kws if k["term"].lower() not in rm]
    template = kws[0]
    added = []
    for t in ed["add"]:
        if t.lower() in {k["term"].lower() for k in kept}:
            continue
        new = {"term": t, "weight": 2.0, "required": True}
        if "alts" in template:
            new["alts"] = []
        kept.append(new); added.append(t)
    print(f"{jd}: {len(kws)} -> {len(kept)} | removed {sorted(rm & have)} | added {added}"
          + (f" | not found (skipped): {missing_rm}" if missing_rm else ""))
    if len(kept) > 25:
        print(f"!! {jd} would have {len(kept)} > 25 keywords; not writing"); continue
    if apply:
        if cont is None: doc = kept
        else: cont[key] = kept
        p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
print("written" if apply else "dry run only: re-run with --apply to write")
