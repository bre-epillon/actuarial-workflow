"""Job directories, manifests, hashing and dedup.

A job directory looks like:
    <jobs_dir>/<YYYY-MM-DD>_<hash8>/
        original/      untouched copy of the inbox item
        extracted/     preprocessed text, attachments, summary.json
        output/<route>/ agent outputs
        manifest.json  state machine + audit trail
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_STATES = {"new", "preprocessed", "classified", "running"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(name: str, max_len: int = 80) -> str:
    """Windows-safe file name (Outlook subjects often contain : ? / etc.)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "item")[:max_len]


def content_hash(path: Path) -> str:
    """SHA-256 of a file's bytes, or of a folder's relative paths + bytes."""
    h = hashlib.sha256()
    if path.is_file():
        files, root = [path], None
    else:
        files, root = sorted(p for p in path.rglob("*") if p.is_file()), path
    for f in files:
        if root is not None:
            h.update(f.relative_to(root).as_posix().encode("utf-8"))
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


class Job:
    def __init__(self, job_dir: Path):
        self.dir = Path(job_dir)
        self.manifest_path = self.dir / "manifest.json"
        self.data: dict = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def state(self) -> str:
        return self.data["state"]

    @property
    def original_dir(self) -> Path:
        return self.dir / "original"

    @property
    def extracted_dir(self) -> Path:
        return self.dir / "extracted"

    def output_dir(self, route: str) -> Path:
        return self.dir / "output" / route

    def save(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.manifest_path)  # atomic on Windows and POSIX

    def set_state(self, state: str, **details) -> None:
        self.data["state"] = state
        self.data.setdefault("history", []).append({"state": state, "at": now_iso(), **details})
        self.save()


def create_job(jobs_dir: Path, source: Path, digest: str) -> Job:
    job_id = f"{datetime.now().strftime('%Y-%m-%d')}_{digest[:8]}"
    job_dir = jobs_dir / job_id
    original = job_dir / "original"
    original.mkdir(parents=True, exist_ok=False)
    target = original / safe_name(source.name, 120)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    manifest = {
        "id": job_id,
        "source_name": source.name,
        "sha256": digest,
        "created_at": now_iso(),
        "state": "new",
        "history": [{"state": "new", "at": now_iso()}],
        "classification": None,
        "route_runs": {},
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return Job(job_dir)


def load_jobs(jobs_dir: Path) -> list[Job]:
    jobs = []
    for m in sorted(jobs_dir.glob("*/manifest.json")):
        try:
            jobs.append(Job(m.parent))
        except (json.JSONDecodeError, KeyError):
            continue  # half-written job; leave for manual inspection
    return jobs
