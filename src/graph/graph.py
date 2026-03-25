"""LangGraph StateGraph for the BI Chat application.

Graph topology
--------------

    START
      │
      ▼
   router  ──────────────────────────────────────────┐
      │                                              │
  ┌───┴────────────────┐                    "reformat" / "chart"
  ▼                    ▼                             │
query_agent    visualization_agent                   ▼
  │                    │                        chart_agent
  │ (has df?)          │                             │
  ▼                    ▼                             ▼
offer_plot ──────► chart_agent                      END
  │ (no df / user                    (may interrupt() to ask
  │  declines)                        clarifying questions)
  ▼
 END

Nodes
-----
router              Calls Claude with a brief system prompt and returns one
                    of four edge labels: "query" | "visualization" | "chart"
                    | "reformat". Replaces OrchestratorAgent + the
                    classify_intent() call in app.py.

query_agent         Wraps QueryAgent: text-to-SQL via MCP, returns df_json
                    and last_sql.

visualization_agent Wraps VisualizationAgent: multi-SQL dashboard builder,
                    saves HTML report to disk, writes dashboard_url to state.
                    TODO (Phase 5): replace save_report() with publish to
                    intranet / Power BI and write the remote URL instead.

offer_plot          Interrupts after query_agent when a DataFrame is present
                    to ask "Would you like to plot this?". Holds the yes/no
                    answer in state before routing onward. (STUB — Phase 3)

chart_agent         Wraps ChartAgent: multi-turn chart builder. Uses
                    interrupt() to pause and wait for user clarification when
                    it needs more information. (STUB — Phase 3)
"""
import logging

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from agents.query_agent import QueryAgent
from agents.visualization_agent import VisualizationAgent
from config import LLM_MODEL, get_async_client
from graph.state import BIState
from services.mcp_connection import MCPConnectionManager

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons — created once per process
# ---------------------------------------------------------------------------

_mcp = MCPConnectionManager()
_query_agent = QueryAgent()
_visualization_agent = VisualizationAgent()

# ---------------------------------------------------------------------------
# Router prompt
# ---------------------------------------------------------------------------

_ROUTER_SYSTEM = """\
You are an intent classifier for a BI chat application.

Classify the user's message into exactly one of these categories:
- query: The user wants to fetch or analyse specific data (most common).
- visualization: The user wants a full dashboard with multiple KPI cards and charts.
- chart: The user explicitly wants a single chart or visualisation from scratch.
- reformat: The user wants to change how existing query results are displayed \
(only valid when a DataFrame from a previous query is already available).

Reply with ONLY the category name — no punctuation, no explanation.
"""


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------


async def router_node(state: BIState) -> dict:
    """Classify user intent via a lightweight Claude call.

    Writes intent into state["intent"]; classify_intent() reads it to pick
    the next node.
    """
    last_message = state["messages"][-1].content
    has_df = bool(state.get("df_json"))

    user_content = last_message
    if has_df:
        user_content = (
            "[A DataFrame from a previous query is available.]\n\n" + last_message
        )

    client = get_async_client()
    response = await client.messages.create(
        model=LLM_MODEL,
        max_tokens=10,
        system=_ROUTER_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = response.content[0].text.strip().lower()
    valid = {"query", "visualization", "chart", "reformat"}
    intent = raw if raw in valid else "query"
    # "reformat" only makes sense when there is an existing DataFrame
    if intent == "reformat" and not has_df:
        intent = "query"

    _logger.info("router_node: intent=%r", intent)
    return {"intent": intent}


async def query_agent_node(state: BIState) -> dict:
    """Run QueryAgent: write SQL, execute via MCP, populate df_json / last_sql."""
    question = state["messages"][-1].content
    filter_context = state.get("filter_context", "")

    async def _run(session, schema):
        tools = await _mcp.get_mcp_tools(session)
        return await _query_agent.run(
            question=question,
            session=session,
            schema=schema,
            context={"filter_context": filter_context, "mcp_tools": tools},
        )

    result = await _mcp.execute_with_session(_run)

    updates: dict = {"messages": [AIMessage(content=result.text)]}
    if result.data is not None:
        updates["df_json"] = result.data.write_json()
    if result.last_sql:
        updates["last_sql"] = result.last_sql
    return updates


async def visualization_agent_node(state: BIState) -> dict:
    """Run VisualizationAgent: build dashboard, save to disk, write dashboard_url.

    Phase 5: replace save_report() with a real publish step (intranet /
    Power BI) and store the remote URL in dashboard_url instead.
    """
    question = state["messages"][-1].content
    filter_context = state.get("filter_context", "")

    async def _run(session, schema):
        tools = await _mcp.get_mcp_tools(session)
        return await _visualization_agent.run(
            question=question,
            session=session,
            schema=schema,
            context={"filter_context": filter_context, "mcp_tools": tools},
        )

    result = await _mcp.execute_with_session(_run)

    updates: dict = {"messages": [AIMessage(content=result.text)]}
    if result.dashboard_html:
        # VisualizationAgent already calls save_report() internally; we call
        # it again here to get the URL path for state.  TODO (Phase 5): publish
        # to remote target and store the remote URL instead.
        from services.dashboard_renderer import save_report

        updates["dashboard_url"] = save_report(result.dashboard_html)
    return updates


def offer_plot_node(state: BIState) -> dict:
    """Interrupt and ask the user if they want to plot the query results.

    Phase 3: replace stub with interrupt("Would you like to plot this data?")
    and parse the yes/no answer into state.
    """
    return {}


def chart_agent_node(state: BIState) -> dict:
    """Run ChartAgent: multi-turn chart builder with interrupt() for clarification.

    Phase 3: wire in ChartAgent.respond() with interrupt() for mid-conversation
    pauses and chart_history accumulation.
    """
    return {}


# ---------------------------------------------------------------------------
# Edge condition functions
# ---------------------------------------------------------------------------


def classify_intent(state: BIState) -> str:
    """Return the edge label written by router_node."""
    return state.get("intent") or "query"


def has_dataframe(state: BIState) -> str:
    """Route to offer_plot if query_agent produced a DataFrame, else END."""
    return "yes" if state.get("df_json") else "no"


def offer_plot_answer(state: BIState) -> str:
    """Route to chart_agent if the user accepted the plot offer, else END.

    Phase 3: offer_plot_node will write the user's yes/no answer into state.
    Stub routes to END so the skeleton is runnable.
    """
    # TODO (Phase 3): return state.get("plot_accepted", "no")
    return "no"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

_builder = StateGraph(BIState)

_builder.add_node("router", router_node)
_builder.add_node("query_agent", query_agent_node)
_builder.add_node("visualization_agent", visualization_agent_node)
_builder.add_node("offer_plot", offer_plot_node)
_builder.add_node("chart_agent", chart_agent_node)

_builder.add_edge(START, "router")

_builder.add_conditional_edges(
    "router",
    classify_intent,
    {
        "query": "query_agent",
        "visualization": "visualization_agent",
        "chart": "chart_agent",
        "reformat": "chart_agent",
    },
)

_builder.add_conditional_edges(
    "query_agent",
    has_dataframe,
    {
        "yes": "offer_plot",
        "no": END,
    },
)

_builder.add_conditional_edges(
    "offer_plot",
    offer_plot_answer,
    {
        "yes": "chart_agent",
        "no": END,
    },
)

_builder.add_edge("visualization_agent", END)
_builder.add_edge("chart_agent", END)

# Compile without a checkpointer for Phase 1–4 — added in Phase 5
graph = _builder.compile()
