"""Classification: which route(s) does a job belong to?

The list of categories is built from the routes on disk, so adding a route
folder automatically extends the classifier.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import Config
from .jobs import Job
from .routes import Route

MAX_BODY_CHARS = 6000

SYSTEM_PROMPT = """You are the intake classifier for the actuarial team of a small reinsurance company.
You receive the content of one forwarded email or file and a list of routes (business processes).
Decide which route(s) the item belongs to. An item can belong to several routes, or to none.
Be conservative: if nothing clearly fits, return an empty list.
Respond with JSON only, no prose, no code fences, in exactly this shape:
{"routes": [{"name": "<route name>", "confidence": <0..1>, "reason": "<one sentence>"}]}"""


def describe_routes(routes: dict[str, Route]) -> str:
    parts = []
    for r in routes.values():
        ex = "; ".join(r.examples) if r.examples else "(none)"
        parts.append(f"- name: {r.name}\n  description: {r.description}\n  example subjects: {ex}")
    return "\n".join(parts)


def describe_job(job: Job) -> str:
    summary = json.loads((job.extracted_dir / "summary.json").read_text(encoding="utf-8"))
    parts = []
    for e in summary["emails"]:
        body = (job.extracted_dir / e["body"]).read_text(encoding="utf-8")
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n[... truncated ...]"
        parts.append(f"EMAIL {e['source']}\nattachments: {e['attachments']}\n{body}")
    if summary["files"]:
        parts.append(f"LOOSE FILES: {summary['files']}")
    return "\n\n".join(parts) or f"(no readable content; original name: {job.data['source_name']})"


def parse_classification(text: str, known: set[str]) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"classifier returned no JSON: {text[:200]!r}")
    data = json.loads(m.group(0))
    out = []
    for item in data.get("routes", []):
        if item.get("name") in known:
            out.append({"name": item["name"], "confidence": float(item.get("confidence", 0)), "reason": item.get("reason", "")})
    return out


async def classify(job: Job, routes: dict[str, Route], cfg: Config) -> dict:
    prompt = f"ROUTES:\n{describe_routes(routes)}\n\nITEM:\n{describe_job(job)}"
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],          # load no CLAUDE.md / settings from disk
        cwd=str(job.dir),
        model=cfg.classifier_model,
    )
    res = await run_agent(prompt, options)
    candidates = parse_classification(res.text, set(routes))
    accepted = [c for c in candidates if c["confidence"] >= cfg.confidence_threshold]
    return {"candidates": candidates, "accepted": [c["name"] for c in accepted], "cost_usd": res.cost_usd}
