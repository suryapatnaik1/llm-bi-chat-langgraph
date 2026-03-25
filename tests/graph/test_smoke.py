"""Smoke tests for the LangGraph graph module.

Three layers:
1. Structure  — import, compile, node names
2. Edge conditions — pure functions, no mocking required
3. Routing    — end-to-end invocation with mocked nodes (no Claude / MCP)
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from graph.graph import (
    chart_agent_node,
    classify_intent,
    has_dataframe,
    offer_plot_answer,
    offer_plot_node,
)
from graph.state import BIState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s(**overrides) -> dict:
    """Minimal BIState-compatible dict for edge-condition tests."""
    base: dict = {
        "messages": [],
        "intent": "query",
        "filter_context": "",
        "df_json": None,
        "last_sql": None,
        "chart_spec": None,
        "dashboard_url": None,
        "chart_history": [],
    }
    return {**base, **overrides}


def _build_graph(router_fn, query_fn=None, viz_fn=None):
    """Compile a graph using production edge functions and provided node stubs."""

    async def _noop(state):
        return {}

    builder = StateGraph(BIState)
    builder.add_node("router", router_fn)
    builder.add_node("query_agent", query_fn or _noop)
    builder.add_node("visualization_agent", viz_fn or _noop)
    builder.add_node("offer_plot", offer_plot_node)
    builder.add_node("chart_agent", chart_agent_node)

    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        classify_intent,
        {
            "query": "query_agent",
            "visualization": "visualization_agent",
            "chart": "chart_agent",
            "reformat": "chart_agent",
        },
    )
    builder.add_conditional_edges(
        "query_agent", has_dataframe, {"yes": "offer_plot", "no": END}
    )
    builder.add_conditional_edges(
        "offer_plot", offer_plot_answer, {"yes": "chart_agent", "no": END}
    )
    builder.add_edge("visualization_agent", END)
    builder.add_edge("chart_agent", END)
    return builder.compile()


# ---------------------------------------------------------------------------
# 1. Structure
# ---------------------------------------------------------------------------


def test_graph_imports_and_compiles():
    from graph.graph import graph

    assert graph is not None


def test_graph_has_expected_nodes():
    from graph.graph import graph

    assert set(graph.nodes.keys()) == {
        "__start__",
        "router",
        "query_agent",
        "visualization_agent",
        "offer_plot",
        "chart_agent",
    }


# ---------------------------------------------------------------------------
# 2. Edge conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intent", ["query", "visualization", "chart", "reformat"])
def test_classify_intent_all_labels(intent):
    assert classify_intent(_s(intent=intent)) == intent


def test_classify_intent_defaults_to_query_when_empty():
    assert classify_intent(_s(intent="")) == "query"


def test_classify_intent_defaults_to_query_when_missing():
    state = _s()
    del state["intent"]
    assert classify_intent(state) == "query"


def test_has_dataframe_yes():
    assert has_dataframe(_s(df_json='{"a":[1]}')) == "yes"


def test_has_dataframe_no_when_none():
    assert has_dataframe(_s(df_json=None)) == "no"


def test_has_dataframe_no_when_empty_string():
    assert has_dataframe(_s(df_json="")) == "no"


def test_offer_plot_answer_stub_returns_no():
    assert offer_plot_answer(_s()) == "no"


# ---------------------------------------------------------------------------
# 3. Routing (mocked nodes — no Claude / MCP calls)
# ---------------------------------------------------------------------------


def test_query_intent_no_df_ends_at_query_agent():
    """Router → query_agent (no df) → END."""

    async def _router(state):
        return {"intent": "query"}

    async def _query(state):
        return {"messages": [AIMessage(content="42 orders placed.")]}

    result = asyncio.run(
        _build_graph(_router, query_fn=_query).ainvoke(
            {"messages": [HumanMessage(content="How many orders?")], "filter_context": ""}
        )
    )

    assert result["intent"] == "query"
    assert result["messages"][-1].content == "42 orders placed."
    assert result.get("df_json") is None


def test_query_intent_with_df_passes_through_offer_plot():
    """Router → query_agent (df present) → offer_plot (stub) → END."""

    async def _router(state):
        return {"intent": "query"}

    async def _query(state):
        return {
            "messages": [AIMessage(content="Top 5 orders.")],
            "df_json": '{"ref":["A","B"],"sales":[100,200]}',
            "last_sql": "SELECT * FROM orders LIMIT 5",
        }

    result = asyncio.run(
        _build_graph(_router, query_fn=_query).ainvoke(
            {
                "messages": [HumanMessage(content="Show top 5 orders")],
                "filter_context": "date_range=2024",
            }
        )
    )

    assert result["df_json"] == '{"ref":["A","B"],"sales":[100,200]}'
    assert result["last_sql"] == "SELECT * FROM orders LIMIT 5"


def test_visualization_intent_sets_dashboard_url():
    """Router → visualization_agent → END with dashboard_url in state."""

    async def _router(state):
        return {"intent": "visualization"}

    async def _viz(state):
        return {
            "messages": [AIMessage(content="Sales dashboard for Q1.")],
            "dashboard_url": "/app/static/reports/report_12345.html",
        }

    result = asyncio.run(
        _build_graph(_router, viz_fn=_viz).ainvoke(
            {
                "messages": [HumanMessage(content="Give me a Q1 sales dashboard")],
                "filter_context": "",
            }
        )
    )

    assert result["intent"] == "visualization"
    assert result["dashboard_url"] == "/app/static/reports/report_12345.html"
    assert result["messages"][-1].content == "Sales dashboard for Q1."
