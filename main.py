"""Polling loop and orchestration.

    uv run email-pipeline --config config.toml
    uv run email-pipeline --config config.toml --once     # single pass, for testing
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import time
from pathlib import Path

from .classifier import classify
from .config import Config, load_config
from .jobs import ACTIVE_STATES, Job, content_hash, create_job, load_jobs, now_iso
from .notify import notify
from .preprocess import preprocess
from .routes import load_routes
from .runner import run_route

log = logging.getLogger("pipeline")
INGESTED = "_ingested"


def ready_items(inbox: Path, settle_seconds: int) -> list[Path]:
    """Inbox items that have not changed for settle_seconds (finished copying)."""
    items = []
    cutoff = time.time() - settle_seconds
    for p in sorted(inbox.iterdir()):
        if p.name.startswith(("_", ".", "~$")):
            continue
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        if files and max(f.stat().st_mtime for f in files) < cutoff:
            items.append(p)
    return items


def ingest(cfg: Config, known_hashes: set[str]) -> None:
    archive = cfg.inbox_dir / INGESTED
    archive.mkdir(exist_ok=True)
    for item in ready_items(cfg.inbox_dir, cfg.settle_seconds):
        digest = content_hash(item)
        if digest in known_hashes:
            log.info("duplicate, skipping: %s", item.name)
        else:
            job = create_job(cfg.jobs_dir, item, digest)
            known_hashes.add(digest)
            log.info("new job %s from %s", job.id, item.name)
        # Original is safely copied into the job; move it out of the inbox.
        dest = archive / f"{now_iso().replace(':', '')}_{item.name}"
        shutil.move(str(item), str(dest))


async def process(job: Job, cfg: Config) -> None:
    routes = load_routes(cfg.routes_dir)  # reloaded every time: new routes need no restart

    if job.state == "new":
        s = preprocess(job)
        job.set_state("preprocessed", warnings=s["warnings"])

    if job.state == "preprocessed":
        result = await classify(job, routes, cfg)
        job.data["classification"] = result
        if not result["accepted"]:
            job.set_state("needs_review", reason="no route above confidence threshold")
            notify(cfg.teams_webhook_url, "Needs manual review", _candidates_text(result), {"Job": job.id, "Item": job.data["source_name"]})
            return
        job.set_state("classified", routes=result["accepted"])

    if job.state == "classified":
        job.set_state("running")
        failures = []
        for name in job.data["classification"]["accepted"]:
            if job.data["route_runs"].get(name, {}).get("ok"):
                continue  # already done in a previous (interrupted) run
            route = routes.get(name)
            if route is None:
                failures.append(f"{name}: route no longer exists")
                continue
            res = await run_route(job, route, cfg)
            summary_file = job.output_dir(name) / "summary.md"
            summary = summary_file.read_text(encoding="utf-8") if summary_file.exists() else res.text
            ok = not res.is_error and summary_file.exists()
            job.data["route_runs"][name] = {"ok": ok, "at": now_iso(), "cost_usd": res.cost_usd, "turns": res.num_turns}
            job.save()
            notify(
                cfg.teams_webhook_url,
                f"{'✅' if ok else '⚠️'} {name}",
                summary,
                {"Job": job.id, "Item": job.data["source_name"], "Cost (USD)": res.cost_usd},
            )
            if not ok:
                failures.append(f"{name}: agent error or no summary.md")
        job.set_state("failed" if failures else "done", failures=failures)


def _candidates_text(result: dict) -> str:
    if not result["candidates"]:
        return "No route matched."
    return "\n".join(f"- {c['name']} ({c['confidence']:.2f}): {c['reason']}" for c in result["candidates"])


async def run(cfg: Config, once: bool) -> None:
    for d in (cfg.inbox_dir, cfg.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    jobs = load_jobs(cfg.jobs_dir)
    known = {j.data["sha256"] for j in jobs}
    for j in jobs:  # crash recovery
        if j.state == "running":
            j.set_state("classified", note="recovered after restart")

    while True:
        try:
            ingest(cfg, known)
        except Exception:
            log.exception("ingest failed")
        for job in load_jobs(cfg.jobs_dir):
            if job.state not in ACTIVE_STATES:
                continue
            try:
                await process(job, cfg)
            except Exception as exc:
                log.exception("job %s failed", job.id)
                job.set_state("failed", error=repr(exc))
                notify(cfg.teams_webhook_url, "❌ Pipeline error", repr(exc), {"Job": job.id})
        if once:
            return
        await asyncio.sleep(cfg.poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Email/file processing pipeline")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--once", action="store_true", help="process the inbox once and exit")
    args = parser.parse_args()
    cfg = load_config(args.config)
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.jobs_dir / "pipeline.log", encoding="utf-8")],
    )
    asyncio.run(run(cfg, args.once))


if __name__ == "__main__":
    main()
