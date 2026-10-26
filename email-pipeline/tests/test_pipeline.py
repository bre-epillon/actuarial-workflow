"""The whole loop (ingest -> preprocess -> classify -> route -> notify) with fake agents."""
import asyncio
import dataclasses
import json
import os
import time

import pytest

from conftest import FIXTURES, FakeAgents, first_body, job_from, valid_large_loss
from pipeline import commands, main
from pipeline.agent import AgentUnavailable
from pipeline.classifier import classify
from pipeline.jobs import load_jobs
from pipeline.preprocess import preprocess, preprocess_isolated
from pipeline.routes import load_routes
from pipeline.runner import route_options

SMOKE = [{"name": "smoke_test", "confidence": 0.95, "reason": "test subject"}]
LOSS = [{"name": "large_loss", "confidence": 0.9, "reason": "loss advice"}]


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr("pipeline.main.notify", lambda url, title, text, facts: sent.append((title, text, facts)) or True)
    return sent


def drop(cfg, fixture: str) -> None:
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.inbox_dir / fixture
    target.write_bytes((FIXTURES / fixture).read_bytes())
    past = time.time() - 60  # already settled
    os.utime(target, (past, past))


def run_once(cfg) -> None:
    asyncio.run(main.run(cfg, once=True))


def the_job(cfg):
    [job] = load_jobs(cfg.jobs_dir)
    return job


def test_smoke_flow_end_to_end(cfg, monkeypatch, notes):
    FakeAgents(SMOKE).install(monkeypatch)
    drop(cfg, "smoke_test.eml")
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "done" and job.data["subject"] == "[PIPELINE TEST] hello"
    run = job.data["route_runs"]["smoke_test"]
    assert run["ok"] and run["versions"]["claude-agent-sdk"] and run["models"] == ["fake-sonnet"]
    assert not list(cfg.inbox_dir.glob("*.eml"))
    assert len(list((cfg.inbox_dir / "_ingested").iterdir())) == 1
    [(title, text, facts)] = notes
    assert title.startswith("✅ smoke_test") and text == "smoke_test done" and facts["Job"] == job.id


def test_duplicate_is_skipped(cfg, monkeypatch, notes):
    FakeAgents(SMOKE).install(monkeypatch)
    drop(cfg, "smoke_test.eml")
    run_once(cfg)
    drop(cfg, "smoke_test.eml")
    run_once(cfg)
    assert len(load_jobs(cfg.jobs_dir)) == 1
    assert notes[-1][0] == "Duplicate skipped"


def test_once_waits_for_files_still_being_copied(cfg, monkeypatch, notes):
    """Files written seconds before `--once` used to be skipped silently."""
    FakeAgents(SMOKE).install(monkeypatch)
    cfg = dataclasses.replace(cfg, settle_seconds=1)
    cfg.inbox_dir.mkdir(parents=True)
    (cfg.inbox_dir / "smoke_test.eml").write_bytes((FIXTURES / "smoke_test.eml").read_bytes())  # mtime: now
    run_once(cfg)
    assert the_job(cfg).state == "done"


def test_one_bad_item_does_not_stop_the_others(cfg, monkeypatch, notes):
    FakeAgents(SMOKE).install(monkeypatch)
    real_create = main.create_job

    def create(jobs_dir, item, digest):
        if item.name == "broken.msg":
            raise ValueError("unreadable item")
        return real_create(jobs_dir, item, digest)

    monkeypatch.setattr("pipeline.main.create_job", create)
    drop(cfg, "smoke_test.eml")
    (cfg.inbox_dir / "broken.msg").write_bytes(b"not an email")
    past = time.time() - 60
    os.utime(cfg.inbox_dir / "broken.msg", (past, past))
    run_once(cfg)
    assert the_job(cfg).state == "done"
    assert (cfg.inbox_dir / "broken.msg").exists()  # left in the inbox for the next poll


def test_one_job_failing_to_read_does_not_stop_the_others(cfg, monkeypatch, notes):
    FakeAgents(SMOKE).install(monkeypatch)
    real_preprocess = main.preprocess_isolated

    def flaky(job, timeout):
        if job.data["source_name"] == "quarterly_accounts.eml":
            raise TimeoutError("reading the item took longer than 300 s and was stopped")
        return real_preprocess(job, timeout)

    monkeypatch.setattr("pipeline.main.preprocess_isolated", flaky)
    for fixture in ("quarterly_accounts.eml", "smoke_test.eml"):
        drop(cfg, fixture)
    run_once(cfg)
    states = {j.data["source_name"]: j.state for j in load_jobs(cfg.jobs_dir)}
    assert states == {"quarterly_accounts.eml": "failed", "smoke_test.eml": "done"}
    assert any(title.startswith("❌ Pipeline error") for title, _, _ in notes)


def test_reading_runs_in_a_child_process_with_a_timeout(tmp_path):
    job = job_from(tmp_path / "a", FIXTURES / "large_loss_advice.eml")
    assert preprocess_isolated(job, timeout=120) == preprocess(job_from(tmp_path / "b", FIXTURES / "large_loss_advice.eml"))
    with pytest.raises(TimeoutError):
        preprocess_isolated(job_from(tmp_path / "c", FIXTURES / "large_loss_advice.eml"), timeout=0.01)


def test_unmovable_item_is_reported_once(cfg, monkeypatch, notes):
    def locked(src, dst):
        raise PermissionError("still open in Outlook")

    monkeypatch.setattr("pipeline.main.shutil.move", locked)
    drop(cfg, "smoke_test.eml")
    known, handled = {}, set()
    for _ in range(3):  # three polls while the file stays locked
        main.ingest(cfg, known, handled)
    assert len(load_jobs(cfg.jobs_dir)) == 1 and notes == []


def test_unsettled_and_system_files_wait(cfg):
    cfg.inbox_dir.mkdir(parents=True)
    (cfg.inbox_dir / "desktop.ini").write_text("x")
    (cfg.inbox_dir / "~$lock.xlsx").write_text("x")
    (cfg.inbox_dir / "fresh.msg").write_text("x")
    assert main.ready_items(cfg.inbox_dir, settle_seconds=30) == []


def test_needs_review_then_assign(cfg, monkeypatch, notes):
    FakeAgents([{"name": "large_loss", "confidence": 0.4, "reason": "unsure"}]).install(monkeypatch)
    drop(cfg, "quarterly_accounts.eml")
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "needs_review"
    assert "large_loss (0.40)" in notes[-1][1]
    assert commands.assign(cfg, job.id, ["smoke_test"]) == 0
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "done" and job.data["classification"]["manual"]


def test_large_loss_repair_then_postprocess(cfg, monkeypatch, notes):
    def invalid(job_dir):
        return json.dumps(dict(valid_large_loss(first_body(job_dir)), date_of_loss="14/09/2026"))

    def valid(job_dir):
        return json.dumps(valid_large_loss(first_body(job_dir)))

    fake = FakeAgents(LOSS, {"large_loss": [{"extracted.json": invalid, "summary.md": "draft"},
                                            {"extracted.json": valid}]})
    fake.install(monkeypatch)
    drop(cfg, "large_loss_advice.eml")
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "done"
    assert "date_of_loss" in fake.prompts[-1]  # the repair prompt quotes the validation error
    run = job.data["route_runs"]["large_loss"]
    assert run["facts"]["Movement vs previous"] == "EUR +2,500,000" and run["turns"] == 8
    assert (job.output_dir("large_loss") / "computed.json").exists()
    title, _, facts = notes[-1]
    assert title.startswith("✅ large_loss: Large Loss Advice") and facts["Gross loss (100%)"] == "EUR 12,500,000"


def test_large_loss_invalid_twice_fails(cfg, monkeypatch, notes):
    invalid = json.dumps(dict(valid_large_loss(), underwriting_year="2026"))
    FakeAgents(LOSS, {"large_loss": [{"extracted.json": invalid, "summary.md": "x"}, {"extracted.json": invalid}]}).install(monkeypatch)
    drop(cfg, "large_loss_advice.eml")
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "failed"
    assert any("underwriting_year" in p for p in job.data["route_runs"]["large_loss"]["problems"])
    assert notes[-1][0].startswith("⚠️ large_loss")


def test_unavailable_pauses_and_resumes(cfg, monkeypatch, notes):
    fake = FakeAgents(SMOKE)
    fake.install(monkeypatch)

    async def down(prompt, options, log_path=None):
        raise AgentUnavailable("usage limit reached", retry_at=time.time() + 3600)

    monkeypatch.setattr("pipeline.runner.run_agent", down)
    drop(cfg, "smoke_test.eml")
    run_once(cfg)
    assert the_job(cfg).state == "classified"  # nothing wrong with the job
    assert notes[-1][0] == "⏸️ Pipeline paused"
    monkeypatch.setattr("pipeline.runner.run_agent", fake.route)
    run_once(cfg)
    assert the_job(cfg).state == "done"


def test_retry_failed_job_sets_old_output_aside(cfg, monkeypatch, notes):
    FakeAgents(SMOKE, {"smoke_test": [{"echo.md": "no summary this time"}]}).install(monkeypatch)
    drop(cfg, "smoke_test.eml")
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "failed" and "no summary.md" in job.data["route_runs"]["smoke_test"]["problems"][0]
    assert commands.retry(cfg, job.id[-8:], "classified") == 0
    run_once(cfg)
    job = the_job(cfg)
    assert job.state == "done"
    assert not (job.output_dir("smoke_test") / "echo.md").exists()
    assert list((job.dir / "output" / "_previous").glob("smoke_test_*/echo.md"))


def test_status_lists_jobs(cfg, monkeypatch, notes, capsys):
    FakeAgents(SMOKE).install(monkeypatch)
    drop(cfg, "smoke_test.eml")
    run_once(cfg)
    commands.status(cfg, show_all=False)
    assert "done" in capsys.readouterr().out


def test_route_agent_is_locked_down(cfg):
    job = job_from(cfg.jobs_dir, FIXTURES / "smoke_test.eml")
    route = load_routes(cfg.routes_dir)["large_loss"]
    o = route_options(job, route, cfg)
    assert o.setting_sources == [] and o.strict_mcp_config and o.skills == [] and o.verbatim_prompts
    assert o.tools == o.allowed_tools == ["Read", "Write", "Glob", "Grep"]
    assert {"WebFetch", "WebSearch", "Bash", "PowerShell"} <= set(o.disallowed_tools)
    assert o.permission_mode == "dontAsk" and o.cwd == str(job.dir)
    assert {"restricted", "no-session-persistence"} <= set(o.extra_args)
    assert o.hooks["PreToolUse"][0].hooks and o.system_prompt["append"] == route.instructions


def test_classifier_sees_body_and_attachment_previews(cfg, monkeypatch):
    fake = FakeAgents([])
    fake.install(monkeypatch)
    job = job_from(cfg.jobs_dir, FIXTURES / "large_loss_advice.eml")
    preprocess(job)
    routes = load_routes(cfg.routes_dir)
    assert asyncio.run(classify(job, routes, cfg))["accepted"] == []
    o = fake.options[0]
    assert o.tools == [] and o.setting_sources == [] and o.verbatim_prompts
    assert o.output_format["schema"]["properties"]["routes"]["items"]["properties"]["name"]["enum"] == sorted(routes)
    assert "EUR 12.5m" in fake.prompts[0] and "Harbour Town" in fake.prompts[0]


def test_single_instance(tmp_path):
    with main.single_instance(tmp_path):
        with pytest.raises(SystemExit):
            with main.single_instance(tmp_path):
                pass
