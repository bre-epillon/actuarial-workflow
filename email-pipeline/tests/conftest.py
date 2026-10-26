"""Shared test helpers. Tests never call Claude: agents are replaced by fakes."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline.agent import AgentResult
from pipeline.config import Config
from pipeline.jobs import Job, content_hash, create_job

REPO = Path(__file__).parents[1]
ROUTES_DIR = REPO / "routes"
FIXTURES = REPO / "tests" / "fixtures"


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(inbox_dir=tmp_path / "inbox", jobs_dir=tmp_path / "jobs", routes_dir=ROUTES_DIR, settle_seconds=0,
                  preprocess_timeout=0)  # in-process: fast, and monkeypatches stay visible


def job_from(jobs_dir: Path, source: Path) -> Job:
    return create_job(jobs_dir, source, content_hash(source))


QUOTES = {  # each one occurs verbatim in tests/fixtures/large_loss_advice.eml's body
    "cedent": "Nordwind Mutual Insurance",
    "broker": "Example Broker Ltd",
    "treaty_reference": "treaty ref. NWM-PCAT-2026-L2",
    "underwriting_year": "underwriting year 2026",
    "event_name": "Event: Storm Example",
    "date_of_loss": "Date of loss: 14 September 2026",
    "line_of_business": "Line of business: Property",
    "country": "Country: Exampleland",
}


def valid_large_loss(source: str = "extracted/x/body.txt") -> dict:
    """A correct extraction of large_loss_advice.eml, citing `source` everywhere."""
    def amount(value, text):
        return {"value": value, "currency": "EUR", "as_written": text, "source": source}

    return {
        "notification_type": "update",
        "cedent": "Nordwind Mutual Insurance",
        "broker": "Example Broker Ltd",
        "treaty_reference": "NWM-PCAT-2026-L2",
        "underwriting_year": 2026,
        "event_name": "Storm Example",
        "date_of_loss": "2026-09-14",
        "line_of_business": "Property",
        "country": "Exampleland",
        "gross_loss": amount(12500000, "EUR 12.5m"),
        "previous_estimate": amount(10000000, "EUR 10,000,000"),
        "cash_call": None,
        "cash_call_due_date": None,
        "our_share": {"percent": 7.5, "as_written": "7.5%", "source": source},
        "sources": {f: {"file": source, "quote": q} for f, q in QUOTES.items()},
        "notes": [],
    }


def first_body(job_dir: Path) -> str:
    """Path (relative to the job) of the first email body preprocess wrote."""
    return json.loads((job_dir / "extracted" / "summary.json").read_text(encoding="utf-8"))["emails"][0]["body"]


def loss_job(tmp_path: Path) -> Path:
    """A job folder whose extracted/x/body.txt is the large-loss fixture's text; returns output/large_loss."""
    job = job_from(tmp_path / "jobs", FIXTURES / "large_loss_advice.eml")
    from pipeline.preprocess import preprocess

    preprocess(job)
    body = job.dir / "extracted" / "x" / "body.txt"
    body.parent.mkdir()
    body.write_text((job.dir / first_body(job.dir)).read_text(encoding="utf-8"), encoding="utf-8")
    out = job.output_dir("large_loss")
    out.mkdir(parents=True)
    return out


class FakeAgents:
    """Stands in for run_agent in classifier.py and runner.py.

    `answer`: the classifier's structured output. `route_files`: per route, a list
    of {filename: content} written on successive runs of that route's agent; a
    content can be a function of the job directory.
    """

    def __init__(self, answer: list[dict], route_files: dict[str, list[dict]] | None = None):
        self.answer = answer
        self.route_files = route_files or {}
        self.prompts: list[str] = []
        self.options: list = []

    async def classify(self, prompt, options, log_path=None):
        self.prompts.append(prompt)
        self.options.append(options)
        return AgentResult(text="", is_error=False, cost_usd=0.001, num_turns=2,
                           structured={"routes": self.answer}, models=["fake-haiku"])

    async def route(self, prompt, options, log_path=None):
        self.prompts.append(prompt)
        self.options.append(options)
        name = re.search(r"for the '(\w+)' process", prompt).group(1)
        out = Path(options.cwd) / "output" / name
        out.mkdir(parents=True, exist_ok=True)
        runs = self.route_files.get(name) or [{"summary.md": f"{name} done"}]
        for filename, content in runs.pop(0).items():
            text = content(Path(options.cwd)) if callable(content) else content
            (out / filename).write_text(text, encoding="utf-8")
        return AgentResult(text="done", is_error=False, cost_usd=0.02, num_turns=4, models=["fake-sonnet"])

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr("pipeline.classifier.run_agent", self.classify)
        monkeypatch.setattr("pipeline.runner.run_agent", self.route)
