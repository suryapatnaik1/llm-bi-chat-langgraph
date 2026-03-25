# LLM BI Chat — LangGraph Architecture

A conversational Business Intelligence app being migrated from a hand-built orchestrator to **LangGraph** — a framework for building stateful, multi-agent AI applications. Users ask questions about order data in plain English. Agents write SQL, execute queries, and generate interactive charts and dashboards.

> **Starting point:** [`llm-bi-chat-agentic`](https://github.com/suryapatnaik1/llm-bi-chat-agentic) — the working version with a custom orchestrator.
> **Goal:** Replace the custom orchestration layer with LangGraph's `StateGraph` while keeping all agent capabilities.

---

## Why LangGraph?

### The Restaurant Analogy

Imagine your BI app is a restaurant:

| Restaurant | This App |
|------------|----------|
| Customer at the door | User's question in chat |
| Host who directs customers | OrchestratorAgent (routes to the right agent) |
| Waiter who looks things up | QueryAgent (writes and runs SQL) |
| Artist who draws the menu | VisualizationAgent (generates dashboards) |
| Chef who customises a dish | ChartAgent (asks clarifying questions, builds chart) |
| Kitchen | DuckDB database (via MCP) |
| Post-it notes on the wall | `st.session_state` (current state management) |
| Head restaurant manager | **LangGraph** (the new part) |

Right now, **the code plays the role of manager, host, AND kitchen designer** — all hand-built. LangGraph lets the code focus on making each specialist better at their job, while the framework handles the management.

---

## What's Wrong With the Hand-Built Approach?

The current `llm-bi-chat-agentic` works well, but has four pain points that grow over time:

### 1. State is scattered Post-it notes

```python
# Current approach — fragile nested dicts in session state
st.session_state.chart_pending = {
    "df_json": ...,
    "original_question": ...,
    "last_sql": ...,
    "phase": "ask_plot" | "charting",
    "history": [...]
}
```

If the app crashes, all state is lost. Adding a new field to the state means hunting down every place that reads or writes it.

### 2. The routing logic is a long if/elif chain

```python
# Current approach — grows with every new capability
if chart_conv and chart_conv["phase"] == "ask_plot":
    ...
elif chart_conv and chart_conv["phase"] == "charting":
    ...
elif intent == "reformat" and last_df_msg:
    ...
else:
    # regular query
```

Every new agent or phase means editing this chain.

### 3. Human-in-the-loop is awkward

The ChartAgent needs to **pause and wait for the user** mid-conversation. The current implementation simulates this by setting `phase: "ask_plot"` and returning early — the graph of "who talks next" lives entirely in if/elif logic across 500 lines of `app.py`.

### 4. No visibility into what happened

When something goes wrong, you add print statements and read logs. There's no visual representation of the execution path.

---

## LangGraph: The Five Big Wins

### 1. Draw the map instead of writing the directions

Instead of writing code that says "if this then go there, else go here", you **declare a graph**:

```python
graph = StateGraph(BIState)

graph.add_node("router", router_node)
graph.add_node("query_agent", query_agent_node)
graph.add_node("visualization_agent", visualization_agent_node)
graph.add_node("chart_agent", chart_agent_node)

graph.add_conditional_edges("router", classify_intent, {
    "query":         "query_agent",
    "visualization": "visualization_agent",
    "chart":         "chart_agent",
})
graph.add_edge("query_agent", END)
graph.add_conditional_edges("chart_agent", has_chart_spec, {
    "yes": "render_chart",
    "no":  END,           # interrupt — wait for user
})
```

The routing logic lives in the graph structure, not buried in if/elif chains.

### 2. One State object, automatically saved

```python
class BIState(MessagesState):
    """Everything the graph needs — one place, typed, checkpointed."""
    filter_context: str = ""           # sidebar filters active when the question was asked;
                                       # preserved across interrupt() resumes so chart SQL
                                       # stays consistent with the data already shown
    df_json:        str | None = None
    last_sql:       str | None = None
    chart_spec:     dict | None = None
    dashboard_url:  str | None = None  # published URL (intranet / Power BI), not HTML content —
                                       # VisualizationAgent publishes the report externally and
                                       # stores only the reference here to keep checkpoints small
    chart_history:  list = []
```

All agents read from and write to this single state. LangGraph checkpoints it automatically — if the app restarts, the conversation resumes exactly where it left off.

**Filter change detection:** `filter_context` is written from the sidebar on every new user question. The UI compares the current sidebar value against `state.filter_context` from the checkpoint before each invoke. If they differ mid-conversation (e.g., user changed the date range while ChartAgent was waiting for an answer), the UI surfaces a prompt:

> _"Filters have changed (Jan–Mar → Jan–Jun). Continue with the original filters or apply the new ones?"_

The user's choice is held in `st.session_state` (not in graph state — the graph has no role in resolving the mismatch). The confirmed value is then written into `filter_context` on the next `graph.invoke()` call.

### 3. Human-in-the-loop is a first-class concept

When the ChartAgent needs to ask the user a question (e.g., _"What would you like on the axes?"_), LangGraph's `interrupt` pauses the entire graph and waits:

```python
# Inside chart_agent_node — the graph pauses here automatically
user_answer = interrupt("What would you like on the axes?")
```

No more phase flags, no more checking `chart_pending["phase"]` in a 100-line if/elif block.

### 4. Retry and error handling built in

```python
# Automatic retry with backoff — no try/except everywhere
graph.add_node("query_agent", query_agent_node, retry=RetryPolicy(max_attempts=3))
```

### 5. Visual debugging with LangGraph Studio

LangGraph Studio shows the graph as a live map — green nodes for completed steps, red for errors, yellow for waiting. No more print statements.

---

## Target Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Streamlit UI (app.py)                     │
│                                                             │
│  Sends user message → graph.invoke()                        │
│  Renders graph state (text, charts, dashboards)             │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              LangGraph StateGraph (graph.py)                 │
│                                                             │
│  BIState = { messages, filter_context, df_json, last_sql,   │
│              chart_spec, dashboard_url, chart_history }     │
│                                                             │
│  ┌──────────┐                                               │
│  │  Router  │  (Claude classifies intent)                   │
│  └────┬─────┘                                               │
│       │                                                     │
│  ┌────┼──────────────┐                                      │
│  ▼    ▼              ▼                                      │
│ Query Visual       Chart ◄──── asks questions               │
│ Agent Agent        Agent       until ready                  │
│  │    │            │  ↑                                     │
│  │    │            ▼  │                                     │
│  │    │         Has chart-spec?                             │
│  │    │         No → interrupt()  ← user answers            │
│  │    │         Yes → Render Chart                          │
│  ▼    ▼              ▼                                      │
│  └────┴──────────────┘                                      │
│               │                                             │
│             END                                             │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    ┌──────┴──────┐
                    │  MCP Layer  │
                    │  (DuckDB)   │
                    └─────────────┘
```

---

## Comparison: Custom Orchestrator vs LangGraph

| Capability | `llm-bi-chat-agentic` (custom) | `llm-bi-chat-langgraph` (target) |
|------------|-------------------------------|----------------------------------|
| **Routing** | Claude tool_use + if/elif chain in app.py | Conditional edges in StateGraph |
| **State** | `st.session_state` dicts with phases | Typed `BIState`, auto-checkpointed |
| **Human-in-the-loop** | Phase flags + early return + resume logic | `interrupt()` — one line |
| **Error handling** | try/except in every agent | Node-level `RetryPolicy` |
| **Multi-turn chat** | Manual history lists in state | `MessagesState` — built in |
| **Adding a new agent** | Write agent + edit app.py routing | Add node + draw edge |
| **Visibility** | Logs + print statements | LangGraph Studio visual debugger |
| **State persistence** | Lost on restart | Checkpointed to DB |
| **Streaming** | Manual `st.write_stream` | Native graph streaming |

---

## What Stays the Same

LangGraph replaces the **orchestration layer** only. The specialist agents, MCP connection, and database layer are unchanged:

| Component | Changes? | Notes |
|-----------|----------|-------|
| `QueryAgent` | ✅ Kept | Becomes a LangGraph node |
| `VisualizationAgent` | ⚠️ Extended | Becomes a LangGraph node; gains a publish step that pushes HTML to intranet/Power BI and writes the URL to `BIState.dashboard_url` instead of returning raw HTML |
| `ChartAgent` | ✅ Kept | Uses `interrupt()` instead of phase flags |
| `run_tool_loop()` | ✅ Kept | Still used inside agent nodes |
| DuckDB MCP Server | ✅ Kept | Unchanged |
| `_DASHBOARD_TEMPLATE` | ✅ Kept | Unchanged |
| `MCPConnectionManager` | ✅ Kept | Unchanged |
| `OrchestratorAgent` | ❌ Replaced | Becomes the router node + conditional edges |
| `st.session_state` routing logic | ❌ Replaced | Becomes graph state + edge conditions |
| Phase flags in app.py | ❌ Replaced | Becomes `interrupt()` in ChartAgent node |

---

## Migration Plan

### Phase 1 — Define the state and graph skeleton
- Create `src/graph/state.py` — `BIState(MessagesState)`
- Create `src/graph/graph.py` — `StateGraph` with nodes and edges
- Wire existing agents as node functions

### Phase 2 — Replace the orchestrator and extend VisualizationAgent
- Remove `OrchestratorAgent` and `AgentRegistry`
- Add router node (same Claude call, now returns edge name)
- Add conditional edges for routing
- Extend `VisualizationAgent` with a publish step: render HTML → publish to intranet/Power BI → write URL to `BIState.dashboard_url`; remove `dashboard_html` from `AgentResult`
- **Implementation gap:** the publish target (intranet base URL, Power BI workspace ID, dataset ID, bearer token) must be supplied via environment variables or LangGraph node config — not hardcoded; the node should raise a clear error at startup if required vars are missing rather than failing silently at publish time

### Phase 3 — Replace state machine with interrupt
- Remove `chart_pending` phases from `st.session_state`
- ChartAgent node uses `interrupt()` to pause and wait for user
- Graph resumes from checkpoint on next user message

### Phase 4 — Update the UI
- `app.py` calls `graph.invoke()` instead of `orchestrator.query()`
- UI reads from `BIState` instead of `result.data`, `result.text` etc.
- Add LangGraph streaming for real-time response rendering
- On each invoke, compare current sidebar filters against `state.filter_context` from the checkpoint; if they differ mid-conversation, prompt the user to choose between original and new filters before proceeding

### Phase 5 — Add persistence
- Configure checkpointer (SQLite or PostgreSQL)
- Conversations survive restarts
- Multiple users get isolated graph instances via `thread_id`

---

## Architecture Decision Records

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](docs/adr/ADR-001-checkpointer.md) | Use `SqliteSaver` for LangGraph checkpointing | Accepted |

---

## Data Model

Three tables in DuckDB (unchanged from `llm-bi-chat-agentic`):

| Table | Key Columns |
|-------|-------------|
| **orders** | `original_reference`, `created_date`, `channel`, `net_sales`, `total_sales`, `gross_margin` |
| **order_lines** | `original_reference` (FK), `style_code`, `name`, `net_sales`, `total_cost` |
| **items** | `style_code` (FK), `category`, `sub_category` |

---

## Setup

### Prerequisites
- Python 3.11+
- [Poetry](https://python-poetry.org/)
- Anthropic API key

### Install

```bash
cd llm-bi-chat-langgraph
poetry install
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
poetry run python scripts/prepare_bi_data.py
poetry run streamlit run src/app.py
```

### Add LangGraph dependency

```bash
poetry add langgraph langchain-anthropic
```

---

## Security

### Security Audit

Security checks run automatically on every push and pull request via [.github/workflows/security.yml](.github/workflows/security.yml), and on a weekly schedule to catch newly published CVEs.

Run all four checks locally in one command:

```bash
make security-scan          # always exits 0 — for local use
make security-scan-strict   # exits 1 on any finding — for CI pipelines
```

Results are saved to `reports/cve_audit.txt` and `reports/gitleaks.json`.

**What it runs:**

| Tool | Checks |
|------|--------|
| [`pip-audit`](https://github.com/pypa/pip-audit) | Known CVEs in all locked dependencies (from `poetry.lock`) |
| [`bandit`](https://bandit.readthedocs.io/) | Insecure code patterns in `src/` (SQL injection, hardcoded secrets, etc.) |
| [`gitleaks`](https://github.com/gitleaks/gitleaks) | Secrets and API keys in working tree and full git history |
| [`trivy`](https://github.com/aquasecurity/trivy) | Dependency CVEs (cross-checks pip-audit) + Dockerfile/docker-compose IaC misconfigurations |

**First-time setup** — install all tools in one command:

```bash
make security-install
```

Or manually:

```bash
brew install pipx gitleaks trivy
pipx install pip-audit
pipx install bandit
```

**Implementation notes:**
- Parses `poetry.lock` directly — no `poetry export` plugin required
- Platform-incompatible packages (e.g. `pywin32` on macOS/Linux) are automatically filtered before the CVE scan
- Allowlisted paths (`.env` is already gitignored and expected to hold credentials) are configured in [.gitleaks.toml](.gitleaks.toml)
- Trivy uses a 15-minute timeout to handle the ~88 MB vuln DB download on first run; the DB is cached locally on subsequent runs
- Reports written to `reports/` (gitignored)

---

## References

- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)
- [LangGraph human-in-the-loop](https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/)
- [LangGraph Studio](https://github.com/langchain-ai/langgraph-studio)
- Starting point: [`llm-bi-chat-agentic`](https://github.com/suryapatnaik1/llm-bi-chat-agentic)
