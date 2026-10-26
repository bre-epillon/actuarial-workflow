"""Thin wrapper around claude_agent_sdk.query.

The SDK changes quickly: if an import or field below fails, check the
installed version's API (see CLAUDE.md rule 7) and adapt only this file.
Checked against claude-agent-sdk 0.2.174 (bundled Claude Code CLI 2.1.310).
"""
from __future__ import annotations

import dataclasses
import json
import platform
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

# Claude itself is unavailable (not logged in, usage limit, outage): retry later,
# the job is not at fault.
TRANSIENT_ERRORS = {"authentication_failed", "billing_error", "rate_limit", "server_error"}
TRANSIENT_HTTP = {401, 403, 408, 429, 500, 502, 503, 504, 529}
LOG_FIELD_LIMIT = 4000


class AgentUnavailable(Exception):
    """Claude could not serve the request; `retry_at` is a Unix time if known."""

    def __init__(self, message: str, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass
class AgentResult:
    text: str
    is_error: bool
    cost_usd: float | None
    num_turns: int | None
    subtype: str | None = None
    structured: Any = None
    models: list[str] = field(default_factory=list)
    denials: list[Any] = field(default_factory=list)


def isolated_options(**kwargs) -> ClaudeAgentOptions:
    """Options shared by every pipeline agent (CLAUDE.md rules 3 and 4).

    No settings, CLAUDE.md, skills, plugins or MCP servers from disk; prompts are
    taken literally (no @file expansion of email text); no session transcripts
    are written to ~/.claude (the job's logs/ folder is the audit trail).
    """
    extra_args = {"no-session-persistence": None, **kwargs.pop("extra_args", {})}
    env = {"CLAUDE_AGENT_SDK_CLIENT_APP": "email-pipeline", **kwargs.pop("env", {})}
    return ClaudeAgentOptions(
        setting_sources=[],
        strict_mcp_config=True,
        skills=[],
        verbatim_prompts=True,
        extra_args=extra_args,
        env=env,
        **kwargs,
    )


async def run_agent(prompt: str, options: ClaudeAgentOptions, log_path: Path | None = None) -> AgentResult:
    texts: list[str] = []
    models: list[str] = []
    result: ResultMessage | None = None
    transient: str | None = None
    retry_at: float | None = None
    log = _open_log(log_path)
    try:
        async for message in query(prompt=prompt, options=options):
            _write_log(log, message)
            if isinstance(message, AssistantMessage):
                texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                if message.model and message.model not in models:
                    models.append(message.model)
                if message.error in TRANSIENT_ERRORS:
                    transient = message.error
            elif isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                # "rejected" with overage allowed means extra usage is serving the request
                if info.status == "rejected" and info.overage_status not in ("allowed", "allowed_warning"):
                    transient = f"usage limit reached ({info.rate_limit_type or 'unknown window'})"
                    retry_at = info.resets_at
            elif isinstance(message, ResultMessage):
                result = message
    except CLIConnectionError as exc:  # includes CLINotFoundError
        raise AgentUnavailable(f"cannot start Claude Code: {exc}") from exc
    except ProcessError as exc:  # ResultError after an error result, or a CLI crash
        if result is None or result.is_error:
            status = getattr(exc, "api_error_status", None)
            network = status is None and getattr(exc, "terminal_reason", None) == "api_error"
            if transient or status in TRANSIENT_HTTP or network:
                raise AgentUnavailable(transient or f"Claude API error {status or ''}: {exc}", retry_at) from exc
            if result is None:
                raise
    finally:
        if log:
            log.close()

    failed = result is None or result.is_error  # a successful run is never thrown away
    if failed and (transient or (result is not None and result.api_error_status in TRANSIENT_HTTP)):
        raise AgentUnavailable(transient or f"Claude API error {result.api_error_status}", retry_at)
    final = (getattr(result, "result", None) or "\n".join(texts)).strip()
    return AgentResult(
        text=final,
        is_error=result.is_error if result else True,
        cost_usd=getattr(result, "total_cost_usd", None),
        num_turns=getattr(result, "num_turns", None),
        subtype=getattr(result, "subtype", None),
        structured=getattr(result, "structured_output", None),
        models=models,
        denials=list(getattr(result, "permission_denials", None) or []),
    )


def _open_log(path: Path | None):
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8")


def _write_log(fh, message) -> None:
    if fh is None:
        return
    data = dataclasses.asdict(message) if dataclasses.is_dataclass(message) else {"repr": repr(message)}
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "type": type(message).__name__, **_truncate(data)}
    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    fh.flush()


def _truncate(value):
    if isinstance(value, str) and len(value) > LOG_FIELD_LIMIT:
        return value[:LOG_FIELD_LIMIT] + f"... [{len(value) - LOG_FIELD_LIMIT} chars truncated]"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items() if k != "signature"}
    if isinstance(value, (list, tuple)):
        return [_truncate(v) for v in value]
    return value


def cli_path() -> str | None:
    """The Claude Code CLI the SDK will use: the bundled one, else one on PATH."""
    name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(bundled) if bundled.is_file() else shutil.which("claude")


def sdk_versions() -> dict[str, str]:
    try:
        from claude_agent_sdk._cli_version import __cli_version__  # private; may move
    except ImportError:
        __cli_version__ = "unknown"
    return {"claude-agent-sdk": claude_agent_sdk.__version__, "claude-code-cli": __cli_version__}
