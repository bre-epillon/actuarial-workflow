"""Runs one route agent inside a job directory."""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from .agent import AgentResult, run_agent
from .config import Config
from .jobs import Job
from .routes import Route

ALWAYS_DENIED = ["WebFetch", "WebSearch"]

TASK_TEMPLATE = """A new item has arrived for the '{route}' process.

Working directory layout:
- ./extracted/         preprocessed inputs; start with ./extracted/summary.json
- ./original/          the untouched original (read-only: never modify)
- ./output/{route}/    the ONLY place you may write

Follow your route instructions. When finished, write ./output/{route}/summary.md:
at most 10 lines, suitable for a Teams notification, stating what you did, the key
figures you EXTRACTED (with the source file for each), and anything missing,
ambiguous or needing human review. If information is missing, say so: never guess."""


async def run_route(job: Job, route: Route, cfg: Config) -> AgentResult:
    job.output_dir(route.name).mkdir(parents=True, exist_ok=True)
    denied = list(ALWAYS_DENIED)
    if "Bash" not in route.allowed_tools:
        denied.append("Bash")
    options = ClaudeAgentOptions(
        # Claude Code's default system prompt + this route's instructions.
        # (Preset form as documented for the Python SDK; verify against your version.)
        system_prompt={"type": "preset", "preset": "claude_code", "append": route.instructions},
        allowed_tools=route.allowed_tools,
        disallowed_tools=denied,
        permission_mode="acceptEdits",
        setting_sources=[],
        cwd=str(job.dir),
        max_turns=cfg.max_turns,
        model=cfg.route_model,
    )
    return await run_agent(TASK_TEMPLATE.format(route=route.name), options)
