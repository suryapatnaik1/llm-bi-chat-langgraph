# LLM BI Chat — Agentic Architecture

A conversational Business Intelligence app where users ask questions about order data in plain English. An **LLM-powered orchestrator** classifies intent and routes to specialist agents that write SQL, execute queries, and generate interactive dashboards — all without hardcoded rules.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Streamlit UI (app.py)                     │
│  Chat input ─► filters ─► message history ─► render result  │
│                                                             │
│  Two rendering paths:                                       │
│    1. Query results → Plotly charts inline in chat          │
│    2. Dashboard reports → saved to disk, opened via button  │
└──────────────────────────┬──────────────────────────────────┘
                           │ question + filter context
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              OrchestratorAgent (orchestrator.py)             │
│                                                             │
│  Single Claude call with specialist agents exposed as tools │
│  Claude picks the right agent via tool_use — no keywords,   │
│  no regex, no if/else chains                                │
└────────────┬───────────────────────────┬────────────────────┘
             │                           │
             ▼                           ▼
┌────────────────────────┐  ┌────────────────────────────────┐
│  QueryAgent            │  │  VisualizationAgent            │
│  (query_agent.py)      │  │  (visualization_agent.py)      │
│                        │  │                                │
│  Text-to-SQL specialist│  │  Standard BI report specialist │
│  Writes DuckDB SQL,    │  │  Generates dashboard-data JSON │
│  executes via MCP,     │  │  with KPIs + Chart.js configs, │
│  returns text + DF     │  │  renders to _DASHBOARD_TEMPLATE│
└────────────┬───────────┘  └──────────────┬─────────────────┘
             │                             │
             ▼                             ▼
┌─────────────────────────────────────────────────────────────┐
│                  Shared Agent Loop (base.py)                 │
│                                                             │
│  run_tool_loop(): Claude ↔ MCP execute_sql, repeat until    │
│  Claude returns end_turn. Returns (text, last_sql).         │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              DuckDB MCP Server (duckdb_mcp_server.py)        │
│                                                             │
│  Subprocess over stdio  │  Tools: get_schema, execute_sql   │
│  Read-only SELECT only  │  Returns markdown tables          │
└─────────────────────────┴───────────────────────────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  DuckDB      │
                    │  local_data/ │
                    │  bi.db       │
                    └──────────────┘
```

## How the Agentic Routing Works

Traditional BI apps use keyword matching or regex to decide what to do with a user's question. This app uses **Claude itself as the router**.

### The Orchestrator Pattern

1. **User asks**: _"What were our top 5 products last month?"_
2. **OrchestratorAgent** makes a **single Claude API call** with specialist agents registered as tools:
   ```
   tools: [
     { name: "query_agent",         description: "Answer factual questions..." },
     { name: "visualization_agent", description: "Create full BI reports/dashboards..." }
   ]
   ```
3. **Claude classifies intent** and returns a `tool_use` block:
   ```json
   { "type": "tool_use", "name": "query_agent", "input": { "question": "..." } }
   ```
4. **Orchestrator dispatches** to the selected agent's `run()` method
5. **Agent executes** its own Claude loop with `execute_sql` MCP tools
6. **Result flows back** to the UI as a structured `AgentResult`

This means:
- _"Show me total revenue"_ → `query_agent` (factual data lookup → text + DataFrame)
- _"Give me a dashboard of sales by channel"_ → `visualization_agent` (standard report with KPIs + charts)
- _"What does our revenue look like over time?"_ → Claude decides based on nuance, not keywords

### The Agent Loop

Each specialist agent uses a shared `run_tool_loop()` that implements the standard Claude tool-use cycle:

```
Claude call (system prompt + tools + messages)
    │
    ├─ stop_reason: "tool_use" → execute MCP tool → track last_sql → append result → loop
    │
    └─ stop_reason: "end_turn" → return (final_text, last_sql)
```

This allows agents to make **multiple SQL calls** in a single conversation turn — e.g., the visualization agent might run 4-5 queries to gather data for different dashboard charts before composing the final response.

The `last_sql` tracking is critical: QueryAgent re-executes the final SQL against DuckDB to obtain a Polars DataFrame, which the UI then uses for interactive Plotly charting.

## Two Rendering Paths

The app has two distinct ways to display data, matching the approach in [`llm-bi-chat`](../llm-bi-chat):

### Path 1: LLM-Driven Conversational Charts (QueryAgent → ChartAgent)

When the QueryAgent returns a DataFrame, the UI offers to plot it. If the user says yes, the **ChartAgent** — a separate multi-turn LLM conversation — takes over. Unlike a fixed state machine, the ChartAgent **decides what to ask** based on context:

```
User: "show me net sales by channel"
  │
  ▼
QueryAgent → text answer + DataFrame
  │
  ▼
App: "Would you like me to plot this data?"
  │
  ▼ (user says "yes")
ChartAgent receives: DataFrame schema + original question + conversation history
  │
  ├─ If specific enough → returns chart-spec immediately (0 questions)
  │
  └─ If ambiguous → asks ONE clarifying question at a time:
       "What would you like on the axes? e.g., revenue over time, or sales by region"
       "A bar chart would work well. Do you want monthly or yearly aggregation?"
       "Would you like to compare with another metric?"
       │
       ▼ (when ready)
  ChartAgent returns ```chart-spec``` JSON → Plotly chart rendered inline
```

**Key details:**
- **Adaptive questions**: the LLM evaluates what's known vs missing — asks 0-3 questions depending on clarity
- Uses **Plotly Express** for all chart types (bar, line, pie, scatter, area, histogram)
- Charts render **inline in the chat** — no external pages or links
- Supports **reformat intent**: _"show that as a bar chart"_ → ChartAgent gets the previous DataFrame and returns a new chart-spec immediately
- **No hardcoded step order**: the LLM picks questions based on the data (e.g., asks about time aggregation only when date columns exist)

**Chart-spec format** (returned by ChartAgent when ready):
```json
{
  "chart_type": "bar",
  "title": "Monthly Net Sales by Channel",
  "x": "channel",
  "y": "net_sales",
  "color": null
}
```

**Session state** (`chart_pending`):
```
phase: "ask_plot"  → waiting for yes/no
phase: "charting"  → active ChartAgent conversation (multi-turn LLM with history)
```

### Path 2: Standard BI Reports (VisualizationAgent)

When the VisualizationAgent is selected, it generates a **full standard report** with KPIs and multiple charts:

```
User: "give me an overview of sales performance"
  │
  ▼
VisualizationAgent → runs 4-5 SQL queries via MCP
  │
  ▼
Claude returns ```dashboard-data JSON block
  │
  ▼
parse_dashboard_response() → extract JSON via regex
  │
  ▼
render_dashboard() → inject into _DASHBOARD_TEMPLATE
  │
  ▼
save_report() → src/static/reports/report_<timestamp>.html
  │
  ▼
Summary text displayed in chat (no inline HTML, no "Open report" link)
Dashboard button (top-right) opens the latest report in a new tab
```

**The dashboard is never shown inline in chat** — it's a full HTML page with Chart.js, opened via the 📋 Dashboard button at the top-right corner of the app.

## `_DASHBOARD_TEMPLATE`

Uses the same `_DASHBOARD_TEMPLATE` approach as [`llm-bi-chat`](../llm-bi-chat) — a complete HTML/CSS/JS bundle in `dashboard_renderer.py` with placeholder tokens that get replaced at render time.

### Template Structure

Defined in `src/services/dashboard_renderer.py`, the template is a self-contained HTML page:

- **Chart.js 4.4.0** embedded inline (no CDN dependency — works in Streamlit's sandboxed iframe)
- **Inline CSS** — responsive grid for KPI cards and chart panels
- **JavaScript renderer** — reads `__DASHBOARD_JSON__` at page load and dynamically creates:
  - KPI cards with label, value, and change indicators (▲/▼)
  - Chart.js canvases for `bar`, `pie`/`doughnut`, and `line` chart types
- **Color palette** — 8-color scheme injected via `__PALETTE_JSON__` (indigo, emerald, amber, red, blue, purple, teal, orange)

### Dashboard JSON Schema

The `VisualizationAgent` system prompt instructs Claude to return exactly this structure inside a ` ```dashboard-data ` fenced block:

```json
{
  "title": "Sales Overview",
  "subtitle": "One-sentence description of the dashboard",
  "kpis": [
    { "label": "Total Revenue", "value": "£234,567", "change": "+12.3%", "up": true }
  ],
  "charts": [
    { "title": "Revenue by Channel", "type": "bar", "data": { ... } },
    { "title": "Category Mix", "type": "pie", "data": { ... } },
    { "title": "Monthly Trend", "type": "line", "data": { ... } }
  ]
}
```

## Project Structure

```
src/
  app.py                          # Streamlit UI — chat, filters, Plotly charts, dashboard button
  config.py                       # API keys, model, paths (single source of truth)
  agents/
    base.py                       # AgentResult dataclass + BaseAgent ABC + run_tool_loop()
    orchestrator.py               # LLM-based router via tool_use
    query_agent.py                # Text-to-SQL specialist (returns text + DataFrame)
    visualization_agent.py        # Standard BI report generation
    chart_agent.py                # LLM-driven conversational chart builder (multi-turn)
    registry.py                   # AgentRegistry — auto-generates orchestrator tools
  services/
    mcp_connection.py             # MCP subprocess lifecycle + schema caching
    dashboard_renderer.py         # _DASHBOARD_TEMPLATE + report file management
    schema_pruner.py              # 3-tier schema optimization
  sql/
    duckdb_mcp_server.py          # MCP server: get_schema + execute_sql tools
scripts/
  prepare_bi_data.py              # JSON → DuckDB ingestion
local_data/
  uploads/                        # Raw JSON data files
  bi.db                           # DuckDB database (generated)
```

### Layer Responsibilities

| Layer | Files | Does | Does NOT |
|-------|-------|------|----------|
| **UI** | `app.py` | Render chat, manage filters, Plotly charts inline, dashboard button, route to ChartAgent | Call MCP agents directly, write SQL, parse data |
| **Orchestrator Agents** | `orchestrator.py`, `query_agent.py`, `visualization_agent.py` | Classify intent, write SQL prompts, interpret results, return DataFrames | Manage connections, render HTML, touch the filesystem |
| **Chart Agent** | `chart_agent.py` | Multi-turn chart conversation, ask clarifying questions, return chart-spec JSON | Execute SQL, access MCP, render charts |
| **Services** | `mcp_connection.py`, `dashboard_renderer.py`, `schema_pruner.py` | Manage MCP subprocess, render dashboard templates, optimize schemas | Make LLM calls, handle user input |
| **Data** | `duckdb_mcp_server.py` | Execute read-only SQL, return schema DDL | Anything else |

## Adding a New Agent

The architecture is designed so adding a new capability is a 3-step process:

**1. Create the agent** (`src/agents/my_agent.py`):
```python
from agents.base import AgentResult, BaseAgent, run_tool_loop

class MyAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "my_agent"

    @property
    def description(self) -> str:
        return "One-line description of what this agent does."

    async def run(self, question, session, schema, context=None):
        # Use run_tool_loop() with your own system prompt
        ...
        return AgentResult(text="...", agent_name=self.name)
```

**2. Register it** in `src/app.py`:
```python
registry.register(MyAgent())
```

**3. Done.** The orchestrator automatically sees the new agent as a tool and will route appropriate questions to it.

## Data Model

Three tables in DuckDB, ingested from JSON files:

| Table | Source | Key Columns |
|-------|--------|-------------|
| **orders** | `HeaderResults.json` | `original_reference`, `created_date`, `channel`, `net_sales`, `total_sales`, `gross_margin` |
| **order_lines** | `LinesResults.json` | `original_reference` (FK), `style_code`, `name`, `net_sales`, `total_cost` |
| **items** | `items.json` | `style_code` (FK), `category`, `sub_category` |

## Setup

### Prerequisites
- Python 3.11+
- [Poetry](https://python-poetry.org/)
- Anthropic API key

### Local Development

```bash
# Install dependencies
poetry install

# Set your API key
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Ingest data (JSON → DuckDB)
poetry run python scripts/prepare_bi_data.py

# Run the app
poetry run streamlit run src/app.py
```

Open [http://localhost:8501](http://localhost:8501).

### Docker

```bash
# Ingest data
docker compose run --rm --profile tools ingest

# Build and start
docker compose up --build
```

## Configuration

All configuration lives in `src/config.py`, driven by environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required. Anthropic API key |
| `LLM_MODEL` | `claude-sonnet-4-20250514` | Claude model ID |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |

## MCP — How the Database Connection Works

The app uses the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) as the standardized interface between Claude and DuckDB. There are **two paths to the database**, each serving a different purpose.

### Path 1: MCP (Claude ↔ DuckDB)

MCP is the primary path for all agent SQL execution. The `duckdb_mcp_server.py` runs as a **stdio subprocess** exposing two tools:

| MCP Tool | Purpose |
|----------|---------|
| `get_schema` | Returns `CREATE TABLE` DDL for all tables (fetched once, cached) |
| `execute_sql` | Executes read-only `SELECT` queries, returns markdown tables |

The execution flow:

```
Claude generates tool_use: execute_sql({ sql: "SELECT ..." })
    ↓
run_tool_loop() calls session.call_tool("execute_sql", { sql })
    ↓
MCP subprocess receives request over stdio
    ↓
duckdb_mcp_server.py executes query against bi.db
    ↓
Returns markdown table → fed back to Claude as tool_result
    ↓
Claude interprets results → generates next tool_use or end_turn
```

**Lifecycle**: A new MCP subprocess is spawned per `orchestrator.query()` call. Within that call, the same subprocess handles multiple `execute_sql` invocations (e.g., the VisualizationAgent may run 4–5 queries). The schema is cached in-memory across subprocess instances.

### Path 2: Direct DuckDB (UI ↔ DuckDB)

For charting, the app bypasses MCP and connects directly to DuckDB to get native Polars DataFrames:

- **`QueryAgent._fetch_df()`** — re-executes `last_sql` locally after the MCP loop to get a DataFrame for Plotly
- **`_execute_chart_sql()`** in `app.py` — executes SQL from `chart-spec` when the ChartAgent needs a different data shape (e.g., monthly aggregation)
- **Sidebar filters** — `_get_channel_options()` and `get_date_bounds()` query DuckDB directly for filter values

This avoids parsing MCP's markdown table format and is faster for getting structured data into Plotly.

### Which components use which path?

| Component | MCP | Direct DuckDB | Why |
|-----------|-----|---------------|-----|
| **OrchestratorAgent** | Schema + tool listing | — | Needs schema for agents, tool list for Claude |
| **QueryAgent** | `execute_sql` via agent loop | `_fetch_df()` for DataFrame | MCP for Claude, direct for Plotly |
| **VisualizationAgent** | `execute_sql` via agent loop | — | Dashboard data stays as text in Claude |
| **ChartAgent** | — | `_execute_chart_sql()` | No MCP needed — just re-queries for chart data |
| **Sidebar filters** | — | Direct queries | Simple lookups, no LLM involved |

### MCP Server Configuration

The MCP server is configured in `MCPConnectionManager` (`src/services/mcp_connection.py`):

```python
# Spawns as a subprocess over stdio
server_params = StdioServerParameters(
    command="python",
    args=["src/sql/duckdb_mcp_server.py"],
)
```

The connection manager handles:
- Subprocess lifecycle (spawn/cleanup)
- Schema caching (fetched once on first query)
- Async/sync bridge (Streamlit is sync, MCP is async)
- 120-second query timeout

## Schema Pruning

The `schema_pruner` service applies three optimization tiers before sending schema to agents:

1. **Tier 1 — Table selection**: keyword hints determine which tables are relevant
2. **Tier 2 — Column linking**: only columns matching question tokens are included
3. **Tier 3 — Value annotation**: low-cardinality VARCHAR columns get inline value hints (e.g., `-- values: 'Shopify -UK', 'Amazon'`)

This reduces token usage and improves SQL accuracy.

## Tech Stack

| Component | Purpose |
|-----------|---------|
| Streamlit | Chat UI + sidebar filters |
| Claude (Anthropic) | LLM for routing, SQL generation, data interpretation, chart suggestions |
| DuckDB | In-process SQL database (read-only) |
| MCP | Standardized tool interface between Claude and DuckDB |
| Plotly Express | Inline interactive charts in chat (bar, line, pie, scatter, area, histogram) |
| Chart.js | Client-side charting in generated dashboard reports |
| Polars | DataFrame handling between agents and UI |
