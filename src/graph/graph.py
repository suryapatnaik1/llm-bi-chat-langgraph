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
router              Classifies user intent into one of four edge labels:
                    "query" | "visualization" | "chart" | "reformat".
                    Replaces OrchestratorAgent + the classify_intent() call
                    in app.py. (STUB — Phase 2)

query_agent         Wraps QueryAgent: text-to-SQL via MCP, returns df_json
                    and last_sql. (STUB — Phase 2)

visualization_agent Wraps VisualizationAgent: multi-SQL dashboard builder,
                    publishes HTML and writes dashboard_url to state.
                    (STUB — Phase 2)

offer_plot          Interrupts after query_agent when a DataFrame is present
                    to ask "Would you like to plot this?". Holds the yes/no
                    answer in state before routing onward. (STUB — Phase 3)

chart_agent         Wraps ChartAgent: multi-turn chart builder. Uses
                    interrupt() to pause and wait for user clarification when
                    it needs more information. (STUB — Phase 3)
"""
from langgraph.graph import END, START, StateGraph

from graph.state import BIState


# ---------------------------------------------------------------------------
# Stub node functions — replaced in Phase 2 / Phase 3
# ---------------------------------------------------------------------------


def router_node(state: BIState) -> dict:
    """Classify user intent. Returns edge label via classify_intent().

    Phase 2: replace with a Claude call that returns one of
    "query" | "visualization" | "chart" | "reformat".
    """
    return {}


def query_agent_node(state: BIState) -> dict:
    """Run QueryAgent: write SQL, execute via MCP, populate df_json / last_sql.

    Phase 2: wire in QueryAgent.run() inside an MCP session.
    """
    return {}


def visualization_agent_node(state: BIState) -> dict:
    """Run VisualizationAgent: build dashboard, publish, write dashboard_url.

    Phase 2: wire in VisualizationAgent.run() + publish step.
    """
    return {}


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
    """Return the edge label chosen by router_node.

    Phase 2: router_node will write the intent into state; read it here.
    Stub returns "query" so the skeleton graph is runnable end-to-end.
    """
    # TODO (Phase 2): return state["intent"]
    return "query"


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

# Compile without a checkpointer for Phase 1 — added in Phase 5
graph = _builder.compile()
