# Architecture Diagrams

---

## Groups

| Group | Diagrams |
|-------|----------|
| **Architecture Overview** | 1 — Graph Topology · 2 — End-to-End Request Flow |
| **Phase 3: Human-in-the-Loop** | 3 — interrupt() Flow · 4 — LLM-Driven Chart |
| **Internal Mechanics** | 5 — Agent Loop · 6 — Dashboard Rendering · 7 — Schema Pruning |
| **Phase 6: Scale & Operations** | 8 — Deployment Architecture · 9 — Caching Strategy · 10 — Model Tier Split · 11 — Checkpointer Evolution |
| **Legacy Reference** | 12 — Pre-LangGraph Agent Routing · 13 — Pre-LangGraph State Machine |

---

## 1. LangGraph Graph Topology

The target `StateGraph` structure. All routing decisions are expressed as conditional edges over pure functions — no if/elif chains in application code.

```mermaid
flowchart TD
    START([START]) --> RN

    RN["router_node<br/>────────────────<br/>Model: claude-haiku-4-5<br/>Classifies: query / visualization / chart / reformat<br/>L3: LRU cache (512 slots)"]

    RN -->|query| QA
    RN -->|visualization| VA
    RN -->|"chart / reformat"| CA

    QA["query_agent_node<br/>────────────────<br/>Model: claude-sonnet-4-6<br/>SQL generation via run_tool_loop<br/>Resets df_json at start of each run"]

    VA["visualization_agent_node<br/>────────────────<br/>Model: claude-sonnet-4-6<br/>Multi-SQL dashboard builder<br/>Publishes HTML → writes URL to BIState"]

    CA["chart_agent_node<br/>────────────────<br/>Model: claude-sonnet-4-6<br/>Conversational chart builder<br/>interrupt() for clarifying questions"]

    OP["offer_plot_node<br/>────────────────<br/>interrupt() — graph pauses<br/>Asks: Would you like to plot?<br/>Reads plot_accepted from resume"]

    QA -->|"has_dataframe = yes"| OP
    QA -->|"has_dataframe = no"| E1([END])

    OP -->|"plot_accepted = yes"| CA
    OP -->|"plot_accepted = no"| E2([END])

    VA --> E3([END])
    CA --> E4([END])

    style RN fill:#fef3c7,stroke:#f59e0b,color:#000000
    style QA fill:#e0e7ff,stroke:#6366f1,color:#000000
    style VA fill:#e0e7ff,stroke:#6366f1,color:#000000
    style CA fill:#e0e7ff,stroke:#6366f1,color:#000000
    style OP fill:#fce7f3,stroke:#ec4899,color:#000000
```

**Edge condition functions (pure, unit-testable):**

| Function | Reads | Returns |
|----------|-------|---------|
| `classify_intent(state)` | `state["intent"]` | `"query"` / `"visualization"` / `"chart"` / `"reformat"` |
| `has_dataframe(state)` | `state["df_json"]` | `"yes"` / `"no"` |
| `offer_plot_answer(state)` | `state["plot_accepted"]` | `"yes"` / `"no"` |

---

## 2. End-to-End Request Flow (Phase 6)

Complete sequence for a SQL query request in the Phase 6 multi-worker deployment. Shows how all layers — routing, caching, agents, DuckDB, checkpointing, and streaming — interact on a single user turn.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'actorTextColor': '#000000', 'noteTextColor': '#000000', 'loopTextColor': '#000000', 'labelTextColor': '#000000', 'actorBkg': '#ffffff', 'actorBorder': '#333333'}}}%%
sequenceDiagram
    actor User
    participant SL as Streamlit UI
    participant LGS as LangGraph Server
    participant RN as router_node
    participant QA as query_agent_node
    participant PG as PostgreSQL
    participant DDB as DuckDB (shared)
    participant CL as Claude API

    User->>SL: "Top 5 products last month?"
    SL->>LGS: client.runs.stream(thread_id, input, stream_mode="values")
    LGS->>PG: Load checkpoint (thread_id) — BIState restore
    PG-->>LGS: BIState (conversation history, filter_context)

    rect rgb(254, 243, 199)
        Note over RN: router_node — Haiku classification
        LGS->>RN: BIState
        RN->>RN: Check L3 LRU cache (message, has_df)
        alt L3 cache hit (~25–40% of calls)
            RN-->>LGS: intent = "query" (no API call)
        else L3 cache miss
            RN->>CL: claude-haiku-4-5 (50 tokens in, max_tokens=10)
            CL-->>RN: "query"
        end
        RN->>PG: Checkpoint (intent="query")
    end

    rect rgb(224, 231, 255)
        Note over QA: query_agent_node — Sonnet + DuckDB
        LGS->>QA: BIState
        QA->>QA: Reset df_json=None, last_sql=None

        QA->>PG: L4 Tier B semantic lookup (embed question → pgvector cosine ≥0.97)
        alt Tier B hit (~40–60% at 100 users)
            PG-->>QA: Cached (sql, result_json) — zero tokens, zero DuckDB
        else Tier B miss — try Tier A
            QA->>PG: L4 Tier A exact lookup (sqlglot SHA256)
            alt Tier A hit
                PG-->>QA: Cached result_json — DuckDB saved
            else Full miss
                QA->>CL: claude-sonnet-4-6 (L1 prompt cache hit → 90% token cost)
                CL-->>QA: tool_use: execute_sql { sql: "SELECT ..." }
                QA->>DDB: execute_sql (read_only shared connection)
                DDB-->>QA: Result rows
                QA->>CL: tool_result → continue
                CL-->>QA: end_turn + answer text
                QA->>PG: Write L4 Tier A + Tier B cache entries
            end
        end

        QA->>QA: Write df to local_data/df_cache/uuid.json (L5)
        QA->>PG: Checkpoint (df_path, last_sql, answer)
    end

    LGS-->>SL: SSE stream — node events as they complete
    SL->>SL: Render text answer + "Would you like to plot?" button
    SL->>User: Display response
```

---

## 3. Phase 3: Human-in-the-Loop with interrupt()

LangGraph's `interrupt()` pauses the graph mid-execution and persists all state to the checkpointer. The UI resumes the graph on the next user message using `Command(resume=...)`.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'actorTextColor': '#000000', 'noteTextColor': '#000000', 'loopTextColor': '#000000', 'labelTextColor': '#000000', 'actorBkg': '#ffffff', 'actorBorder': '#333333'}}}%%
sequenceDiagram
    actor User
    participant SL as Streamlit UI
    participant LG as LangGraph
    participant QA as query_agent_node
    participant OP as offer_plot_node
    participant CA as chart_agent_node
    participant CP as Checkpointer

    Note over LG,CP: Turn 1 — Query executes, graph pauses at offer_plot_node

    User->>SL: "Show top products by channel"
    SL->>LG: graph.invoke(messages, thread_id)
    LG->>QA: Run node
    QA-->>LG: df_json set, last_sql set, chart_history reset
    LG->>OP: Run node
    OP->>CP: Checkpoint state (df_json, last_sql, original_question)
    OP->>LG: interrupt("Would you like to plot this data?")
    LG-->>SL: Returns interrupt value
    SL->>User: "Would you like to plot this data?"

    Note over LG,CP: Turn 2 — User confirms, graph resumes from checkpoint

    User->>SL: "Yes"
    SL->>LG: graph.invoke(Command(resume="yes"), thread_id)
    LG->>CP: Load checkpoint (restore full BIState)
    CP-->>LG: BIState (df_json, last_sql intact)
    LG->>OP: Resume → plot_accepted = "yes"
    LG->>CA: Run node
    CA->>LG: interrupt("What type of chart? bar / line / pie / scatter")
    LG-->>SL: Interrupt value
    SL->>User: "What type of chart would you like?"

    Note over LG,CP: Turn 3 — Chart spec complete, graph reaches END

    User->>SL: "Bar chart by channel"
    SL->>LG: graph.invoke(Command(resume="bar chart by channel"), thread_id)
    LG->>CP: Load checkpoint
    LG->>CA: Resume → append to chart_history, return chart_spec
    CA-->>LG: chart_spec JSON complete
    LG-->>SL: END — final BIState
    SL->>User: Render Plotly chart inline
```

**State fields used by Phase 3:**

| Field | Written by | Read by |
|-------|-----------|---------|
| `df_json` | `query_agent_node` | `offer_plot_node`, `chart_agent_node` |
| `last_sql` | `query_agent_node` | `chart_agent_node` |
| `original_question` | `query_agent_node` | `chart_agent_node` |
| `db_schema` | `query_agent_node` | `chart_agent_node` |
| `plot_accepted` | `offer_plot_node` (via resume) | `offer_plot_answer()` edge |
| `chart_history` | `chart_agent_node` (reset by `query_agent_node`) | `chart_agent_node` |

---

## 4. LLM-Driven Conversational Chart Generation

Instead of a rigid state machine, the **LLM itself decides what questions to ask** based on the user's input and DataFrame schema. Implemented in Phase 3 as `chart_agent_node` using `interrupt()`.

### Why LLM-Driven?

The old state machine always asks the same 4 questions in the same order, regardless of context. But:
- _"Plot net sales by channel as a bar chart"_ — needs **zero** clarifications
- _"Show me a chart"_ — needs axes, chart type, aggregation
- _"Revenue over time"_ — needs aggregation level but chart type is obvious (line)

The LLM evaluates what's **already known** vs **what's missing** and asks only what's needed.

### Conversation Flow

```mermaid
flowchart TD
    A[QueryAgent returns DataFrame] --> B["offer_plot_node: Would you like to plot?"]
    B -->|No| Z[END — text answer only]
    B -->|Yes| E["chart_agent_node activated<br/>Pass: df_json, db_schema, original_question, last_sql"]

    E --> F{"ChartAgent LLM evaluates:<br/>What information is missing?"}

    F -->|Axes unclear| G["What would you like on the axes?<br/>e.g., revenue over time, or sales by region"]
    F -->|Chart type unclear| H["What kind of chart?<br/>bar, line, pie, scatter, area"]
    F -->|Time column detected| I["What aggregation level?<br/>hourly, daily, monthly, yearly"]
    F -->|Multiple metrics possible| J["Compare with another metric?<br/>Apply any filters?"]
    F -->|Everything clear| K["Generate chart-spec JSON"]

    G --> L[User answers via interrupt resume]
    H --> L
    I --> L
    J --> L

    L --> F

    K --> M[Execute SQL from chart-spec]
    M --> N["Render Plotly chart inline in chat"]
    N --> O["Anything you'd like to adjust?"]
    O -->|Yes| F
    O -->|No| Z

    style E fill:#e0e7ff,stroke:#6366f1,color:#000000
    style F fill:#fef3c7,stroke:#f59e0b,color:#000000
    style K fill:#d1fae5,stroke:#10b981,color:#000000
    style N fill:#d1fae5,stroke:#10b981,color:#000000
```

### Multi-Turn Sequence

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'actorTextColor': '#000000', 'noteTextColor': '#000000', 'loopTextColor': '#000000', 'labelTextColor': '#000000', 'actorBkg': '#ffffff', 'actorBorder': '#333333'}}}%%
sequenceDiagram
    participant U as User
    participant UI as Streamlit
    participant CA as chart_agent_node (LLM)
    participant CP as Checkpointer

    Note over UI: offer_plot_node returned "yes"

    UI->>CA: df_json + db_schema + original_question + chart_history
    Note over CA: System prompt: Ask ONE question at a time.<br/>When ready, return chart-spec JSON.

    CA->>CP: interrupt("What would you like on the axes?")
    CP-->>UI: Pause — return interrupt value
    UI->>U: "What would you like on the axes?"
    U->>UI: "net sales by channel"
    UI->>CA: Command(resume="net sales by channel")

    CA->>CP: interrupt("Bar chart would work. Daily, monthly, or yearly?")
    CP-->>UI: Pause — return interrupt value
    UI->>U: "Bar chart would work. Daily, monthly, or yearly?"
    U->>UI: "monthly"
    UI->>CA: Command(resume="monthly")

    Note over CA: Has enough info → returns chart-spec

    CA-->>UI: chart-spec JSON { type: "bar", sql: "SELECT...", x: "month", y: "net_sales" }
    UI->>UI: Execute SQL → Render Plotly chart
    UI->>U: "Here's your monthly net sales by channel!"
```

### Implementation Architecture (Phase 3+)

```mermaid
flowchart LR
    subgraph BIStateBox ["BIState (checkpointed)"]
        MS[messages]
        DF[df_json / last_sql]
        CH[chart_history]
        OQ[original_question]
        PA[plot_accepted]
    end

    subgraph Graph ["LangGraph StateGraph"]
        OP["offer_plot_node<br/>interrupt()"]
        CA2["chart_agent_node<br/>interrupt() for questions<br/>returns full chart_history"]
    end

    MS --> OP
    DF --> OP
    OP -->|"plot_accepted = yes"| CA2
    CH --> CA2
    OQ --> CA2
    CA2 -->|"updated chart_history"| CH
    CA2 -->|"chart_spec"| RENDER["Plotly render<br/>inline in chat"]

    style CA2 fill:#e0e7ff,stroke:#6366f1,color:#000000
    style RENDER fill:#d1fae5,stroke:#10b981,color:#000000
```

### Key Differences: State Machine vs LLM-Driven

| Aspect | Pre-LangGraph State Machine | Phase 3 LangGraph (chart_agent_node) |
|--------|----------------------------|--------------------------------------|
| **Question order** | Fixed: plot? → type → X → Y | Adaptive: LLM decides based on context |
| **# of questions** | Always 3–4 | 0–4 depending on clarity of request |
| **Context awareness** | None — always asks everything | Skips questions when answers are obvious |
| **Pause/resume** | Phase flag in `st.session_state` | `interrupt()` — native LangGraph |
| **State persistence** | Lost on browser refresh | Checkpointed to DB |
| **Extensibility** | Add new steps = change code | Change prompt = change behavior |

---

## 5. Agent Loop Detail — `run_tool_loop()`

The inner loop shared by `QueryAgent` and `VisualizationAgent`. In Phase 6, the `ExecTool` step calls direct DuckDB callables instead of MCP session methods.

```mermaid
flowchart TD
    Entry([Agent.run called]) --> BuildPrompt["Build system prompt<br/>+ tools + user message<br/>(L1: cache_control on system block)"]
    BuildPrompt --> CallClaude["Claude API call<br/>model, system, tools, messages"]

    CallClaude --> CheckStop{stop_reason?}

    CheckStop -->|tool_use| ExtractTools[Extract tool_use blocks]
    ExtractTools --> ExecTool["Execute each tool<br/>Phase 2–5: session.call_tool() via MCP<br/>Phase 6: direct DuckDB callable"]
    ExecTool --> TrackSQL{tool = execute_sql?}
    TrackSQL -->|Yes| SaveSQL["Track last_sql<br/>for DataFrame fetch"]
    TrackSQL -->|No| SkipSQL[Continue]
    SaveSQL --> AppendResult[Append tool_result to messages]
    SkipSQL --> AppendResult
    AppendResult --> CallClaude

    CheckStop -->|end_turn| ReturnText["Return (text, last_sql)"]
    CheckStop -->|max_tokens| ReturnText
    CheckStop -->|unexpected| ReturnError[Return error message]

    ReturnText --> Exit([Back to caller])
    ReturnError --> Exit

    style Entry fill:#e0e7ff,stroke:#6366f1,color:#000000
    style Exit fill:#d1fae5,stroke:#10b981,color:#000000
    style CallClaude fill:#fef3c7,stroke:#f59e0b,color:#000000
    style ExecTool fill:#fee2e2,stroke:#ef4444,color:#000000
```

**Phase 6 migration note:** `run_tool_loop` currently calls `session.call_tool()` on the MCP `ClientSession`. The direct DuckDB migration (ADR-005) requires one of: (a) refactoring `run_tool_loop` to accept a callable dict, (b) a thin `ClientSession` adapter over direct DuckDB, or (c) two separate tool loops. See G9 in CLAUDE.md for the full gap analysis.

---

## 6. Dashboard Rendering Pipeline

```mermaid
flowchart LR
    A["VisualizationAgent<br/>run_tool_loop"] -->|"Claude returns<br/>dashboard-data block"| B[parse_dashboard_response]
    B -->|regex extract JSON| C[render_dashboard]
    C -->|"replace placeholders:<br/>__TITLE__<br/>__DASHBOARD_JSON__<br/>__PALETTE_JSON__<br/>__CHARTJS__"| D["_DASHBOARD_TEMPLATE<br/>(self-contained HTML)"]
    D --> E[save_report]
    E -->|"report_&lt;ts&gt;.html<br/>Phases 1–4: disk only"| F["src/static/reports/"]
    F --> G["Dashboard button<br/>opens in new tab"]

    D --> EP["Phase 5+: publish_report<br/>Push HTML to intranet / Power BI<br/>Return URL"]
    EP -->|"dashboard_url"| BIS["BIState.dashboard_url<br/>(URL only — not raw HTML)"]
    BIS --> G5["Phase 5+ button<br/>opens published URL"]

    style A fill:#e0e7ff,stroke:#6366f1,color:#000000
    style D fill:#fef3c7,stroke:#f59e0b,color:#000000
    style G fill:#d1fae5,stroke:#10b981,color:#000000
    style EP fill:#fce7f3,stroke:#ec4899,color:#000000
    style G5 fill:#d1fae5,stroke:#10b981,color:#000000
```

**Phase 5 note:** `visualization_agent_node` currently calls `save_report()` twice — once inside `VisualizationAgent.run()` and once in the node itself. Phase 5 consolidates this into a single `publish_report()` call that writes the URL directly to `BIState.dashboard_url`.

---

## 7. Schema Pruning Pipeline

```mermaid
flowchart LR
    Q[User question] --> T1

    subgraph Pruning ["3-Tier Schema Optimization"]
        T1["Tier 1<br/>Table Selection<br/>keyword hints"] --> T2["Tier 2<br/>Column Linking<br/>token matching"]
        T2 --> T3["Tier 3<br/>Value Annotation<br/>low-cardinality hints"]
    end

    T3 --> S[Pruned schema DDL<br/>sent to agent]

    style T1 fill:#fef3c7,stroke:#f59e0b,color:#000000
    style T2 fill:#fef3c7,stroke:#f59e0b,color:#000000
    style T3 fill:#fef3c7,stroke:#f59e0b,color:#000000
```

---

## 8. Phase 6: Deployment Architecture

LangGraph Server replaces Streamlit's single-process model. Streamlit becomes a thin HTTP client. Multiple async workers share a single PostgreSQL checkpointer and a read-only DuckDB file.

```mermaid
flowchart TD
    User([Browser / User]) -->|HTTP 8501| SL

    subgraph Docker["docker-compose.yml (Phase 6)"]
        SL["Streamlit UI<br/>Port 8501<br/>langgraph_sdk.get_client()<br/>calls client.runs.stream()"]

        SL -->|"HTTP / SSE streaming\nPort 8000"| LGS

        subgraph LGSBlock["LangGraph Server — Port 8123"]
            LGS["LangGraph Server<br/>langchain/langgraph-api<br/>LANGCHAIN_WORKERS=4"]
            W1["Worker 1"]
            W2["Worker 2"]
            W3["Worker 3"]
            W4["Worker 4"]
            LGS --> W1
            LGS --> W2
            LGS --> W3
            LGS --> W4
        end

        W1 -->|"checkpoint read/write<br/>row-level MVCC"| PG
        W2 -->|"checkpoint read/write"| PG
        W3 -->|"checkpoint read/write"| PG
        W4 -->|"checkpoint read/write"| PG

        W1 -->|"SQL query<br/>read_only=True"| DDB
        W2 -->|"SQL query<br/>read_only=True"| DDB
        W3 -->|"SQL query<br/>read_only=True"| DDB
        W4 -->|"SQL query<br/>read_only=True"| DDB

        PG["PostgreSQL 16<br/>checkpoints table<br/>query_cache table (L4 Tier A)<br/>semantic_query_cache + pgvector (L4 Tier B)"]

        DDB["DuckDB bi.db<br/>read_only=True<br/>Shared via Docker volume<br/>Unlimited concurrent readers (MVCC)"]
    end

    style SL fill:#e0e7ff,stroke:#6366f1,color:#000000
    style LGS fill:#fef3c7,stroke:#f59e0b,color:#000000
    style PG fill:#d1fae5,stroke:#10b981,color:#000000
    style DDB fill:#fee2e2,stroke:#ef4444,color:#000000
```

---

## 9. Multi-Layer Caching Strategy

Six cache layers activated across Phases 2.5–6. Layers are hit in order — earlier layers save more cost and latency.

```mermaid
flowchart TD
    UQ([User Question]) --> L3

    subgraph InProc["In-Process — per worker"]
        L3["L3: Router LRU Cache<br/>────────────────<br/>512-entry dict in graph.py<br/>Key: (message.lower(), has_df)<br/>Hit rate: ~25–40%<br/>Saves: haiku call (~$0.001/hit)"]
    end

    L3 -->|cache miss| RC
    RC["Claude API<br/>claude-haiku-4-5<br/>classify intent"] --> L3

    RC -->|"intent = query/visualization"| L4B

    subgraph SharedPG["Shared PostgreSQL — cross-worker, cross-user"]
        L4B["L4 Tier B: Semantic Cache<br/>────────────────<br/>text-embedding-3-small → pgvector<br/>Cosine similarity ≥ 0.97<br/>Hit rate: ~40–60% at 100 users<br/>Saves: ALL tokens + DuckDB on hit"]
        L4A["L4 Tier A: SQL Exact Cache<br/>────────────────<br/>sqlglot canonicalise → SHA256<br/>PostgreSQL query_cache table<br/>TTL: QUERY_CACHE_TTL (default 3600s)<br/>Saves: DuckDB execution on hit"]
    end

    L4B -->|semantic miss| L4A
    L4A -->|sql miss| L1

    subgraph Anthropic["Anthropic Server-Side Cache"]
        L1["L1: Prompt Cache<br/>────────────────<br/>cache_control: ephemeral<br/>Applies to: QueryAgent + VisualizationAgent<br/>Schema block: 3,500–5,700 tokens<br/>90% input token cost on hit<br/>5-minute TTL<br/>Saves: ~$3,000–4,000/year at 100 users"]
    end

    L1 -->|cache miss| AC
    AC["Claude API<br/>claude-sonnet-4-6<br/>SQL generation"] --> L1

    AC --> L5

    subgraph SharedVol["Shared Volume / Object Storage — Phase 6"]
        L5["L5: df_json Externalised<br/>────────────────<br/>DataFrame → local_data/df_cache/uuid.json<br/>BIState stores path only (~50 bytes)<br/>vs inline JSON (50 KB–2 MB)<br/>Checkpoint size: ~95% reduction"]
    end

    subgraph PerWorker["Per-Worker — DuckDB direct connection"]
        L6["L6: Schema Cache<br/>────────────────<br/>_schema: str in duckdb_connection.py<br/>Fetched once at worker startup<br/>invalidate_schema_cache() on data reload"]
    end

    L3 -->|"any intent"| L6
    AC --> L6

    style L1 fill:#fef3c7,stroke:#f59e0b,color:#000000
    style L3 fill:#e0e7ff,stroke:#6366f1,color:#000000
    style L4A fill:#d1fae5,stroke:#10b981,color:#000000
    style L4B fill:#d1fae5,stroke:#10b981,color:#000000
    style L5 fill:#fce7f3,stroke:#ec4899,color:#000000
    style L6 fill:#fee2e2,stroke:#ef4444,color:#000000
```

**Phase activation:**

| Layer | Activated | Scope |
|-------|-----------|-------|
| L1 Prompt cache | Phase 2.5 | Anthropic server-side |
| L2 Client singleton | Phase 2.5 | Per-process |
| L3 Router LRU | Phase 2.5 | Per-process |
| L4 SQL cache | Phase 5 (in-process TTL) → Phase 6 (PostgreSQL) | Phase 6: cross-worker |
| L5 df_json file | Phase 5 (local disk) → Phase 6 (shared volume) | Phase 6: cross-worker |
| L6 Schema cache | Phase 6 | Per-worker (acceptable) |

---

## 10. Model Tier Split

Every user turn makes two Claude API calls. ADR-009 routes each to the appropriate model based on task complexity.

```mermaid
flowchart LR
    UT([User Turn]) --> RN

    subgraph RouterCall["Call 1 — Classification"]
        RN["router_node<br/>────────────────<br/>claude-haiku-4-5-20251001<br/>max_tokens = 10<br/>Latency: ~200–400 ms<br/>Cost: ~$0.001 / 1,000 calls<br/>Task: 4-label intent classifier"]
    end

    RN -->|intent| AN

    subgraph AgentCall["Call 2 — Agent (one of three)"]
        AN["query_agent_node<br/>visualization_agent_node<br/>chart_agent_node<br/>────────────────<br/>claude-sonnet-4-6<br/>Latency: ~500–800 ms<br/>Cost: ~$3–15 / 1,000 calls<br/>Task: SQL generation / dashboard / chart"]
    end

    subgraph Saving["Annual saving at 100 users"]
        S1["All Sonnet router: ~$80/year<br/>Haiku router: ~$1/year<br/>Saving: ~$79/year router alone<br/>+ ~300–400 ms perceived latency"]
    end

    RN -.->|"env: ROUTER_MODEL"| RM["config.py<br/>ROUTER_MODEL = claude-haiku-4-5-20251001<br/>AGENT_MODEL  = claude-sonnet-4-6"]
    AN -.->|"env: AGENT_MODEL"| RM

    style RN fill:#fef3c7,stroke:#f59e0b,color:#000000
    style AN fill:#e0e7ff,stroke:#6366f1,color:#000000
    style RM fill:#f3f4f6,stroke:#9ca3af,color:#000000
```

---

## 11. Checkpointer Evolution

The checkpointer progresses through three implementations. The interface is identical across all three — only the connection string and import change.

```mermaid
flowchart LR
    P3["Phase 3<br/>MemorySaver<br/>────────────────<br/>In-memory dict<br/>Lost on restart<br/>No DB required<br/>Required for interrupt()"]

    P5["Phase 5<br/>AsyncSqliteSaver<br/>────────────────<br/>local_data/checkpoints.db<br/>Survives restarts<br/>Single process only<br/>WAL mode serialises writes"]

    P6P["Phase 6 (prod)<br/>AsyncPostgresSaver<br/>────────────────<br/>CHECKPOINT_DB_URL env var<br/>Row-level MVCC locking<br/>Shared across all workers<br/>Horizontal scaling enabled"]

    P6D["Phase 6 (dev)<br/>AsyncSqliteSaver<br/>────────────────<br/>Fallback when<br/>CHECKPOINT_DB_URL absent<br/>No PostgreSQL required locally"]

    P3 -->|"Phase 5"| P5
    P5 -->|"Phase 6 prod"| P6P
    P5 -->|"Phase 6 dev"| P6D

    subgraph Code["get_checkpointer() in graph.py"]
        GC["if CHECKPOINT_DB_URL:<br/>    AsyncPostgresSaver<br/>else:<br/>    AsyncSqliteSaver"]
    end

    P6P -.-> GC
    P6D -.-> GC

    style P3 fill:#fee2e2,stroke:#ef4444,color:#000000
    style P5 fill:#fef3c7,stroke:#f59e0b,color:#000000
    style P6P fill:#d1fae5,stroke:#10b981,color:#000000
    style P6D fill:#e0e7ff,stroke:#6366f1,color:#000000
```

---

## 12. Pre-LangGraph: Agent Routing (Legacy)

> **Note:** This diagram shows the **pre-migration architecture** (the hand-built orchestrator in `llm-bi-chat-agentic`). The `OrchestratorAgent` and `AgentRegistry` are replaced by the LangGraph `StateGraph` (see Diagram 1). Kept here as a reference for the migration delta.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'actorTextColor': '#000000', 'noteTextColor': '#000000', 'loopTextColor': '#000000', 'labelTextColor': '#000000', 'actorBkg': '#ffffff', 'actorBorder': '#333333'}}}%%
sequenceDiagram
    actor User
    participant UI as Streamlit UI<br/>(app.py)
    participant Orch as OrchestratorAgent<br/>(orchestrator.py)
    participant Claude as Claude API
    participant Agent as Specialist Agent<br/>(query / visualization)
    participant MCP as DuckDB MCP Server<br/>(duckdb_mcp_server.py)
    participant DB as DuckDB<br/>(bi.db)

    User->>UI: "What were our top 5 products?"
    UI->>UI: Build filter context<br/>(date range + channels)
    UI->>Orch: query(question, context)

    Note over Orch: Sync → async bridge<br/>via MCPConnectionManager

    Orch->>MCP: Spawn subprocess (stdio)
    MCP-->>Orch: Session initialized
    Orch->>MCP: call_tool("get_schema")
    MCP->>DB: SHOW ALL TABLES + DESCRIBE
    DB-->>MCP: DDL schema
    MCP-->>Orch: Schema text (cached)

    rect rgb(240, 245, 255)
        Note over Orch,Claude: Routing — single Claude call
        Orch->>Claude: messages + router tools<br/>[query_agent, visualization_agent]
        Claude-->>Orch: tool_use: query_agent<br/>{ question: "..." }
    end

    Orch->>Agent: agent.run(question, session, schema, context)

    rect rgb(240, 255, 240)
        Note over Agent,DB: Agent Loop — run_tool_loop()
        Agent->>Claude: System prompt + MCP tools + question
        Claude-->>Agent: tool_use: execute_sql<br/>{ sql: "SELECT ..." }
        Agent->>MCP: call_tool("execute_sql", { sql })
        MCP->>DB: SELECT ...
        DB-->>MCP: Result rows
        MCP-->>Agent: Markdown table
        Agent->>Claude: tool_result + continue
        Claude-->>Agent: end_turn + text answer
    end

    Agent-->>Orch: AgentResult(text, data=DataFrame, last_sql)
    Orch-->>UI: AgentResult

    alt QueryAgent returned DataFrame
        UI->>User: Display text answer
        UI->>User: "Would you like me to plot this data?"
        Note over UI,User: State machine chart flow begins<br/>(see Diagram 13 — legacy)
    else VisualizationAgent returned dashboard
        UI->>UI: save_report(html) → disk
        UI->>User: Display summary text
        Note over UI: Dashboard button<br/>opens report in new tab
    end
```

---

## 13. Pre-LangGraph: Charting State Machine (Legacy)

> **Note:** This is the **pre-Phase 3 implementation**. Phase 3 replaces this rigid state machine with `chart_agent_node` + `interrupt()` (see Diagrams 3 and 4). Kept here as a reference for the migration delta.

```mermaid
stateDiagram-v2
    [*] --> QueryResult: QueryAgent returns DataFrame

    QueryResult --> ask_plot: "Would you like to plot this?"

    ask_plot --> ask_chart_type: User says "yes"
    ask_plot --> [*]: User says "no"

    ask_chart_type --> ask_x_axis: User picks chart type
    ask_chart_type --> ask_chart_type: Unrecognized → re-ask

    ask_x_axis --> ask_y_axis: User picks X column
    ask_x_axis --> RenderChart: histogram (no Y needed)

    ask_y_axis --> RenderChart: User picks Y column

    RenderChart --> [*]: Plotly chart rendered inline

    note right of ask_chart_type
        Options: bar, line, pie,
        scatter, area, histogram
    end note
```
