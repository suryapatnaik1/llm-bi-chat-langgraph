# Architecture Diagrams

## 1. Agent Routing — Sequence Diagram

Shows the full lifecycle of a user question, from chat input through orchestrator routing to specialist agent execution and back.

```mermaid
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
        Note over UI,User: Conversational chart flow begins<br/>(see Diagram 3)
    else VisualizationAgent returned dashboard
        UI->>UI: save_report(html) → disk
        UI->>User: Display summary text
        Note over UI: Dashboard button<br/>opens report in new tab
    end
```

## 2. Chat Interaction — Current Charting State Machine

The current implementation uses a step-by-step state machine to gather chart parameters from the user.

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

## 3. Proposed Pattern: LLM-Driven Conversational Chart Generation

Instead of a rigid state machine with fixed steps, the **LLM itself decides what questions to ask** based on the user's input and the DataFrame schema. The questions are context-aware and adaptive.

### Why LLM-Driven?

The current state machine always asks the same 4 questions in the same order, regardless of context. But:
- _"Plot net sales by channel as a bar chart"_ — needs **zero** clarifications
- _"Show me a chart"_ — needs to know axes, chart type, aggregation
- _"Revenue over time"_ — needs aggregation level (daily/monthly/yearly) but chart type is obvious (line)

The LLM evaluates what's **already known** vs **what's missing** and asks only what's needed.

### Conversation Flow

```mermaid
flowchart TD
    A[QueryAgent returns DataFrame] --> B["UI: Would you like me to plot this data?"]
    B -->|No| Z[Done — text answer only]
    B -->|Yes| E["Activate ChartAgent conversation<br/>Pass: df_schema, column types, sample values"]

    E --> F{"ChartAgent (LLM) evaluates:<br/>What information is missing?"}

    F -->|Axes unclear| G["What would you like on the axes?<br/>e.g., revenue over time, or sales by region"]
    F -->|Chart type unclear| H["What kind of chart?<br/>bar, line, pie, scatter, area"]
    F -->|Time column detected| I["What aggregation level?<br/>hourly, daily, monthly, yearly"]
    F -->|Multiple metrics possible| J["Compare with another metric?<br/>Apply any filters?"]
    F -->|Everything clear| K["Generate chart-spec JSON"]

    G --> L[User answers]
    H --> L
    I --> L
    J --> L

    L --> F

    K --> M[Execute SQL from chart-spec]
    M --> N["Render Plotly chart inline in chat"]
    N --> O["Anything you'd like to adjust?"]
    O -->|Yes| F
    O -->|No| Z

    style E fill:#e0e7ff,stroke:#6366f1
    style F fill:#fef3c7,stroke:#f59e0b
    style K fill:#d1fae5,stroke:#10b981
    style N fill:#d1fae5,stroke:#10b981
```

### Multi-Turn Sequence

```mermaid
sequenceDiagram
    participant U as User
    participant UI as Streamlit
    participant CA as ChartAgent (LLM)
    participant DB as DuckDB

    Note over UI: QueryAgent already returned a DataFrame<br/>User said "yes" to plotting

    UI->>CA: df_schema + column info + conversation history
    Note over CA: System prompt:<br/>"Ask ONE question at a time.<br/>When ready, return chart-spec JSON."

    CA-->>UI: "What would you like on the axes?<br/>For example: revenue over time, or sales by region."
    UI->>U: Display question
    U->>UI: "net sales by channel"
    UI->>CA: history + user reply

    CA-->>UI: "A bar chart would work well here.<br/>Do you want daily, monthly, or yearly aggregation?"
    UI->>U: Display question
    U->>UI: "monthly"
    UI->>CA: history + user reply

    CA-->>UI: "Would you like to compare with another<br/>metric like total_cost or gross_margin?"
    UI->>U: Display question
    U->>UI: "no just net sales"
    UI->>CA: history + user reply

    Note over CA: Has enough info → returns chart-spec

    CA-->>UI: chart-spec JSON { type: "bar", sql: "SELECT...", x: "month", y: "net_sales" }

    UI->>DB: Execute SQL from chart-spec
    DB-->>UI: Result DataFrame
    UI->>UI: Render Plotly chart inline
    UI->>U: "Here's your monthly net sales by channel!"
```

### Implementation Architecture

```mermaid
flowchart LR
    subgraph SessionState ["st.session_state"]
        MS[messages]
        CC["chart_conversation<br/>{active, df_schema, history}"]
    end

    subgraph Routing ["Message Routing (app.py)"]
        R{chart_conversation<br/>active?}
        R -->|Yes| CA["ChartAgent<br/>(multi-turn LLM)"]
        R -->|No| O["Orchestrator<br/>(single Claude call)"]
    end

    subgraph ChartAgent ["ChartAgent Logic"]
        SYS["System prompt includes:<br/>- DataFrame schema + types<br/>- Available columns + samples<br/>- 'Ask ONE question at a time'<br/>- 'Return chart-spec when ready'"]
        HIST[Full conversation<br/>history preserved<br/>across turns]
        OUT{Output contains<br/>chart-spec?}
        OUT -->|No| ASK["Return question text<br/>→ continue conversation"]
        OUT -->|Yes| RENDER["Parse chart-spec<br/>→ execute SQL<br/>→ Plotly render"]
    end

    MS --> R
    CC --> R
    CA --> SYS
    CA --> HIST

    style CA fill:#e0e7ff,stroke:#6366f1
    style RENDER fill:#d1fae5,stroke:#10b981
```

### ChartAgent System Prompt (Conceptual)

The ChartAgent receives a system prompt like:

```
You are a chart-building assistant helping the user visualize data.

Available DataFrame:
  Columns: channel (VARCHAR), created_date (DATE), net_sales (FLOAT), total_cost (FLOAT)
  Sample values: channel = ['Shopify-UK', 'Amazon', 'TikTok'], ...
  Row count: 847

Your job:
1. Understand what the user wants to visualize
2. Ask ONE clarifying question at a time (only if needed)
3. Consider asking about:
   - What should be on the axes (if unclear from context)
   - What chart type (if not already stated or obvious)
   - Time aggregation level (if a date column is involved)
   - Comparisons or filters (if multiple metrics exist)
4. When you have enough info, return a ```chart-spec JSON block:

{
  "chart_type": "bar|line|pie|scatter|area|histogram",
  "title": "Chart title",
  "x_column": "column_name",
  "y_column": "column_name",
  "x_label": "Human-readable X label",
  "y_label": "Human-readable Y label",
  "sql": "SELECT ... (optional: if aggregation/transform needed)"
}

IMPORTANT: If the user's request is specific enough, skip unnecessary questions
and go straight to chart-spec. Don't ask 4 questions when 1 will do.
```

### Key Differences: State Machine vs LLM-Driven

| Aspect | Current State Machine | LLM-Driven Pattern |
|--------|----------------------|-------------------|
| **Question order** | Fixed: plot? → type → X → Y | Adaptive: LLM decides based on context |
| **# of questions** | Always 3-4 | 0-4 depending on clarity of request |
| **Context awareness** | None — always asks everything | Skips questions when answers are obvious |
| **Aggregation** | Not asked | Asked when time columns detected |
| **Comparisons** | Not supported | LLM can suggest comparing metrics |
| **Error recovery** | Rigid retry on same step | Natural conversation, LLM adapts |
| **Extensibility** | Add new steps = change code | Change prompt = change behavior |

## 4. Agent Loop Detail — `run_tool_loop()`

Shows the inner loop that both QueryAgent and VisualizationAgent share.

```mermaid
flowchart TD
    Entry([Agent.run called]) --> BuildPrompt[Build system prompt<br/>+ MCP tools + user message]
    BuildPrompt --> CallClaude[Claude API call<br/>model, system, tools, messages]

    CallClaude --> CheckStop{stop_reason?}

    CheckStop -->|tool_use| ExtractTools[Extract tool_use blocks]
    ExtractTools --> ExecTool[Execute each tool via MCP]
    ExecTool --> TrackSQL{tool = execute_sql?}
    TrackSQL -->|Yes| SaveSQL[Track last_sql<br/>for DataFrame fetch]
    TrackSQL -->|No| SkipSQL[Continue]
    SaveSQL --> AppendResult[Append tool_result to messages]
    SkipSQL --> AppendResult
    AppendResult --> CallClaude

    CheckStop -->|end_turn| ReturnText["Return (text, last_sql)"]
    CheckStop -->|max_tokens| ReturnText
    CheckStop -->|unexpected| ReturnError[Return error message]

    ReturnText --> Exit([Back to caller])
    ReturnError --> Exit

    style Entry fill:#e0e7ff,stroke:#6366f1
    style Exit fill:#d1fae5,stroke:#10b981
    style CallClaude fill:#fef3c7,stroke:#f59e0b
    style ExecTool fill:#fee2e2,stroke:#ef4444
```

## 5. Dashboard Rendering Pipeline

```mermaid
flowchart LR
    A[VisualizationAgent<br/>run_tool_loop] -->|"Claude returns<br/>```dashboard-data block"| B[parse_dashboard_response]
    B -->|regex extract JSON| C[render_dashboard]
    C -->|"replace placeholders:<br/>__TITLE__<br/>__DASHBOARD_JSON__<br/>__PALETTE_JSON__<br/>__CHARTJS__"| D["_DASHBOARD_TEMPLATE<br/>(self-contained HTML)"]
    D --> E[save_report]
    E -->|"report_&lt;ts&gt;.html"| F["src/static/reports/"]
    F --> G["📋 Dashboard button<br/>opens in new tab"]

    style A fill:#e0e7ff,stroke:#6366f1
    style D fill:#fef3c7,stroke:#f59e0b
    style G fill:#d1fae5,stroke:#10b981
```

## 6. Schema Pruning Pipeline

```mermaid
flowchart LR
    Q[User question] --> T1

    subgraph Pruning ["3-Tier Schema Optimization"]
        T1["Tier 1<br/>Table Selection<br/>keyword hints"] --> T2["Tier 2<br/>Column Linking<br/>token matching"]
        T2 --> T3["Tier 3<br/>Value Annotation<br/>low-cardinality hints"]
    end

    T3 --> S[Pruned schema DDL<br/>sent to agent]

    style T1 fill:#fef3c7,stroke:#f59e0b
    style T2 fill:#fef3c7,stroke:#f59e0b
    style T3 fill:#fef3c7,stroke:#f59e0b
```
