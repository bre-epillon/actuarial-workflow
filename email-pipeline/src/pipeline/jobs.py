"""Job directories, manifests, hashing and dedup.

A job directory looks like:
    <jobs_dir>/<YYYY-MM-DD>_<hash8>/
        original/      untouched copy of the inbox item
        extracted/     preprocessed text, attachments, summary.json
        output/<route>/ agent outputs
        logs/          agent transcripts (one .jsonl per agent run)
        manifest.json  state machine + audit trail
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_STATES = {"new", "preprocessed", "classified", "running"}
TERMINAL_STATES = {"done", "failed", "needs_review"}

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(name: str, max_len: int = 80) -> str:
    """Windows-safe file name (Outlook subjects often contain : ? / etc.).

    Truncation keeps a short extension, so 'very long name.xlsx' stays an .xlsx.
    """
    name = re.sub(r'[<>:"/\\|?*%\x00-\x1f]', "_", name).strip(" .")  # '%': see guard.path_problem
    name = name or "item"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and len(ext) <= 8 and " " not in ext:
            name = stem[: max_len - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            name = name[:max_len].rstrip(" .")
    return name


def unique_path(directory: Path, name: str) -> Path:
    """directory/name, or directory/name_2.ext, name_3.ext, ... if taken."""
    target = directory / name
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = directory / f"{stem}_{n}{suffix}"
        n += 1
    return target


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
        # Fully resolved: a Windows 8.3 short name (C:\Users\ERIKPI~1\...) in the agent's cwd
        # makes Claude Code treat files under it as outside the working directory.
        self.dir = Path(job_dir).resolve()
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

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    def output_dir(self, route: str) -> Path:
        return self.dir / "output" / route

    def save(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, self.manifest_path)  # atomic on Windows and POSIX
                return
            except PermissionError:  # Windows: another process (e.g. `status`) is reading it
                if attempt == 9:
                    raise
                time.sleep(0.2)

    def set_state(self, state: str, **details) -> None:
        self.data["state"] = state
        self.data.setdefault("history", []).append({"state": state, "at": now_iso(), **details})
        self.save()


def create_job(jobs_dir: Path, source: Path, digest: str) -> Job:
    job_id = f"{datetime.now().strftime('%Y-%m-%d')}_{digest[:8]}"
    job_dir = jobs_dir / job_id
    if job_dir.exists() and not (job_dir / "manifest.json").exists():
        shutil.rmtree(job_dir)  # left over from a copy that failed earlier
    original = job_dir / "original"
    original.mkdir(parents=True, exist_ok=False)
    target = original / safe_name(source.name, 120)
    try:
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
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
        except (json.JSONDecodeError, KeyError, OSError):
            continue  # half-written job; leave for manual inspection
    return jobs


def find_job(jobs_dir: Path, ref: str) -> Job:
    """A job by full id or unique prefix/suffix (e.g. the 8-char hash)."""
    matches = [j for j in load_jobs(jobs_dir) if j.id == ref or j.id.startswith(ref) or j.id.endswith(ref)]
    exact = [j for j in matches if j.id == ref]
    if exact:
        return exact[0]
    if len(matches) != 1:
        raise LookupError(f"{'no' if not matches else 'more than one'} job matches '{ref}'")
    return matches[0]
