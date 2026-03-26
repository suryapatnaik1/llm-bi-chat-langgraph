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
    ADR-001-checkpointer.md          Why SqliteSaver (dev) / AsyncPostgresSaver (prod)
    ADR-002-async-mcp-bridge.md      Why LangGraph nodes await MCP directly (not sync bridge)
    ADR-003-chart-history-reducer.md chart_history plain field over operator.add
    ADR-004-query-agent-*.md         Full conversation history to QueryAgent
    ADR-005-direct-duckdb-*.md       Shared DuckDB connection over MCP subprocess (Phase 6)
    ADR-006-clickhouse-*.md          ClickHouse migration path (deferred Phase 7)
    ADR-007-postgres-checkpointer.md AsyncPostgresSaver for 100-user production (Phase 6)
    ADR-008-langgraph-server-*.md    LangGraph Server deployment for 100 users (Phase 6)
    ADR-009-model-tier-split.md      haiku for router / sonnet for agents (Phase 6)
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
    chart_history: list = []  # Phase 3: change from operator.add to plain field; reset by query_agent_node
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
| 2 | ⚠️ Incomplete | `router_node`, `query_agent_node`, `visualization_agent_node` — conversation history not yet passed (G6, G7); diagnostic SQL prompt still singular (G8) |
| 2.5 | 🔲 Pre-Phase 3 | Fix G1–G5 (chart_history reducer, BIState fields, ChartAgent async, stale df_json, docstrings) |
| 3 | 🔲 Next | `offer_plot_node` and `chart_agent_node` using `interrupt()`; MemorySaver added |
| 4 | 🔲 Pending | Update `app.py` to call `graph.invoke()` instead of orchestrator |
| 5 | 🔲 Pending | Add `AsyncSqliteSaver` (dev) / `AsyncPostgresSaver` (prod) checkpointer; `thread_id` per session |
| 6 | 🔲 Pending | Scale to 100 users: shared DuckDB connection, `AsyncPostgresSaver`, LangGraph Server (G9 resolved) |
| 7 | 🔲 Deferred | ClickHouse migration (trigger: data > 50 GB or p95 latency > 5 s) |

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

**Conversation history (pending fix):**
`query_agent_node` currently passes only `state["messages"][-1].content` to
`QueryAgent`. This means drill-down and refinement patterns ("same but for
wholesale", "break that down by country") silently fail — the agent has no
context for what "same" refers to. Fix: pass full `state["messages"]` so
QueryAgent can reference prior answers in the conversation.

**`dashboard_url` vs `dashboard_html`:**
- `VisualizationAgent.run()` saves the HTML to disk internally and returns
  `dashboard_html` in `AgentResult`
- `visualization_agent_node` calls `save_report()` a second time to get the URL
- This saves the file twice — known inefficiency, tracked as TODO in the code
- Phase 5 will replace `save_report()` with a real publish step (intranet / Power BI)

### Phase 3 — What Needs to Happen

**Pre-requisites (must land before any Phase 3 code):**
- Fix G1: change `chart_history` to plain `list` in `state.py`; update docstring
- Fix G2: add `plot_accepted: str = ""`, `original_question: str = ""`, `db_schema: str = ""` to `BIState`
- Fix G3: make ChartAgent async-safe (`AsyncAnthropic` or `run_in_executor`)
- Fix G4: `query_agent_node` resets `df_json = None`, `last_sql = None` at start

**`offer_plot_node`**: use `interrupt("Would you like to plot this data?")`, parse
yes/no into `state["plot_accepted"]`, update `offer_plot_answer()` to read it.

**`chart_agent_node`**: call `ChartAgent.respond()` (no MCP) with df
deserialized from `df_json`, history from `chart_history`, `original_question`,
`last_sql`, and `db_schema` from state. Use `interrupt()` for clarifying
questions. Returns complete updated `chart_history` list on every run. Must
detect `intent == "reformat"` and skip `offer_plot` re-ask (G11).

**Checkpointer required for interrupt():** compile graph with `MemorySaver` in
Phase 3; Phase 5 swaps it for `AsyncSqliteSaver` / `AsyncPostgresSaver`.

### Phase 4 — app.py Changes

- Replace `orchestrator.query()` calls with `graph.invoke()`
- Replace reads of `result.data`, `result.text`, `result.dashboard_html` with
  reads from `BIState` fields
- Add filter mismatch detection: compare current sidebar value against
  `state["filter_context"]` from checkpoint before each invoke; if different,
  prompt user to choose which filters to use
- `OrchestratorAgent` and `AgentRegistry` can be deleted after this phase

### Phase 5 — Persistence

- Add `AsyncSqliteSaver` for development / `AsyncPostgresSaver` for production (see ADR-001 amendment)
- Generate a stable `thread_id` per Streamlit session (e.g. `st.session_state`)
- Pass `{"configurable": {"thread_id": thread_id}}` to every `graph.invoke()`
- Conversations then survive app restarts
- Required env var for production: `CHECKPOINT_DB_URL` (PostgreSQL connection string)

### Phase 6 — Scale to 100 users

Four changes, each with a dedicated ADR:

1. **Shared DuckDB connection** ([ADR-005](docs/adr/ADR-005-direct-duckdb-connection.md)):
   Create `src/services/duckdb_connection.py` with `get_connection()`, `get_schema()`,
   `execute_sql()`; remove `execute_with_session` wrappers from node functions;
   rename `context["mcp_tools"]` to `context["db"]` in both agent files.

2. **`AsyncPostgresSaver`** ([ADR-007](docs/adr/ADR-007-postgres-checkpointer.md)):
   Replaces `AsyncSqliteSaver` in production (`CHECKPOINT_DB_URL` env var set);
   dev falls back to `AsyncSqliteSaver` when env var is absent.
   Add `langgraph-checkpoint-postgres` + `asyncpg` to `pyproject.toml`.

3. **LangGraph Server** ([ADR-008](docs/adr/ADR-008-langgraph-server-deployment.md)):
   Self-hosted Docker deployment replaces Streamlit single-process model.
   `app.py` calls `client.runs.stream()` via `langgraph_sdk` instead of
   `graph.ainvoke()`. Add `docker-compose.yml` with server + Streamlit + PostgreSQL.
   `MCPConnectionManager` can be deleted after Phase 4 removes the Streamlit direct path.

4. **Model tier split** ([ADR-009](docs/adr/ADR-009-model-tier-split.md)):
   Add `ROUTER_MODEL` and `AGENT_MODEL` constants to `config.py`.
   `router_node` uses `ROUTER_MODEL` (`claude-haiku-4-5`); agent nodes keep
   `AGENT_MODEL` (`claude-sonnet-4-6`). Run shadow comparison before enabling
   in production (log disagreements between haiku and sonnet classifications).

### Phase 7 — ClickHouse migration (deferred)

Trigger-based — do not start unless: data > 50 GB, p95 query latency > 5 s,
or horizontal app scaling is required.

- Replace `src/services/duckdb_connection.py` with `clickhouse-connect` client
- Update `QueryAgent` and `VisualizationAgent` system prompts to ClickHouse SQL dialect
- Rewrite `scripts/prepare_bi_data.py` with `MergeTree` DDL
- Run full interaction taxonomy test (6 types × 6 patterns) against both databases
  in parallel before cutover
- See ADR-006 for dialect gap table and validation approach

---
                  
## Open Implementation Gaps

Gaps are grouped by phase gate — the phase at which each must be fixed to avoid
blocking progress or shipping broken behaviour.

---

### 🔴 Must fix before Phase 3 starts

**G1 — `chart_history` reducer not fixed (code contradicts ADR-003):**
`state.py:53` still has `Annotated[list, operator.add]`. The class docstring
still reads "Uses an append reducer so turns are never overwritten" — the
exact rationale ADR-003 rejected. Phase 3 will behave incorrectly from line 1.
Fix: remove `Annotated[list, operator.add]`; change to plain `list`; update
docstring; `query_agent_node` resets `chart_history = []` when writing `df_json`.

**G2 — BIState missing three Phase 3 fields:**
`plot_accepted`, `original_question`, and `db_schema` are required by
`offer_plot_node` and `chart_agent_node` but absent from `state.py`.
Phase 3 cannot be implemented without adding them first.

**G3 — ChartAgent uses sync Anthropic client — will deadlock LangGraph event loop:**
`chart_agent.py:126` — `self._client = anthropic.Anthropic(...)` (sync).
When `chart_agent_node` calls `ChartAgent.respond()` from inside an async
LangGraph node, the blocking HTTP call freezes the event loop — the same
deadlock ADR-002 identified for MCP's sync bridge. Fix before Phase 3:
either switch ChartAgent to `AsyncAnthropic`, or wrap `respond()` in
`asyncio.run_in_executor()`.

---

### 🟠 Fix before Phase 3 completes

**G4 — Stale `df_json` from previous query survives into current turn:**
`has_dataframe()` returns `"yes"` if any `df_json` is in state, including
from a prior turn. If a query returns 0 rows, `query_agent_node` doesn't set
`df_json` (correct) but also doesn't _clear_ the old one. Users then see
"Would you like to plot this?" after a query that produced no data, and the
chart would use stale data. Fix: `query_agent_node` resets `df_json = None`
and `last_sql = None` at the start of each invocation.

**G5 — `state.py` docstring contradicts ADR-003:**
`chart_history` docstring still describes the append reducer as correct
behaviour. Update it to say "plain field — reset by `query_agent_node`; see
ADR-003."

---

### 🟡 Phase 2 incomplete — fix before calling Phase 2 done

**G6 — QueryAgent conversation memory not implemented (ADR-004 pending):**
`query_agent_node` passes only `state["messages"][-1].content` to `QueryAgent`.
Drill-down and refinement patterns silently fail.
Fix documented in ADR-004 — implementation not yet done.

**G7 — VisualizationAgent has same single-message gap (undocumented):**
`visualization_agent_node:153` — same as G6 but for `VisualizationAgent`.
Pattern E (Report Assembly — "add breakdown... add trend... show as dashboard")
requires prior context to accumulate correctly. Not mentioned in ADR-004.
Fix: extend ADR-004 to cover `visualization_agent_node`.

**G8 — QueryAgent prompt allows only one SQL call:**
System prompt says "Write a precise DuckDB SELECT query" (singular). Diagnostic
questions need 3–4 decomposition queries. `run_tool_loop` supports multiple
tool calls — it's a prompt-only change.
Tracked as CLAUDE.md gap #2 but has no ADR. Needs ADR-010.

---

### 🟡 Document before Phase 6 starts

**G9 — ADR-005 underestimates `run_tool_loop` migration scope:**
`run_tool_loop` in `base.py` takes a `ClientSession` and calls
`session.call_tool()` for every tool invocation. Direct DuckDB callables
cannot be passed as a `ClientSession`. ADR-005 says "rename `context["mcp_tools"]`
to `context["db"]`" but that doesn't change how `run_tool_loop` executes tools.
The full migration requires one of: (a) refactor `run_tool_loop` to accept a
callable dict, (b) thin `ClientSession` adapter over direct DuckDB, or
(c) two separate tool loops. This design decision must be documented as an
addendum to ADR-005.

**G10 — ADR-001 amendment and ADR-007 are substantively duplicated:**
Both documents describe the same decision (PostgreSQL checkpointer for 100
users). ADR-001 amendment should be shortened to "see ADR-007" to avoid the
two documents drifting apart.

---

### 🟢 Fix before production launch

**G11 — `reformat` intent routing (Phase 3 gap):**
When `intent == "reformat"`, the graph routes to `chart_agent_node` directly.
That node must detect this case and use existing `df_json` + `last_sql` without
re-asking "would you like to plot this?".

**G12 — Model IDs in code predate ADR-009:**
`config.py:10` — `LLM_MODEL = "claude-sonnet-4-20250514"` (old ID).
ADR-009 proposes separate `ROUTER_MODEL` and `AGENT_MODEL` constants.
`chart_agent.py:129` reads `os.getenv("LLM_MODEL")` directly instead of
importing from `config.py`. Phase 6 implementation must wire these up.

**G13 — `get_async_client()` is not cached (docstring is wrong):**
`config.py:19` docstring says "Return a cached AsyncAnthropic client" but
creates a new `AsyncAnthropic()` instance on every call. Under 100 concurrent
users each agent call creates a new HTTP client. Fix: module-level singleton
or `functools.lru_cache`.

**G14 — No `ANTHROPIC_API_KEY` startup validation:**
If the env var is missing, all Claude calls fail at runtime, not at startup.
Add a check in `config.py` that raises `RuntimeError` if the key is empty.

**G15 — `langgraph` version pin too loose (`>=0.2`):**
A major version bump (1.x → 2.x) won't be caught. Pin to `>=1.0,<2.0` or the
current known-good minor version.

**G16 — No `RetryPolicy` on any node:**
README describes node-level retry as a LangGraph benefit but no node uses it.
Add `retry=RetryPolicy(max_attempts=3)` to at least `query_agent_node` and
`visualization_agent_node` to handle transient MCP/API errors.

**G17 — Dashboard publish target not validated at startup (Phase 5 gap):**
`visualization_agent_node` calls `save_report()` (writes to disk). Phase 5
replaces this with a remote publish step. Required env vars (intranet URL,
Power BI workspace ID, bearer token) must be validated at startup, not at
publish time.

---

### 🔵 Future scope (not blocking current phases)

**G18 — Dashboard drill-down:** No graph path for interrogating a specific KPI
after a dashboard is generated. Requires a new node and edge. Deferred.

**G19 — MCP subprocess bottleneck:** Addressed in Phase 6 (ADR-005). Current
async approach is correct in the interim.

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
