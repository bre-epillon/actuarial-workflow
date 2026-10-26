import asyncio
from pathlib import Path

import pytest

from pipeline.guard import check, make_guard

TOOLS = {"Read", "Write", "Edit", "Glob", "Grep"}


@pytest.fixture
def dirs(tmp_path):
    job = (tmp_path / "jobs" / "2026-10-26_abcdef12").resolve()
    out = job / "output" / "large_loss"
    out.mkdir(parents=True)
    return job, out


@pytest.mark.parametrize("tool,args", [
    ("Read", {"file_path": "extracted/summary.json"}),
    ("Read", {"file_path": "{job}/original/mail.msg"}),
    ("Glob", {"pattern": "**/*.csv"}),
    ("Glob", {"pattern": "*.txt", "path": "{job}/extracted"}),
    ("Grep", {"pattern": "EUR", "path": "extracted", "glob": "*.txt"}),
    ("Write", {"file_path": "{job}/output/large_loss/extracted.json"}),
    ("Edit", {"file_path": "output/large_loss/summary.md"}),
])
def test_allowed(dirs, tool, args):
    job, out = dirs
    args = {k: v.format(job=job) for k, v in args.items()}
    assert check(tool, args, job, out, TOOLS) is None


@pytest.mark.parametrize("tool,args", [
    ("Read", {"file_path": "{job}/../other_job/manifest.json"}),
    ("Read", {"file_path": "../../secret.txt"}),
    ("Read", {"file_path": "~/.claude/.credentials.json"}),
    ("Read", {"file_path": "%USERPROFILE%/.ssh/id_rsa"}),
    ("Read", {"file_path": "C:/Windows/win.ini"}),
    ("Glob", {"pattern": "../**/*"}),
    ("Glob", {"pattern": "C:/Users/**"}),
    ("Glob", {"pattern": "*", "path": "{job}/.."}),
    ("Grep", {"pattern": "x", "glob": "/etc/*"}),
    ("Write", {"file_path": "{job}/original/mail.msg"}),
    ("Write", {"file_path": "extracted/summary.json"}),
    ("Write", {"file_path": "output/smoke_test/x.md"}),
    ("Write", {"file_path": "manifest.json"}),
    ("Write", {}),
    ("Edit", {"file_path": "output/large_loss/../../manifest.json"}),
    ("Bash", {"command": "dir"}),
    ("WebFetch", {"url": "https://example.com"}),
])
def test_denied(dirs, tool, args):
    job, out = dirs
    args = {k: v.format(job=job) for k, v in args.items()}
    assert check(tool, args, job, out, TOOLS) is not None


def test_network_and_device_paths_are_denied_without_touching_them(dirs, monkeypatch):
    """Resolving \\\\host\\share would make Windows contact the host (DNS, SMB/NTLM) before the deny."""
    job, out = dirs
    other_drive = "Y:" if job.drive.upper() == "Z:" else "Z:"
    calls = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: calls.append(str(self)) or real_resolve(self, *a, **k))
    for value in [r"\\attacker.example\share\x", "//attacker.example/share/x", r"\\host@SSL\DavWWWRoot\x",
                  r"\\?\UNC\host\share\x", r"\\.\pipe\x", r"\\?\GLOBALROOT\Device\HarddiskVolume1\x",
                  f"{other_drive}/data/x", r"extracted\a.txt:hidden_stream"]:
        assert check("Read", {"file_path": value}, job, out, TOOLS) is not None, value
        assert check("Write", {"file_path": value}, job, out, TOOLS) is not None, value
    assert calls == []


def test_guard_fails_closed(dirs, monkeypatch):
    job, out = dirs

    def broken(*args):
        raise OSError("boom")

    monkeypatch.setattr("pipeline.guard.check", broken)
    result = asyncio.run(make_guard(job, out, sorted(TOOLS))({"tool_name": "Read", "tool_input": {}}, "t", {}))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_output_shape(dirs):
    job, out = dirs
    guard = make_guard(job, out, sorted(TOOLS))
    ok = asyncio.run(guard({"tool_name": "Read", "tool_input": {"file_path": "extracted/a.txt"}}, "t1", {"signal": None}))
    assert ok == {}
    denied = asyncio.run(guard({"tool_name": "Write", "tool_input": {"file_path": "original/a"}}, "t2", {"signal": None}))
    spec = denied["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert "output/large_loss" in spec["permissionDecisionReason"]
