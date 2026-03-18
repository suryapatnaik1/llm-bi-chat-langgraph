"""QueryAgent: text-to-SQL specialist."""
import logging
from typing import Any

import duckdb
import polars as pl
from mcp import ClientSession

from agents.base import AgentResult, BaseAgent, run_tool_loop
from config import DB_PATH, LLM_MODEL, get_async_client

_logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are a business intelligence analyst with direct access to a DuckDB database.

Database schema:
{schema}

When answering questions about the data:
1. Write a precise DuckDB SELECT query and call execute_sql to run it.
2. Interpret the results and provide a clear, concise answer.
3. Include the SQL you used in a fenced sql code block.

Do not make up data. If the query returns no rows, say so clearly.
"""


def _fetch_df(sql: str) -> pl.DataFrame | None:
    """Re-execute *sql* read-only against DuckDB and return a Polars DataFrame."""
    if not sql or not DB_PATH.exists():
        return None
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        rel = conn.execute(sql)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchall()
        conn.close()
        if not rows:
            return None
        return pl.DataFrame(rows, schema=cols, orient="row")
    except Exception as exc:
        _logger.warning("QueryAgent: _fetch_df error: %s", exc)
        return None


class QueryAgent(BaseAgent):
    """Answers factual questions about business data by writing and executing SQL."""

    @property
    def name(self) -> str:
        return "query_agent"

    @property
    def description(self) -> str:
        return (
            "Answer factual questions about business data by writing and executing SQL. "
            "Use for questions that need specific numbers, lists, comparisons, or data lookups."
        )

    async def run(
        self,
        question: str,
        session: ClientSession,
        schema: str,
        context: dict[str, Any] | None = None,
    ) -> AgentResult:
        client = get_async_client()
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(schema=schema)

        user_message = question
        if context and context.get("filter_context"):
            user_message = f"{context['filter_context']}\n\n{question}"

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message}
        ]

        # MCP tools are passed via context by the orchestrator
        tools = context.get("mcp_tools", []) if context else []

        text, last_sql = await run_tool_loop(
            client=client,
            session=session,
            model=LLM_MODEL,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=2048,
        )

        # Re-execute the last SQL to get a DataFrame for charting
        df = _fetch_df(last_sql) if last_sql else None

        return AgentResult(
            text=text,
            data=df,
            last_sql=last_sql,
            agent_name=self.name,
        )
