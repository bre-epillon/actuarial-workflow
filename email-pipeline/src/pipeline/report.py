"""Status overview of all jobs as one self-contained HTML page: <jobs_dir>/status.html.

Read-only over the job folders; regenerated after every poll and by
`python -m pipeline report`. Jobs of a route that declares `case_keys` are linked
into cases (e.g. the advice, updates and cash call of one loss), ordered by email
date, and their figures are checked against each other across stages.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .jobs import ACTIVE_STATES, Job, load_jobs, now_iso
from .routes import Route

TEMPLATE = Path(__file__).with_name("status_template.html")
REPORT_NAME = "status.html"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _email_date(job: Job) -> str:
    """Date of the (outer) email, ISO; the job's creation time when there is none."""
    summary = _read_json(job.extracted_dir / "summary.json") or {}
    for e in summary.get("emails", []):
        try:
            return parsedate_to_datetime(e.get("date") or "").astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            continue
    return job.data.get("created_at", "")


def verdict(job: Job) -> str:
    """ok | check | failed | review | active: what a person has to do with the job."""
    state, runs = job.state, job.data.get("route_runs", {}).values()
    if state in ACTIVE_STATES:
        return "active"
    if state == "needs_review":
        return "review"
    if state == "failed" or not all(r.get("ok") for r in runs):
        return "failed"
    return "check" if any(r.get("flags") for r in runs) else "ok"


def job_record(job: Job, routes: dict[str, Route]) -> dict:
    d = job.data
    preprocessed = [h for h in d.get("history", []) if h["state"] == "preprocessed"]
    failure = next((h.get("error") or "; ".join(h.get("failures") or [])
                    for h in reversed(d.get("history", [])) if h["state"] == "failed"), "")
    runs = {}
    for name, run in d.get("route_runs", {}).items():
        out = job.output_dir(name)
        runs[name] = {
            **{k: run.get(k) for k in ("ok", "at", "cost_usd", "models", "problems", "flags", "facts")},
            "summary": (out / "summary.md").read_text(encoding="utf-8-sig", errors="replace")
                       if (out / "summary.md").exists() else "",
            "summary_uri": (out / "summary.md").as_uri() if (out / "summary.md").exists() else None,
            "extracted": _read_json(out / "extracted.json") if name in routes and routes[name].case_keys else None,
        }
    classification = d.get("classification") or {}
    return {
        "id": job.id,
        "subject": d.get("subject") or d.get("source_name"),
        "source": d.get("source_name"),
        "created": d.get("created_at"),
        "email_date": _email_date(job),
        "state": job.state,
        "verdict": verdict(job),
        "history": d.get("history", []),
        "warnings": preprocessed[-1].get("warnings", []) if preprocessed else [],
        "failure": failure,
        "candidates": classification.get("candidates", []),
        "routes": runs,
        "manual": bool(classification.get("manual")),
        "folder": job.dir.as_uri(),
    }


def _norm(value) -> str:
    return re.sub(r"[^0-9a-z]", "", str(value).lower())


def _same_amount(a: dict, b: dict) -> bool:
    return a.get("currency") == b.get("currency") and math.isclose(a["value"], b["value"], rel_tol=1e-9, abs_tol=0.005)


def _fmt(amount: dict | None) -> str:
    return f"{amount.get('currency') or ''} {amount['value']:,.0f}".strip() if amount else ""


def build_cases(records: list[dict], routes: dict[str, Route]) -> list[dict]:
    """Group jobs by (route, case_keys values) and check figures across their stages."""
    cases: dict[str, dict] = {}
    for rec in records:
        for name, run in rec["routes"].items():
            route, ex = routes.get(name), run.get("extracted")
            if not route or not route.case_keys or not isinstance(ex, dict):
                continue
            values = [ex.get(k) for k in route.case_keys]
            if any(v in (None, "") for v in values):
                continue
            key = "|".join([name, *(_norm(v) for v in values)])
            stages = (route.output_schema or {}).get("properties", {}).get(route.stage_field or "", {}).get("enum", [])
            case = cases.setdefault(key, {
                "key": key, "route": name, "keys": dict(zip(route.case_keys, map(str, values))),
                "title": " / ".join(str(ex[f]) for f in ("cedent", "event_name") if ex.get(f)) or " / ".join(map(str, values)),
                "stages": [s for s in stages if s], "steps": [],
                "notes": [],  # figures that contradict each other across stages
                "info": [],  # e.g. an update whose earlier emails were never processed
            })
            case["steps"].append({
                "job": rec["id"], "date": rec["email_date"], "subject": rec["subject"], "verdict": rec["verdict"],
                "stage": ex.get(route.stage_field) if route.stage_field else None,
                "facts": run.get("facts") or {}, "flags": run.get("flags") or [],
                "gross": _fmt(ex.get("gross_loss")), "previous": _fmt(ex.get("previous_estimate")),
                "cash_call": _fmt(ex.get("cash_call")), "_ex": ex,
            })
    for case in cases.values():
        case["steps"].sort(key=lambda s: s["date"] or "")
        last_gross = None  # (amount, job) of the latest estimate seen so far
        for step in case["steps"]:
            ex = step.pop("_ex")
            prev, gross = ex.get("previous_estimate"), ex.get("gross_loss")
            if prev and last_gross and not _same_amount(prev, last_gross[0]):
                case["notes"].append(f"{step['job']} states the previous estimate as {_fmt(prev)}, but the last "
                                     f"estimate received ({last_gross[1]}) was {_fmt(last_gross[0])}")
            if prev and not last_gross:
                case["info"].append(f"{step['job']} refers to a previous estimate of {_fmt(prev)} "
                                    "that is not among the processed emails")
            if gross:
                last_gross = (gross, step["job"])
        case["last"] = case["steps"][-1]["date"]
        case["verdict"] = next((v for v in ("failed", "check", "review", "active")
                                if any(s["verdict"] == v for s in case["steps"])), "ok")
        if case["notes"] and case["verdict"] == "ok":
            case["verdict"] = "check"
    return sorted(cases.values(), key=lambda c: c["last"] or "", reverse=True)


def build_report(jobs_dir: Path, routes: dict[str, Route]) -> dict:
    records = sorted((job_record(j, routes) for j in load_jobs(jobs_dir)),
                     key=lambda r: r["email_date"] or r["created"] or "", reverse=True)
    counts: dict[str, int] = {}
    for r in records:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {
        "generated": now_iso(),
        "jobs_dir": str(jobs_dir),
        "counts": counts,
        "routes": sorted(routes),
        "cases": build_cases(records, routes),
        "jobs": records,
    }


def write_report(jobs_dir: Path, routes: dict[str, Route]) -> Path:
    data = json.dumps(build_report(jobs_dir, routes), ensure_ascii=False, default=str)
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data.replace("</", "<\\/"))
    target = jobs_dir / REPORT_NAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, target)  # the open page reloads a complete file, never a half-written one
    return target
