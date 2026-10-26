"""Per-user configuration, loaded from config.toml (gitignored)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    inbox_dir: Path
    jobs_dir: Path
    routes_dir: Path
    poll_seconds: int = 30
    settle_seconds: int = 10
    classifier_model: str | None = None
    route_model: str | None = None
    confidence_threshold: float = 0.7
    max_turns: int = 30
    teams_webhook_url: str | None = None


def _expand(value: str) -> Path:
    """Expand ~ and environment variables (%USERPROFILE%, $HOME, ...)."""
    return Path(os.path.expandvars(value)).expanduser()


def load_config(path: Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: copy config.example.toml to config.toml and adjust it")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    def as_path(key: str) -> Path:
        if not data.get(key):
            raise ValueError(f"config: '{key}' is required")
        return (base / _expand(data[key])).resolve()

    return Config(
        inbox_dir=as_path("inbox_dir"),
        jobs_dir=as_path("jobs_dir"),
        routes_dir=as_path("routes_dir"),
        poll_seconds=int(data.get("poll_seconds", 30)),
        settle_seconds=int(data.get("settle_seconds", 10)),
        classifier_model=data.get("classifier_model") or None,
        route_model=data.get("route_model") or None,
        confidence_threshold=float(data.get("confidence_threshold", 0.7)),
        max_turns=int(data.get("max_turns", 30)),
        teams_webhook_url=os.environ.get("TEAMS_WEBHOOK_URL") or data.get("teams_webhook_url") or None,
    )
