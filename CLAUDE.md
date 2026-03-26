# CLAUDE.md — Project Context for AI Sessions

This file is loaded automatically at the start of every Claude Code session.
Read it fully before making any changes to this codebase.

---

## What This Project Is

A conversational Business Intelligence app being migrated from a hand-built
orchestrator (`llm-bi-chat-agentic`) to **LangGraph**. Users ask questions
about order data in plain English. Agents write SQL, execute queries via MCP,
and generate interactive charts and dashboards.

**Migration goal:** replace the custom `if/elif` orchestration in `app.py`
with a LangGraph `StateGraph` while keeping all existing agent capabilities.

---

## Repository Layout

```
src/
  app.py                        Streamlit UI — NOT yet updated (Phase 4)
  config.py                     Env vars, paths, get_async_client()
  agents/
    base.py                     AgentResult dataclass + run_tool_loop()
    query_agent.py              Text-to-SQL via MCP
    visualization_agent.py      Multi-SQL dashboard builder
    chart_agent.py              Multi-turn conversational chart builder
    orchestrator.py             OLD orchestrator — will be deleted in Phase 4
    registry.py                 OLD agent registry — will be deleted in Phase 4
  services/
    mcp_connection.py           MCPConnectionManager (async event loop bridge)
    dashboard_renderer.py       parse_dashboard_response / render_dashboard / save_report
  sql/
    duckdb_mcp_server.py        MCP server exposing get_schema + execute_sql tools
  graph/
    state.py                    BIState — single source of truth for graph state
    graph.py                    StateGraph definition — THE main migration file
docs/
  adr/
    ADR-001-checkpointer.md     Why SqliteSaver over DuckDB / PostgreSQL
tests/
  graph/
    test_smoke.py               15 smoke tests (structure, edge conditions, routing)
```

---

## BIState — The State Object

```python
class BIState(MessagesState):
    intent: str = ""            # set by router_node; read by classify_intent()
    filter_context: str = ""    # sidebar filters active when question was asked
    df_json: str | None = None  # Polars DataFrame.write_json() from last QueryAgent run
    last_sql: str | None = None # SQL that produced df_json
    chart_spec: dict | None = None
    dashboard_url: str | None = None   # URL of saved/published report
    chart_history: Annotated[list, operator.add] = []  # append reducer
```

`filter_context` is written by the UI on every question and preserved across
`interrupt()` resumes so chart SQL stays consistent with the original data.

---

## Graph Topology

```
START → router → query_agent → offer_plot → chart_agent → END
                                    ↓ (no df)
               → visualization_agent → END
               → chart_agent → END          (intent = "chart" | "reformat")
```

Edge conditions (pure functions, easy to test):
- `classify_intent(state)` → reads `state["intent"]`; defaults to `"query"`
- `has_dataframe(state)` → `"yes"` if `df_json` present, else `"no"`
- `offer_plot_answer(state)` → stub returns `"no"` until Phase 3

---

## Migration Phases

| Phase | Status | What it does |
|-------|--------|-------------|
| 1 | ✅ Done | `BIState`, skeleton `StateGraph`, `langgraph` dependency |
| 2 | ✅ Done | `router_node`, `query_agent_node`, `visualization_agent_node` implemented |
| 3 | 🔲 Next | `offer_plot_node` and `chart_agent_node` using `interrupt()` |
| 4 | 🔲 Pending | Update `app.py` to call `graph.invoke()` instead of orchestrator |
| 5 | 🔲 Pending | Add `SqliteSaver` checkpointer; `thread_id` per Streamlit session |

---

## Key Implementation Details

### Phase 2 — Router and Agent Nodes

**Router node** (`router_node` in `graph.py`):
- Makes a single Claude call with a 4-label classifier prompt
- Returns `{"intent": "query" | "visualization" | "chart" | "reformat"}`
- Guards `reformat`: falls back to `"query"` if no `df_json` in state

**MCP async bridge:**
- `MCPConnectionManager` spawns a background event loop thread for sync Streamlit
- LangGraph nodes are `async`, so they `await _mcp.execute_with_session(fn)` directly
  (no sync `.run()` wrapper needed)
- `_mcp`, `_query_agent`, `_visualization_agent` are module-level singletons in `graph.py`

**MCP tools must be fetched and passed in context:**
```python
async def _run(session, schema):
    tools = await _mcp.get_mcp_tools(session)
    return await _query_agent.run(..., context={"filter_context": ..., "mcp_tools": tools})
```
Both `QueryAgent` and `VisualizationAgent` read `context["mcp_tools"]` — if this
is missing they get an empty tool list and will fail silently.

**`dashboard_url` vs `dashboard_html`:**
- `VisualizationAgent.run()` saves the HTML to disk internally and returns
  `dashboard_html` in `AgentResult`
- `visualization_agent_node` calls `save_report()` a second time to get the URL
- This saves the file twice — known inefficiency, tracked as TODO in the code
- Phase 5 will replace `save_report()` with a real publish step (intranet / Power BI)

### Phase 3 — What Needs to Happen

`offer_plot_node`: use `interrupt("Would you like to plot this data?")`, parse
yes/no into `state["plot_accepted"]`, update `offer_plot_answer()` to read it.

`chart_agent_node`: call `ChartAgent.respond()` with the DataFrame from state,
use `interrupt()` for mid-conversation pauses, accumulate turns into
`chart_history` (the `operator.add` reducer handles appending automatically).

### Phase 4 — app.py Changes

- Replace `orchestrator.query()` calls with `graph.invoke()`
- Replace reads of `result.data`, `result.text`, `result.dashboard_html` with
  reads from `BIState` fields
- Add filter mismatch detection: compare current sidebar value against
  `state["filter_context"]` from checkpoint before each invoke; if different,
  prompt user to choose which filters to use
- `OrchestratorAgent` and `AgentRegistry` can be deleted after this phase

### Phase 5 — Persistence

- Add `SqliteSaver` (see ADR-001)
- Generate a stable `thread_id` per Streamlit session (e.g. `st.session_state`)
- Pass `{"configurable": {"thread_id": thread_id}}` to every `graph.invoke()`
- Conversations then survive app restarts

---

## Open Implementation Gaps

1. **MCP + async (design complete, not yet stress-tested):** The node functions
   `await _mcp.execute_with_session()` directly in LangGraph's event loop.
   The MCPConnectionManager's background thread loop is idle during LangGraph
   execution — it's only used by the legacy Streamlit path in `app.py`.

2. **Dashboard publish target (Phase 5 gap):** `visualization_agent_node`
   currently calls `save_report()` (writes to `src/static/reports/`).
   The real publish target (intranet base URL, Power BI workspace/dataset ID,
   bearer token) must come from env vars. Raise a clear error at startup if
   required vars are missing.

3. **`reformat` intent (Phase 3 gap):** When `intent == "reformat"`, the graph
   routes to `chart_agent_node`. That node needs to detect the reformat case
   (vs a fresh chart request) and pass the existing `df_json` + `last_sql`
   accordingly.

---

## Running Tests

```bash
poetry run pytest                          # all tests
poetry run pytest tests/graph/test_smoke.py -v   # graph tests only
```

Tests use `asyncio.run(graph.ainvoke(...))` for routing tests because the mock
nodes are `async`. LangGraph's sync `.invoke()` only works with sync node
functions.

---

## Running the App (current — pre-Phase 4)

```bash
poetry run streamlit run src/app.py
```

The app still uses the old orchestrator. The graph is not yet wired into the UI.

---

## Environment

- Python 3.11 (managed via `poetry env use python3.11`)
- Key env var: `ANTHROPIC_API_KEY` in `.env` at repo root
- DuckDB database: `local_data/bi.db` (created by `scripts/prepare_bi_data.py`)
- LangGraph: `1.1.3`
