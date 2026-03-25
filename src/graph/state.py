"""BIState — single source of truth for the LangGraph state machine."""
import operator
from typing import Annotated

from langgraph.graph import MessagesState


class BIState(MessagesState):
    """Everything the graph needs — one place, typed, checkpointed.

    Fields
    ------
    filter_context:
        Sidebar filters (date range, channels) active when the user submitted
        the question. Written by the UI on every new question and preserved
        across interrupt() resumes so that chart SQL stays consistent with
        the data already shown to the user.

        The UI compares the current sidebar value against this field before
        each graph.invoke(); if they differ mid-conversation it prompts the
        user to choose between original and new filters before proceeding.

    df_json:
        Serialised Polars DataFrame (write_json) from the last QueryAgent run.
        Used by ChartAgent to understand the data shape without re-querying.

    last_sql:
        The SQL query that produced df_json. Passed to ChartAgent so it can
        write new queries that are consistent with the original data pull.

    chart_spec:
        Parsed chart-spec dict returned by ChartAgent when it has enough
        information to render a chart (chart_type, x, y, sql, etc.).

    dashboard_url:
        URL of the published dashboard (intranet / Power BI). The
        VisualizationAgent renders HTML, publishes it to the target system,
        and stores only the reference here — never the raw HTML — to keep
        checkpoint size small.

    chart_history:
        Accumulated message turns for the current ChartAgent conversation
        (user clarification questions and agent responses). Uses an append
        reducer so turns are never overwritten by a partial state update.
    """

    filter_context: str = ""
    df_json: str | None = None
    last_sql: str | None = None
    chart_spec: dict | None = None
    dashboard_url: str | None = None
    chart_history: Annotated[list, operator.add] = []
