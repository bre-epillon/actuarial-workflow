"""Runs an Excel-based tool: fill input cells, let Excel calculate, read the results back.

The calculation stays in the tool (CLAUDE.md rule 1): the pipeline only writes the
inputs a route extracted and reads the outputs. Uses Microsoft Excel through
PowerShell COM automation (excel_bridge.ps1), so it needs Windows with Excel but
no extra Python packages. The template is opened read-only; the filled copy is
saved to `output`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

BRIDGE = Path(__file__).with_name("excel_bridge.ps1")
EXCEL_EPOCH = date(1899, 12, 30)


class ExcelToolError(RuntimeError):
    """Excel could not run the tool (not installed, crashed, timed out, bad workbook)."""


@dataclass
class ExcelJob:
    template: Path
    output: Path | None
    writes: list[dict] = field(default_factory=list)  # {"sheet", "cell", "value"}
    tables: list[dict] = field(default_factory=list)  # {"sheet", "cell" (top-left), "rows": [[...]]}
    reads: list[dict] = field(default_factory=list)  # {"sheet", "cell"}
    read_ranges: list[dict] = field(default_factory=list)  # {"sheet", "range"}
    full_calculation: bool = False
    activate: str | None = None  # sheet shown when the saved copy is opened

    def write(self, sheet: str, cell: str, value) -> None:
        self.writes.append({"sheet": sheet, "cell": cell, "value": value})

    def read(self, sheet: str, cell: str) -> None:
        self.reads.append({"sheet": sheet, "cell": cell})


def excel_date(value: str | date) -> float:
    """ISO date -> Excel serial day number (what a date cell holds)."""
    d = datetime.strptime(value, "%Y-%m-%d").date() if isinstance(value, str) else value
    return float((d - EXCEL_EPOCH).days)


def as_text(value: str) -> str:
    """Keep text as text: Excel would turn '00123' or '1/2' into a number or date."""
    if value[:1] in "=+-@" or _looks_numeric(value):
        return "'" + value
    return value


def _looks_numeric(value: str) -> bool:
    s = value.strip().replace(",", "").replace("/", "").replace("-", "").replace(".", "").replace(" ", "")
    return bool(s) and s.isdigit()


def available() -> str | None:
    """None if Excel automation can run here, else why not."""
    if sys.platform != "win32":
        return "Excel tools need Windows with Microsoft Excel"
    if not shutil.which("powershell"):
        return "powershell.exe not found"
    return None


def run(job: ExcelJob, work_dir: Path, timeout: int = 1800) -> dict:
    """Run the job; returns {"reads": {"Sheet!A1": {"value", "text"}}, "ranges": {...}, ...}."""
    if why := available():
        raise ExcelToolError(why)
    work_dir.mkdir(parents=True, exist_ok=True)
    job_file, result_file, pid_file = (work_dir / n for n in ("excel_job.json", "excel_result.json", "excel_pid.txt"))
    for f in (result_file, pid_file):
        f.unlink(missing_ok=True)
    payload = {
        "template": str(job.template.resolve()),
        "output": str(job.output.resolve()) if job.output else None,
        "writes": job.writes, "tables": job.tables, "reads": job.reads, "read_ranges": job.read_ranges,
        "full_calculation": job.full_calculation, "activate": job.activate,
    }
    job_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(BRIDGE), str(job_file), str(result_file), str(pid_file)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        _kill_excel(pid_file)
        raise ExcelToolError(f"Excel did not finish within {timeout} s") from None
    if not result_file.exists():
        _kill_excel(pid_file)
        raise ExcelToolError(f"Excel bridge failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise ExcelToolError(f"Excel: {result.get('error')}")
    return result


def _kill_excel(pid_file: Path) -> None:
    """Stop the Excel instance this run started (never the user's own Excel)."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    if os.name != "nt":  # pragma: no cover
        return
