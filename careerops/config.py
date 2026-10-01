"""Settings + fact bank loading. Paths in settings.yaml are relative to the repo root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("CAREEROPS_ROOT", Path(__file__).resolve().parent.parent))


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.root / p

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else ROOT / "config" / "settings.yaml"
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings(raw=raw, root=ROOT)


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
