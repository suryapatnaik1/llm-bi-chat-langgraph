# ADR-008 — LangGraph Server as deployment target for 100 concurrent users

**Status:** Accepted (Phase 6)

> **Diagram:** [diagrams.md — Diagram 8: Phase 6 Deployment Architecture](../diagrams/diagrams.md#8-phase-6-deployment-architecture) · [Diagram 2: End-to-End Request Flow](../diagrams/diagrams.md#2-end-to-end-request-flow-phase-6)

## Context

The app is currently deployed as a Streamlit application (`src/app.py`). Streamlit
runs as a single Python process: all user sessions share one event loop and one
GIL. This works for single-user or low-concurrency use, but has two structural
problems at 100 users:

**Problem 1 — CPU-bound work blocks all users:**
Streamlit is not purely I/O-bound. Several operations block the event loop:
- `polars.DataFrame.write_json()` — CPU-bound serialisation of query results
- `parse_dashboard_response()` / `render_dashboard()` — string processing + HTML templating
- `graph.ainvoke()` return value unpacking

When one user's graph invocation triggers Polars JSON serialisation on a large
DataFrame, all other users' async coroutines pause until the GIL is released.
`asyncio` does not protect against synchronous CPU work.

**Problem 2 — Streamlit session state is process-local:**
Horizontal scaling (multiple Streamlit replicas behind a load balancer) requires
all replicas to share `thread_id`-keyed checkpoint state. The LangGraph
checkpointer (PostgreSQL, ADR-007) handles this correctly — but `st.session_state`
is process-local. Sticky sessions (affinity routing) work around this but add
infrastructure complexity and prevent clean failover.

### Options considered

| Option | Description |
|--------|-------------|
| **A — LangGraph Server (self-hosted)** | Dedicated async API server purpose-built for multi-tenant graph execution; Streamlit becomes a thin HTTP client |
| **B — FastAPI + multiple uvicorn workers** | Multiple OS processes each with a separate GIL; graph runs inside each worker; shared PostgreSQL checkpointer ties state together |
| **C — Streamlit + sticky sessions** | Keep Streamlit; add nginx/ALB sticky routing so each user always hits the same replica |
| **D — Celery / task queue** | Graph invocations become async tasks; Streamlit polls for results |

## Decision

Use **LangGraph Server (self-hosted)** as the production backend for Phase 6.
Streamlit is retained as the frontend, calling the LangGraph Server HTTP API
instead of invoking the graph directly.

```
┌──────────────────────────────────────┐
│   Streamlit UI (app.py)              │
│   calls LangGraph Server HTTP API    │
│   via langgraph_sdk.get_client()     │
└──────────────┬───────────────────────┘
               │  HTTP / SSE streaming
               ▼
┌──────────────────────────────────────┐
│   LangGraph Server                   │
│   (Docker, multiple async workers)   │
│                                      │
│   graph.py — StateGraph              │
│   thread_id isolation per user       │
│   streaming node events via SSE      │
└──────────────┬───────────────────────┘
               │
        ┌──────┴──────┐
        │  PostgreSQL  │   ← ADR-007
        │ (checkpoints)│
        └─────────────┘
```

**Streamlit client change** (`app.py`):

```python
# Before (Phase 4 — direct invocation)
result = await graph.ainvoke({"messages": [HumanMessage(content=question)]},
                              config={"configurable": {"thread_id": thread_id}})

# After (Phase 6 — LangGraph Server client)
from langgraph_sdk import get_client
client = get_client(url=os.environ["LANGGRAPH_SERVER_URL"])

async for chunk in client.runs.stream(
    thread_id,
    assistant_id="bi-graph",
    input={"messages": [{"role": "user", "content": question}]},
    stream_mode="values",
):
    # render incremental state updates
```

## Rationale

**LangGraph Server solves the GIL problem natively:**
LangGraph Server runs multiple async worker processes (configurable via
`LANGCHAIN_WORKERS` env var). Each worker has its own GIL. CPU-bound graph work
in one worker does not block users in other workers.

**Thread isolation is built in:**
Each `thread_id` maps to an isolated graph execution context. LangGraph Server
manages thread routing, preventing cross-user state contamination without
sticky sessions.

**Streaming is native:**
LangGraph Server streams node-level events over SSE. The Streamlit UI can render
partial results (router intent, query result, then chart) as they arrive, rather
than waiting for the full graph traversal. This is qualitatively better UX at
any scale.

**Checkpointer integrates directly:**
LangGraph Server manages the checkpointer connection pool itself. ADR-007's
`AsyncPostgresSaver` is configured once in the server's `langgraph.json` — not
in application code.

**Avoids sticky session complexity (Option C):**
Sticky sessions require load balancer configuration, break cleanly on server
restart, and do not survive rolling deployments. Shared PostgreSQL checkpointing
with stateless app servers is the standard cloud-native pattern.

**FastAPI (Option B) is a valid fallback:**
If LangGraph Server introduces operational friction (licensing, Docker image
size, unfamiliar deployment model), FastAPI with `uvicorn --workers N` achieves
the same multi-process isolation. The tradeoff is that streaming and
thread-routing must be re-implemented. FastAPI remains the recommended fallback
if LangGraph Server is ruled out.

## Deployment Configuration

```yaml
# docker-compose.yml (Phase 6)
services:
  langgraph-server:
    image: langchain/langgraph-api:latest
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - CHECKPOINT_DB_URL=${CHECKPOINT_DB_URL}
      - LANGCHAIN_WORKERS=4
    volumes:
      - ./src:/app/src
      - ./local_data:/app/local_data
    ports:
      - "8123:8000"

  streamlit:
    build: .
    environment:
      - LANGGRAPH_SERVER_URL=http://langgraph-server:8000
    ports:
      - "8501:8501"
    depends_on:
      - langgraph-server

  postgres:
    image: postgres:16
    environment:
      - POSTGRES_DB=checkpoints
      - POSTGRES_USER=${PG_USER}
      - POSTGRES_PASSWORD=${PG_PASSWORD}
```

## Migration Steps (Phase 6)

1. Add `langgraph-sdk` to `pyproject.toml` (client library)
2. Create `langgraph.json` at repo root pointing to `src/graph/graph.py`
3. Update `app.py` to call `client.runs.stream()` instead of `graph.ainvoke()`
4. Remove direct graph import from `app.py`
5. Move `ANTHROPIC_API_KEY` and `CHECKPOINT_DB_URL` to server environment
6. Write `docker-compose.yml` with server + Streamlit + PostgreSQL services

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| LangGraph Server adds a Docker dependency for local dev | Keep `poetry run streamlit run src/app.py` (direct graph invocation) as the dev path; only Phase 6 deployment uses the server |
| SSE streaming requires Streamlit's `st.write_stream` or manual chunk handling | Pattern is established in Streamlit docs; Phase 4 already plans streaming support |
| LangGraph Server is open-source but the managed cloud version (LangGraph Cloud) has its own pricing | Self-hosted server is MIT-licensed; cloud version is opt-in |
| Multiple uvicorn workers all open a shared DuckDB `read_only` connection — the connection is process-local | Each worker process opens its own `read_only` DuckDB connection; DuckDB supports multiple simultaneous `read_only` connections from different processes to the same file |
