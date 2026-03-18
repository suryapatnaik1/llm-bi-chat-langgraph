"""ChartAgent: LLM-driven conversational chart builder.

Unlike the other agents, the ChartAgent does NOT use MCP or the orchestrator.
It runs as a multi-turn conversation directly between the Streamlit UI and the
LLM, using the DataFrame schema as context. When it has enough information,
it returns a ```chart-spec``` JSON block that the UI parses and renders via Plotly.
"""
import json
import logging
import os
import re
from typing import Any

import polars as pl

_logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a chart-building assistant helping the user visualize data from a DuckDB database.

Current DataFrame (from the most recent query):
{df_info}

Source SQL that produced this data:
{source_sql}

Database schema (available tables and columns):
{db_schema}

Original data question: "{original_question}"

Your job:
1. Understand what the user wants to visualize.
2. Ask ONE clarifying question at a time — only ask what's genuinely unclear.
3. Consider asking about (only if not already obvious):
   - What should be on the axes (e.g., "revenue over time" or "sales by region")
   - What chart type suits the data (bar, line, pie, scatter, area, histogram)
   - Time aggregation level (hourly, daily, monthly, yearly) — if a date column exists
   - Whether to compare with another metric or apply grouping
4. When you have enough information, return a ```chart-spec``` JSON block.

The chart-spec format:
```chart-spec
{{
  "chart_type": "bar|line|pie|scatter|area|histogram",
  "title": "Short descriptive title",
  "x": "column_name_in_result",
  "y": "column_name_in_result or null for histogram",
  "color": "optional grouping column or null",
  "sql": "DuckDB SELECT query to produce the chart data"
}}
```

CRITICAL RULES FOR SQL:
- ALWAYS include a "sql" field with a DuckDB SELECT query that produces exactly the data \
needed for the chart. The x and y column names must match columns returned by the sql query.
- If the current DataFrame already has the right shape, write sql that reproduces it.
- If the user wants a different breakdown (e.g., "by month", "by channel"), write NEW sql \
with the appropriate GROUP BY, DATE_TRUNC, etc. Use the database schema above to write correct queries.
- For time-based charts use DATE_TRUNC('month', column) or similar, and cast to VARCHAR for clean labels.
- Always ORDER BY the x-axis column for clean chart rendering.

OTHER RULES:
- If the user's request is already specific ("show net sales by channel as a bar chart"), \
skip all questions and return chart-spec immediately.
- For pie charts: x = labels/segments, y = values.
- For histogram: only x is needed, set y to null.
- Ask at most 3 questions total before generating the chart.
- Be conversational and brief. One short sentence for your question.
- Always suggest what you'd recommend based on the data shape.
"""


def _build_df_info(df: pl.DataFrame) -> str:
    """Build a concise description of the DataFrame for the system prompt."""
    lines = [f"Row count: {len(df)}", "Columns:"]
    for col in df.columns:
        dtype = str(df[col].dtype)
        try:
            unique_vals = df[col].drop_nulls().unique().sort().head(8).to_list()
        except Exception:
            unique_vals = df[col].drop_nulls().unique().head(8).to_list()
        n_unique = df[col].n_unique()
        sample_str = ", ".join(repr(v) for v in unique_vals[:6])
        if n_unique > 6:
            sample_str += f", ... ({n_unique} unique)"
        lines.append(f"  - {col} ({dtype}): [{sample_str}]")
    return "\n".join(lines)


def parse_chart_spec(text: str) -> dict | None:
    """Extract ```chart-spec``` JSON block from LLM response.

    Returns the parsed dict if found, None otherwise.
    """
    match = re.search(r"```chart-spec\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            spec = json.loads(match.group(1))
            _logger.info("Parsed chart-spec: %s", spec)
            return spec
        except json.JSONDecodeError as exc:
            _logger.warning("chart-spec JSON parse error: %s", exc)
    return None


def clean_response(text: str) -> str:
    """Remove the chart-spec block from the response, leaving only conversational text."""
    cleaned = re.sub(r"```chart-spec\s*\{.*?\}\s*```", "", text, flags=re.DOTALL).strip()
    return cleaned or ""


class ChartAgent:
    """Multi-turn conversational chart builder.

    Uses the Anthropic or OpenAI API directly (no MCP) for multi-turn
    conversation with proper message history.
    """

    def __init__(self) -> None:
        provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

        if provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=os.environ["ANTHROPIC_API_KEY"]
            )
            self._model = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
            self._provider = "anthropic"
        else:
            from openai import OpenAI

            self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            self._model = os.getenv("LLM_MODEL", "gpt-4o")
            self._provider = "openai"

    def respond(
        self,
        df: pl.DataFrame,
        history: list[dict[str, str]],
        original_question: str = "",
        source_sql: str = "",
        db_schema: str = "",
    ) -> str:
        """Send conversation history to LLM and return the next response.

        Args:
            df: The DataFrame to visualize.
            history: List of {"role": "user"|"assistant", "content": str} messages.
            original_question: The original data question that produced the DataFrame.
            source_sql: The SQL query that produced the DataFrame.
            db_schema: Database schema DDL for writing new queries.

        Returns:
            LLM response text — either a clarifying question or contains a
            ```chart-spec``` block.
        """
        df_info = _build_df_info(df)
        system = _SYSTEM_PROMPT.format(
            df_info=df_info,
            original_question=original_question or "Not specified",
            source_sql=source_sql or "Not available",
            db_schema=db_schema or "Not available",
        )

        try:
            if self._provider == "anthropic":
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    temperature=0.0,
                    system=system,
                    messages=history,
                )
                return response.content[0].text
            else:
                messages = [{"role": "system", "content": system}] + history
                response = self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=1024,
                    temperature=0.0,
                    messages=messages,
                )
                return response.choices[0].message.content
        except Exception as exc:
            _logger.exception("ChartAgent error")
            return f"Sorry, I encountered an error: {exc}"
