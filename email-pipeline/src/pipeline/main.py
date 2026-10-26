"""Polling loop, orchestration and the command line.

    uv run python -m pipeline                       # watch the inbox (Ctrl+C to stop)
    uv run python -m pipeline --once                # single pass, for testing
    uv run python -m pipeline status                # recent jobs
    uv run python -m pipeline retry <job>           # run a failed job again
    uv run python -m pipeline assign <job> <route>  # route a needs_review job by hand
    uv run python -m pipeline doctor                # check install, login, config, routes
    uv run python -m pipeline login                 # log in to Claude (browser)
    uv run python -m pipeline selftest              # live check: agents work and stay sandboxed
    uv run python -m pipeline eval-classifier       # live classifier regression

(`uv run email-pipeline ...` is the same, where Defender allows uv's launcher .exe.)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .agent import AgentUnavailable, sdk_versions
from .classifier import classify
from .config import Config, load_config
from .jobs import ACTIVE_STATES, Job, content_hash, create_job, load_jobs, now_iso, safe_name
from .notify import notify
from .preprocess import preprocess
from .routes import load_routes
from .runner import run_route
from .versions import pipeline_version

log = logging.getLogger("pipeline")
INGESTED = "_ingested"
IGNORED = {"desktop.ini", "thumbs.db"}
PAUSE_SECONDS = 15 * 60  # wait this long when Claude is unavailable and gives no reset time


def ready_items(inbox: Path, settle_seconds: int) -> list[Path]:
    """Inbox items that have not changed for settle_seconds (finished copying)."""
    items = []
    cutoff = time.time() - settle_seconds
    for p in sorted(inbox.iterdir()):
        if p.name.startswith(("_", ".", "~$")) or p.name.lower() in IGNORED:
            continue
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        if files and max(f.stat().st_mtime for f in files) < cutoff:
            items.append(p)
    return items


def ingest(cfg: Config, known: dict[str, str], handled: set[tuple[str, str]] | None = None) -> None:
    """Turn settled inbox items into jobs. `known` maps content hash -> job id.

    `handled` remembers items already turned into a job or reported as duplicates, so
    an item that cannot be moved out of the inbox (still open somewhere) is not
    reported again on every poll.
    """
    handled = set() if handled is None else handled
    archive = cfg.inbox_dir / INGESTED
    archive.mkdir(exist_ok=True)
    for item in ready_items(cfg.inbox_dir, cfg.settle_seconds):
        try:
            digest = content_hash(item)
            key = (digest, item.name)
            if digest not in known:
                job = create_job(cfg.jobs_dir, item, digest)
                known[digest] = job.id
                log.info("new job %s from %s", job.id, item.name)
            elif key not in handled:
                log.info("duplicate of job %s, skipping: %s", known[digest], item.name)
                notify(cfg.teams_webhook_url, "Duplicate skipped",
                       f"Already processed as job {known[digest]}. To process it again: "
                       f"uv run python -m pipeline retry {known[digest]}", {"Item": item.name})
            handled.add(key)
            # The original is safely copied into the job; move it out of the inbox.
            shutil.move(str(item), str(archive / f"{now_iso().replace(':', '')}_{safe_name(item.name, 100)}"))
        except OSError:
            log.exception("could not ingest %s (will retry next poll)", item.name)


def _subject(job: Job) -> str:
    return job.data.get("subject") or job.data["source_name"]


def _facts(job: Job, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"Job": job.id, "Item": job.data["source_name"], **(extra or {}), "Folder": str(job.dir)}


def _set_aside_old_output(job: Job, route: str) -> None:
    """A re-run starts from an empty output folder; keep the previous attempt for reference."""
    out = job.output_dir(route)
    if out.exists() and any(out.iterdir()):
        dest = job.dir / "output" / "_previous" / f"{route}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), str(dest))


async def process(job: Job, cfg: Config) -> None:
    routes = load_routes(cfg.routes_dir)  # reloaded every time: new routes need no restart

    if job.state == "running":  # interrupted earlier
        job.set_state("classified", note="resumed")

    if job.state == "new":
        s = preprocess(job)
        job.data["subject"] = next((e["subject"] for e in s["emails"] if e["subject"]), None)
        job.set_state("preprocessed", warnings=s["warnings"])

    if job.state == "preprocessed":
        result = await classify(job, routes, cfg)
        job.data["classification"] = result
        if not result["accepted"]:
            job.set_state("needs_review", reason="no route above confidence threshold")
            notify(cfg.teams_webhook_url, f"Needs manual review: {_subject(job)}",
                   _candidates_text(result) + f"\n\nTo route it by hand: uv run python -m pipeline assign {job.id} <route>",
                   _facts(job))
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
            _set_aside_old_output(job, name)
            try:
                outcome = await run_route(job, route, cfg)
            except AgentUnavailable:
                job.set_state("classified", note="Claude unavailable; will resume")
                raise
            job.data["route_runs"][name] = {
                "ok": outcome.ok,
                "at": now_iso(),
                "cost_usd": outcome.cost_usd,
                "turns": outcome.turns,
                "models": outcome.models,
                "problems": outcome.problems,
                "flags": outcome.flags,
                "facts": outcome.facts,
                "versions": {"email-pipeline": pipeline_version(), **sdk_versions(), **outcome.versions},
            }
            job.save()
            text = outcome.summary
            if outcome.flags:
                text += "\n\nChecks:\n" + "\n".join(f"- {f}" for f in outcome.flags)
            if outcome.problems:
                text += "\n\nProblems:\n" + "\n".join(f"- {p}" for p in outcome.problems)
            icon = "✅" if outcome.ok and not outcome.flags else "⚠️"
            cost = {"Cost (USD, API-equivalent)": f"{outcome.cost_usd:.3f}"} if outcome.cost_usd else {}
            notify(cfg.teams_webhook_url, f"{icon} {name}: {_subject(job)}", text, _facts(job, {**outcome.facts, **cost}))
            if not outcome.ok:
                failures.append(f"{name}: {'; '.join(outcome.problems)}")
        job.set_state("failed" if failures else "done", failures=failures)


def _candidates_text(result: dict) -> str:
    if not result["candidates"]:
        return "No route matched."
    return "\n".join(f"- {c['name']} ({c['confidence']:.2f}): {c['reason']}" for c in result["candidates"])


async def process_pending(cfg: Config) -> None:
    for job in load_jobs(cfg.jobs_dir):
        if job.state not in ACTIVE_STATES:
            continue
        try:
            await process(job, cfg)
        except AgentUnavailable:
            raise  # nothing is wrong with the job; stop the pass and wait
        except Exception as exc:
            log.exception("job %s failed", job.id)
            try:
                job.set_state("failed", error=repr(exc))
            except OSError:  # e.g. manifest locked: the job is retried at the next poll
                log.exception("could not record the failure of job %s", job.id)
            notify(cfg.teams_webhook_url, f"❌ Pipeline error: {_subject(job)}",
                   f"{exc!r}\n\nTo try again: uv run python -m pipeline retry {job.id}", _facts(job))


async def run(cfg: Config, once: bool) -> None:
    for d in (cfg.inbox_dir, cfg.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    jobs = load_jobs(cfg.jobs_dir)
    known = {j.data["sha256"]: j.id for j in jobs}
    for j in jobs:  # crash recovery
        if j.state == "running":
            j.set_state("classified", note="recovered after restart")
    log.info("watching %s (jobs in %s, routes: %s)", cfg.inbox_dir, cfg.jobs_dir, ", ".join(load_routes(cfg.routes_dir)) or "none")

    paused_until, pause_notified = 0.0, False
    handled: set[tuple[str, str]] = set()
    while True:
        try:
            ingest(cfg, known, handled)
        except Exception:
            log.exception("ingest failed")
        if time.time() >= paused_until:
            try:
                await process_pending(cfg)
                pause_notified = False
            except AgentUnavailable as exc:
                paused_until = exc.retry_at or time.time() + PAUSE_SECONDS
                resume = datetime.fromtimestamp(paused_until).strftime("%H:%M")
                log.warning("Claude unavailable (%s); pausing until %s", exc, resume)
                if not pause_notified:
                    notify(cfg.teams_webhook_url, "⏸️ Pipeline paused",
                           f"Claude is unavailable: {exc}\nWaiting jobs resume automatically at about {resume}. "
                           "If you are logged out, run: uv run python -m pipeline login", {})
                    pause_notified = True
        if once:
            return
        await asyncio.sleep(cfg.poll_seconds)


@contextmanager
def single_instance(jobs_dir: Path):
    """Refuse to start twice on the same jobs folder (two loops would race on the inbox)."""
    fh = (jobs_dir / "pipeline.lock").open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(f"email-pipeline is already running for {jobs_dir}")
    try:
        yield
    finally:
        fh.close()  # releases the lock


def setup_logging(log_file: Path | None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # emoji on a cp1252 console must not crash logging
        except (AttributeError, ValueError):
            pass
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description="Email/file processing pipeline")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="default: ./config.toml")
    parser.add_argument("--once", action="store_true", help="process the inbox once and exit")
    sub = parser.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser("status", help="list recent jobs")
    p.add_argument("--all", action="store_true", help="all jobs, not only the last 20")
    p = sub.add_parser("retry", help="run a failed / needs_review / done job again")
    p.add_argument("job", help="job id, or a unique part of it (e.g. the 8-char hash)")
    p.add_argument("--from", dest="stage", choices=["new", "preprocessed", "classified"], default="classified",
                   help="new: redo everything; preprocessed: classify again; classified (default): re-run routes that did not succeed")
    p = sub.add_parser("assign", help="send a job to specific route(s) by hand")
    p.add_argument("job")
    p.add_argument("routes", nargs="+")
    p = sub.add_parser("doctor", help="check the installation, Claude login, config and routes")
    p.add_argument("--notify-test", action="store_true", help="also post a test card to Teams")
    sub.add_parser("login", help="log in to Claude with your subscription (opens the browser)")
    p = sub.add_parser("eval-classifier", help="run the classifier on tests/classifier_cases.yaml (calls Claude)")
    p.add_argument("--cases", type=Path, default=None)
    sub.add_parser("selftest", help="live check that agents work and stay sandboxed (calls Claude, ~1 minute)")
    return parser


def main(argv: list[str] | None = None) -> None:
    from . import commands

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "login":
        sys.exit(commands.login())
    if args.command == "doctor":
        setup_logging(None)
        sys.exit(commands.doctor(args.config, args.notify_test))
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "status":
        sys.exit(commands.status(cfg, args.all))
    if args.command == "retry":
        sys.exit(commands.retry(cfg, args.job, args.stage))
    if args.command == "assign":
        sys.exit(commands.assign(cfg, args.job, args.routes))
    if args.command in ("eval-classifier", "selftest"):
        setup_logging(None)
        run_command = commands.eval_classifier(cfg, args.cases) if args.command == "eval-classifier" else commands.selftest(cfg)
        sys.exit(asyncio.run(run_command))

    setup_logging(cfg.jobs_dir / "pipeline.log")
    with single_instance(cfg.jobs_dir):
        try:
            asyncio.run(run(cfg, args.once))
        except KeyboardInterrupt:
            log.info("stopped")


if __name__ == "__main__":
    main()
