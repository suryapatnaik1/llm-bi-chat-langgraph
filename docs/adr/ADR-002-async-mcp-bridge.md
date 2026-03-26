# ADR-002 — Async MCP bridge pattern for LangGraph nodes

**Status:** Accepted

> **Diagram:** [diagrams.md — Diagram 12: Pre-LangGraph Agent Routing (Legacy)](../diagrams/diagrams.md#12-pre-langgraph-agent-routing-legacy) · [Diagram 5: Agent Loop](../diagrams/diagrams.md#5-agent-loop-detail--run_tool_loop)

## Context

`MCPConnectionManager` was written to bridge **synchronous Streamlit** with
**asynchronous MCP**. It creates a dedicated background event loop in a daemon
thread and exposes a blocking `run(coro)` method:

```python
def run(self, coro) -> Any:
    future = asyncio.run_coroutine_threadsafe(coro, self._loop)
    return future.result(timeout=120)
```

Streamlit's main thread calls `mcp_manager.run(execute_with_session(fn))` and
blocks until the coroutine completes on the managed loop.

When implementing LangGraph node functions for Phase 2, two approaches were
considered for how the nodes would interact with MCP:

| Option | Description |
|--------|-------------|
| **A — Use `mcp_manager.run()` (sync bridge)** | Node functions are synchronous; call `mcp_manager.run(execute_with_session(fn))` exactly as the existing Streamlit code does |
| **B — `await` directly (async nodes)** | Node functions are `async`; call `await _mcp.execute_with_session(fn)` directly in LangGraph's event loop, bypassing the sync bridge entirely |

## Decision

Use **Option B**: `query_agent_node` and `visualization_agent_node` are `async`
functions that `await _mcp.execute_with_session(fn)` directly.

```python
async def query_agent_node(state: BIState) -> dict:
    async def _run(session, schema):
        tools = await _mcp.get_mcp_tools(session)
        return await _query_agent.run(..., context={..., "mcp_tools": tools})
    result = await _mcp.execute_with_session(_run)
    ...
```

The `MCPConnectionManager` singleton is still created at module level (preserving
the `_schema_cache` across calls), but its background loop thread sits idle
during LangGraph execution — only the Streamlit path in `app.py` uses `run()`.

## Rationale

**Option A (sync bridge) has a fundamental problem:** calling `future.result()`
(a blocking wait) from inside an `async` function running on LangGraph's event
loop would block that loop entirely, preventing any other coroutine from
progressing. This is the standard "blocking call in async context" deadlock.

**Option B works because:**
- `execute_with_session` is a plain `async` coroutine — it runs correctly on
  whichever event loop `await`s it.
- LangGraph natively supports `async` node functions and manages the event loop
  itself.
- The two event loops (LangGraph's and `MCPConnectionManager`'s) are independent;
  the background loop being idle is harmless.

**Schema caching is preserved:** `MCPConnectionManager._schema_cache` is
populated on the first `execute_with_session` call regardless of which code
path invokes it. Subsequent calls across both the LangGraph and Streamlit paths
reuse the cached schema.

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| `MCPConnectionManager` starts a background thread even when only used via async path — a small amount of wasted overhead | Acceptable; thread is lightweight and schema cache justifies the singleton |
| Two code paths (LangGraph async, Streamlit sync) share the same `_schema_cache` — a stale cache after a schema migration would affect both | Schema is DDL-stable for this app; add `invalidate_schema_cache()` method if schema changes become common |
| Not stress-tested under concurrent LangGraph invocations | Multiple users each get their own `graph.invoke()` call, each spawning a separate MCP subprocess via `execute_with_session` — contention is at the DuckDB read-only level, not the event loop level |
