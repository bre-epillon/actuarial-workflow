"""Deterministic preprocessing: no LLM here.

Turns whatever is in job/original into clean inputs in job/extracted:
    extracted/<email_stem>/body.txt
    extracted/<email_stem>/attachments/<files>
    extracted/files/<loose files>
    extracted/**/<excel>__<sheet>.csv   (one CSV per Excel sheet)
    extracted/summary.json              (index of everything above)
"""
from __future__ import annotations

import email
import json
import logging
import shutil
from email import policy
from pathlib import Path

from .jobs import Job, safe_name

log = logging.getLogger(__name__)

EXCEL_SUFFIXES = {".xlsx", ".xlsm"}


def preprocess(job: Job) -> dict:
    out = job.extracted_dir
    out.mkdir(exist_ok=True)
    summary: dict = {"emails": [], "files": [], "warnings": []}

    for f in sorted(p for p in job.original_dir.rglob("*") if p.is_file()):
        suffix = f.suffix.lower()
        try:
            if suffix == ".msg":
                summary["emails"].append(_from_msg(f, out, summary))
            elif suffix == ".eml":
                summary["emails"].append(_from_eml(f, out, summary))
            else:
                dest = out / "files"
                dest.mkdir(exist_ok=True)
                summary["files"].extend(_store_file(f.name, f.read_bytes(), dest, summary))
        except Exception as exc:  # keep going; record what failed
            log.exception("preprocess failed for %s", f)
            summary["warnings"].append(f"could not process {f.name}: {exc}")

    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _write_body(dest: Path, headers: dict, body: str) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    head = "\n".join(f"{k}: {v}" for k, v in headers.items())
    path = dest / "body.txt"
    path.write_text(f"{head}\n\n{body or ''}", encoding="utf-8")
    return path


def _from_msg(path: Path, out: Path, summary: dict) -> dict:
    import extract_msg  # imported lazily so tests don't need it

    dest = out / safe_name(path.stem)
    msg = extract_msg.openMsg(str(path))
    try:
        headers = {"From": msg.sender, "To": msg.to, "Cc": msg.cc, "Date": msg.date, "Subject": msg.subject}
        body_path = _write_body(dest, headers, msg.body)
        attachments: list[str] = []
        for att in msg.attachments:
            name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None) or "attachment"
            data = att.data
            if isinstance(data, (bytes, bytearray)):
                attachments.extend(_store_file(name, bytes(data), dest / "attachments", summary))
            else:
                # Embedded email (a .msg inside the .msg). Not expanded yet.
                summary["warnings"].append(f"embedded item '{name}' in {path.name} not expanded")
        return {
            "source": path.name,
            "subject": msg.subject,
            "from": msg.sender,
            "date": str(msg.date),
            "body": body_path.relative_to(out).as_posix(),
            "attachments": attachments,
        }
    finally:
        close = getattr(msg, "close", None)
        if close:
            close()


def _from_eml(path: Path, out: Path, summary: dict) -> dict:
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    dest = out / safe_name(path.stem)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    headers = {k: msg.get(k, "") for k in ("From", "To", "Cc", "Date", "Subject")}
    body_path = _write_body(dest, headers, body)
    attachments: list[str] = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if payload:
            attachments.extend(_store_file(part.get_filename() or "attachment", payload, dest / "attachments", summary))
    return {
        "source": path.name,
        "subject": headers["Subject"],
        "from": headers["From"],
        "date": headers["Date"],
        "body": body_path.relative_to(out).as_posix(),
        "attachments": attachments,
    }


def _store_file(name: str, data: bytes, dest: Path, summary: dict) -> list[str]:
    """Save a file; for Excel also write one CSV per sheet. Returns the stored file names."""
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / safe_name(name, 120)
    target.write_bytes(data)
    stored = [target.name]
    if target.suffix.lower() in EXCEL_SUFFIXES:
        try:
            stored.extend(_excel_to_csv(target))
        except Exception as exc:
            summary["warnings"].append(f"could not convert {target.name} to CSV: {exc}")
    return stored


def _excel_to_csv(path: Path) -> list[str]:
    import pandas as pd

    names = []
    for sheet, df in pd.read_excel(path, sheet_name=None, header=None).items():
        csv = path.with_name(f"{path.stem}__{safe_name(str(sheet), 40)}.csv")
        df.to_csv(csv, index=False, header=False)
        names.append(csv.name)
    return names
