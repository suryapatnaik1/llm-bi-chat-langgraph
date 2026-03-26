# ADR-005 — Replace MCP subprocess with shared DuckDB connection

**Status:** Accepted (Phase 6)

> **Diagram:** [diagrams.md — Diagram 5: Agent Loop (run_tool_loop)](../diagrams/diagrams.md#5-agent-loop-detail--run_tool_loop) · [Diagram 8: Phase 6 Deployment Architecture](../diagrams/diagrams.md#8-phase-6-deployment-architecture)

## Context

`query_agent_node` and `visualization_agent_node` currently access DuckDB by
spawning a new MCP subprocess on every invocation via `execute_with_session()`:

```python
async def query_agent_node(state: BIState) -> dict:
    async def _run(session, schema):
        tools = await _mcp.get_mcp_tools(session)
        return await _query_agent.run(...)
    result = await _mcp.execute_with_session(_run)   # spawns duckdb_mcp_server.py
```

Each call to `execute_with_session()` in `mcp_connection.py`:
1. Resolves `sys.executable` and the MCP server script path
2. Spawns a new OS subprocess (`stdio_client()`)
3. Creates and initialises a `ClientSession`
4. Runs the agent
5. Tears down the subprocess on exit

At 100 concurrent users, this means up to 100 subprocesses spawning
simultaneously, each paying ~200–500 ms process startup cost plus a DuckDB
file open. Under this load the MCP layer dominates query latency and provides
no benefit — the database is owned by the same codebase, not an external service.

The MCP protocol was designed to expose tools from **external** services
(e.g., a remote database server, a SaaS API). Using it as a transport for an
embedded DuckDB file adds subprocess overhead with no architectural benefit.

## Decision

Replace the MCP subprocess with a **shared module-level DuckDB connection**
opened once at process start. Node functions call DuckDB directly via the
Python `duckdb` client.

```python
# src/services/duckdb_connection.py  (new file)
import duckdb
from src.config import DB_PATH

_conn: duckdb.DuckDBPyConnection | None = None

def get_connection() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        _conn = duckdb.connect(str(DB_PATH), read_only=True)
    return _conn

def get_schema() -> str:
    conn = get_connection()
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return "\n".join(r[0] for r in rows if r[0])

def execute_sql(query: str) -> list[dict]:
    conn = get_connection()
    rel = conn.execute(query)
    return rel.df().to_dict(orient="records")
```

`QueryAgent` and `VisualizationAgent` receive these functions as callables via
`context`, replacing the MCP tool list. `MCPConnectionManager` is retained only
for the legacy Streamlit path in `app.py` until Phase 4 removes it.

## Rationale

**DuckDB `read_only=True` supports unlimited concurrent readers:**
DuckDB uses MVCC snapshots for reads. A single shared connection opened with
`read_only=True` handles 100 concurrent analytical queries without lock
contention — the same guarantee that made the MCP approach safe, but without
the subprocess overhead.

**The subprocess cost is pure overhead for an internal database:**
MCP is the correct boundary for external tools. For a file-local DuckDB the
subprocess adds 200–500 ms per invocation, a process table entry, and a DuckDB
file open — none of which are needed when the connection can live in-process.

**Schema caching is simpler:** `get_schema()` becomes a direct DuckDB call
cached in a module-level variable, replacing the `_schema_cache` logic in
`MCPConnectionManager`.

**Agent code barely changes:** `QueryAgent` and `VisualizationAgent` read tools
from `context["mcp_tools"]`. This context key is renamed to `context["db"]` (a
dict of callables) — a localised change confined to the two agent files and the
two node functions.

**`duckdb_mcp_server.py` is retained** as a standalone MCP server for use cases
where an external client needs SQL access (e.g., LangGraph Studio inspection,
future CLI tools). It is no longer called from within the LangGraph graph.

## Migration Steps (Phase 6)

1. Create `src/services/duckdb_connection.py` with `get_connection()`,
   `get_schema()`, `execute_sql()`
2. Update `query_agent_node` and `visualization_agent_node` to call
   `duckdb_connection.get_schema()` and pass callables in `context["db"]`
3. Update `QueryAgent.run()` and `VisualizationAgent.run()` to read
   `context["db"]` instead of `context["mcp_tools"]`
4. Remove `_mcp.execute_with_session(_run)` wrappers from both node functions
5. Keep `MCPConnectionManager` — still used by `app.py` Streamlit path until
   Phase 4 completes

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| Single DuckDB connection is process-local — no horizontal scaling across multiple app servers | Acceptable for Phase 6; MotherDuck or ClickHouse (ADR-006) resolves this |
| `read_only=True` means no writes from the app — dashboard state written to DuckDB would fail | Not needed; only `local_data/bi.db` (BI data) is accessed; checkpoint DB is separate SQLite/PostgreSQL |
| If the DuckDB file is rotated (new data load), the shared connection may hold a stale file handle | Add `invalidate_connection()` called by the data-load script after replacing the file |
| `duckdb_mcp_server.py` is now a standalone server and diverges from the shared connection | Document clearly: MCP server is for external access only; graph uses `duckdb_connection.py` |
