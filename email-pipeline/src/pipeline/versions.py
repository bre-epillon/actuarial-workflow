"""Versions for the audit trail (CLAUDE.md rule 6).

Route postprocess code reports the pinned external tools it used with
package_version("<distribution>"); the result lands in the job manifest.
"""
from __future__ import annotations

import json
import subprocess
from functools import cache
from importlib import metadata
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]


def package_version(dist: str) -> str:
    """Installed version of a distribution, plus the git commit when installed from git."""
    try:
        d = metadata.distribution(dist)
    except metadata.PackageNotFoundError:
        return "not installed"
    version = d.version
    direct = d.read_text("direct_url.json")
    if direct:
        vcs = json.loads(direct).get("vcs_info") or {}
        if vcs.get("commit_id"):
            version += f" (git {vcs['commit_id'][:12]})"
    return version


@cache
def pipeline_version() -> str:
    """email-pipeline version and the repo commit it runs from (if it is a git checkout)."""
    version = package_version("email-pipeline")
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPO_DIR), "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        return f"{version} ({commit})"
    except (OSError, subprocess.SubprocessError):
        return version
