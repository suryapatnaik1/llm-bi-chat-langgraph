"""VisualizationAgent: standard BI report generation specialist."""
import logging
from typing import Any

from mcp import ClientSession

from agents.base import AgentResult, BaseAgent, run_tool_loop
from config import LLM_MODEL, get_async_client
from services.dashboard_renderer import (
    parse_dashboard_response,
    render_dashboard,
    save_report,
)

_logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a business intelligence analyst with direct access to a DuckDB database.

Database schema:
{schema}

When answering questions about the data:
1. Run as many execute_sql calls as needed to gather all data for the report.
2. Return the results as a single ```dashboard-data JSON block (no HTML, no extra text before it).

The JSON must follow this exact schema:
```dashboard-data
{{
  "title": "Dashboard title",
  "subtitle": "One-sentence description or date range",
  "kpis": [
    {{"label": "Metric name", "value": "£12,345", "change": "+5.2%", "up": true}}
  ],
  "charts": [
    {{
      "title": "Chart title",
      "type": "bar",
      "x_label": "X axis",
      "y_label": "Y axis",
      "data": {{
        "labels": ["A", "B"],
        "datasets": [{{"label": "Series", "data": [100, 200]}}]
      }}
    }},
    {{
      "title": "Mix chart",
      "type": "pie",
      "data": {{
        "labels": ["A", "B"],
        "datasets": [{{"data": [60, 40]}}]
      }}
    }},
    {{
      "title": "Trend chart",
      "type": "line",
      "x_label": "Month",
      "y_label": "Value",
      "data": {{
        "labels": ["Jan", "Feb"],
        "datasets": [{{"label": "Series", "data": [100, 120]}}]
      }}
    }}
  ]
}}
```

Rules:
- Include 3-5 KPI cards and 3-5 charts (bar, pie/doughnut, line as appropriate).
- After the ```dashboard-data block write one short plain-text summary sentence.
- Do NOT generate any HTML. Do not make up data.
"""


class VisualizationAgent(BaseAgent):
    """Creates standard BI reports with KPI cards and charts."""

    @property
    def name(self) -> str:
        return "visualization_agent"

    @property
    def description(self) -> str:
        return (
            "Create full BI reports/dashboards with KPI cards and multiple charts. "
            "Use when the user asks for dashboards, reports, overviews, or insights."
        )

    async def run(
        self,
        question: str,
        session: ClientSession,
        schema: str,
        context: dict[str, Any] | None = None,
    ) -> AgentResult:
        client = get_async_client()
        system_prompt = _SYSTEM_PROMPT.format(schema=schema)

        user_message = question
        if context and context.get("filter_context"):
            user_message = f"{context['filter_context']}\n\n{question}"

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message}
        ]

        tools = context.get("mcp_tools", []) if context else []

        text, _last_sql = await run_tool_loop(
            client=client,
            session=session,
            model=LLM_MODEL,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=4096,
        )

        dash_data, summary = parse_dashboard_response(text)

        if dash_data:
            html = render_dashboard(dash_data)
            save_report(html)
            return AgentResult(
                text=summary or dash_data.get("title", "Report generated."),
                dashboard_html=html,
                agent_name=self.name,
            )

        return AgentResult(text=text, agent_name=self.name)
