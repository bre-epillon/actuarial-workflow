"""agent.run_agent's error classification, with claude_agent_sdk.query replaced by canned messages."""
import asyncio

import pytest
from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, RateLimitInfo, ResultMessage

from pipeline.agent import AgentUnavailable, run_agent


def result(is_error=False, status=None, text="ok"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=2,
                         session_id="s", result=text, total_cost_usd=0.01, api_error_status=status)


def limit(overage=None):
    info = RateLimitInfo(status="rejected", resets_at=1_900_000_000, rate_limit_type="five_hour", overage_status=overage)
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")


def run_with(monkeypatch, messages, tmp_path):
    async def fake_query(prompt, options):
        for m in messages:
            yield m

    monkeypatch.setattr("pipeline.agent.query", fake_query)
    return asyncio.run(run_agent("hi", ClaudeAgentOptions(), tmp_path / "log.jsonl"))


def test_success_is_returned_and_logged(monkeypatch, tmp_path):
    res = run_with(monkeypatch, [result()], tmp_path)
    assert not res.is_error and res.text == "ok" and res.cost_usd == 0.01
    assert '"type": "ResultMessage"' in (tmp_path / "log.jsonl").read_text(encoding="utf-8")


def test_success_served_by_extra_usage_is_kept(monkeypatch, tmp_path):
    """status stays 'rejected' while overage pays for the request: the run must not be discarded."""
    assert not run_with(monkeypatch, [limit(overage="allowed"), result()], tmp_path).is_error
    assert not run_with(monkeypatch, [limit(), result()], tmp_path).is_error  # success always wins


def test_usage_limit_pauses(monkeypatch, tmp_path):
    with pytest.raises(AgentUnavailable) as exc:
        run_with(monkeypatch, [limit(), result(is_error=True, text="limit reached")], tmp_path)
    assert exc.value.retry_at == 1_900_000_000


@pytest.mark.parametrize("status,unavailable", [(429, True), (529, True), (401, True), (400, False)])
def test_api_errors(monkeypatch, tmp_path, status, unavailable):
    messages = [result(is_error=True, status=status, text="API Error")]
    if unavailable:
        with pytest.raises(AgentUnavailable):
            run_with(monkeypatch, messages, tmp_path)
    else:
        assert run_with(monkeypatch, messages, tmp_path).is_error  # the job fails, the loop goes on
