"""Deterministic file-access guard for route agents (CLAUDE.md rules 4 and 5).

Runs as a PreToolUse hook on every tool call, before Claude Code's own
permission check: tools must be in the route's list, reads must stay inside
the job folder and writes inside output/<route>/. Claude Code's --restricted
mode also confines file tools to the working directory; this hook is the
pipeline-owned layer and the one that keeps original/ and extracted/ read-only.
It fails closed: if checking a call raises, the call is denied.
Bash/PowerShell cannot be checked this way: routes should not need them.
"""
from __future__ import annotations

import logging
from pathlib import Path, PurePath, PureWindowsPath

log = logging.getLogger(__name__)

# tool -> [(argument, kind)]; kind is "read", "write" or "pattern" (a glob)
PATH_ARGS = {
    "Read": [("file_path", "read")],
    "Glob": [("path", "read"), ("pattern", "pattern")],
    "Grep": [("path", "read"), ("glob", "pattern")],
    "Write": [("file_path", "write")],
    "Edit": [("file_path", "write")],
    "MultiEdit": [("file_path", "write")],
    "NotebookEdit": [("notebook_path", "write")],
}


def path_problem(value: str, job_dir: Path, root: Path) -> str | None:
    """None if `value` (absolute, or relative to job_dir) is inside `root`, else why not.

    Purely lexical checks come first: resolving a UNC path (\\\\host\\share) would make
    Windows contact that host, leaking data through DNS or the user's NTLM hash over
    SMB, before we could deny it. job_dir and root must be resolved.
    """
    if value.startswith("~") or "%" in value:
        return "must be a plain path inside the job folder"
    v = value.replace("/", "\\")
    if v.startswith("\\\\"):
        return "network and device paths are not allowed"
    drive = PureWindowsPath(v).drive
    if drive and drive.lower() != job_dir.drive.lower():
        return "other drives are not allowed"
    if ":" in v[len(drive):]:
        return "alternate data streams are not allowed"
    target = (job_dir / value).resolve()
    if target != root and not target.is_relative_to(root):
        return "outside the allowed folder"
    return None


def check(tool: str, args: dict, job_dir: Path, write_dir: Path, allowed: set[str]) -> str | None:
    """None if the call is fine, else the reason to deny it. Dirs must be resolved."""
    if tool not in allowed:
        return f"tool {tool} is not available in this process"
    for arg, kind in PATH_ARGS.get(tool, []):
        value = str(args.get(arg) or "")
        if not value:
            if kind == "write":
                return f"{tool} needs {arg}"
            continue
        if kind == "pattern":
            p = PurePath(value)
            if p.anchor or ".." in p.parts or value.startswith("~"):
                return f"{arg} must be a relative pattern inside the job folder: {value}"
            continue
        if kind == "read" and (problem := path_problem(value, job_dir, job_dir)):
            return f"reading is only allowed inside the job folder ({problem}): {value}"
        if kind == "write" and (problem := path_problem(value, job_dir, write_dir)):
            return f"writing is only allowed inside ./{write_dir.relative_to(job_dir).as_posix()}/ ({problem}): {value}"
    return None


def make_guard(job_dir: Path, write_dir: Path, allowed: list[str]):
    """A PreToolUse hook callback (claude_agent_sdk.HookCallback) for one route run."""
    job_dir, write_dir, allowed_set = job_dir.resolve(), write_dir.resolve(), set(allowed)

    async def guard(input_data, tool_use_id, context):
        tool = input_data.get("tool_name", "")
        try:
            reason = check(tool, input_data.get("tool_input") or {}, job_dir, write_dir, allowed_set)
        except Exception as exc:  # fail closed: an unanswered hook would let the call run
            reason = f"could not check this call ({type(exc).__name__}); denied"
        if reason is None:
            return {}
        log.warning("denied %s in %s: %s", tool, job_dir.name, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return guard
