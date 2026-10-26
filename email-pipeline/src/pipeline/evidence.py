"""Deterministic check that what an agent quotes is really in the file it cites."""
from __future__ import annotations

from pathlib import Path

from .guard import path_problem


def _normalize(text: str) -> str:
    text = text.replace("\xa0", " ").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).casefold()


def quote_problem(job_dir: Path, source: str, quote: str) -> str | None:
    """None if `quote` occurs (whitespace- and case-insensitively) in job_dir/source."""
    job_dir = job_dir.resolve()
    if problem := path_problem(source, job_dir, job_dir):
        return f"source {source!r}: {problem}"
    path = (job_dir / source).resolve()
    if not path.is_file():
        return f"source {source!r} does not exist"
    if _normalize(quote) not in _normalize(path.read_text(encoding="utf-8", errors="replace")):
        return f"{quote!r} not found in {source}"
    return None
