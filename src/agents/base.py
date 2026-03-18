"""Base agent contract and shared agent loop utility."""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import polars as pl
from anthropic import AsyncAnthropic
from mcp import ClientSession

_logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Structured result from any agent."""
    text: str = ""
    data: pl.DataFrame | None = None
    chart_config: dict | None = None
    dashboard_html: str | None = None
    last_sql: str | None = None
    agent_name: str = ""


class BaseAgent(ABC):
    """Abstract base for all specialist agents."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description for the orchestrator to understand capabilities."""
        ...

    @abstractmethod
    async def run(
        self,
        question: str,
        session: ClientSession,
        schema: str,
        context: dict[str, Any] | None = None,
    ) -> AgentResult:
        ...


async def run_tool_loop(
    client: AsyncAnthropic,
    session: ClientSession,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 2048,
) -> tuple[str, str | None]:
    """Generic agent loop: call Claude, execute tool_use blocks via MCP, repeat until end_turn.

    Returns:
        (final_text, last_sql_executed_or_None)
    """
    last_sql: str | None = None

    while True:
        t_call = time.monotonic()
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        _logger.info(
            "Claude stop_reason=%s blocks=%d (%.1fs)",
            response.stop_reason,
            len(response.content),
            time.monotonic() - t_call,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason in ("end_turn", "max_tokens"):
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text, last_sql
            return "(No text response from Claude)", last_sql

        if response.stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                _logger.info("tool_use name=%s", block.name)
                if block.name == "execute_sql":
                    last_sql = block.input.get("sql", "").strip() or last_sql
                mcp_result = await session.call_tool(block.name, block.input)
                result_text = "\n".join(
                    c.text for c in mcp_result.content if hasattr(c, "text")
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        _logger.warning("Unexpected stop_reason=%s", response.stop_reason)
        break

    return "Claude stopped unexpectedly. Please try again.", last_sql
