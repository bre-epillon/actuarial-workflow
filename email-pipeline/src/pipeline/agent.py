"""Thin wrapper around claude_agent_sdk.query.

The SDK changes quickly: if an import or field below fails, check the
installed version's API (see CLAUDE.md rule 7) and adapt only this file.
"""
from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query


@dataclass
class AgentResult:
    text: str
    is_error: bool
    cost_usd: float | None
    num_turns: int | None


async def run_agent(prompt: str, options: ClaudeAgentOptions) -> AgentResult:
    texts: list[str] = []
    result: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage):
            result = message
    final = (getattr(result, "result", None) or "\n".join(texts)).strip()
    return AgentResult(
        text=final,
        is_error=bool(getattr(result, "is_error", False)) if result else True,
        cost_usd=getattr(result, "total_cost_usd", None),
        num_turns=getattr(result, "num_turns", None),
    )
