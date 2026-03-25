# ADR-001 — Use `SqliteSaver` for LangGraph checkpointing

**Status:** Accepted

## Context

LangGraph requires a checkpointer to persist graph state between turns (human-in-the-loop pauses, app restarts, multi-user isolation via `thread_id`). The app already runs DuckDB for all BI queries. The candidate options were:

| Option | Notes |
|--------|-------|
| `MemorySaver` | Zero setup, but state is lost on restart — unacceptable for production |
| `SqliteSaver` | Built into LangGraph, zero extra dependencies, concurrent-write safe (WAL mode) |
| DuckDB (custom) | Would reuse the existing DB file, but DuckDB is single-writer — the checkpointer and BI queries would contend for the write lock |
| `PostgresSaver` | Production-grade, but adds an ops dependency (managed Postgres) disproportionate to the team size |

## Decision

Use `SqliteSaver` with a dedicated file (`local_data/checkpoints.db`), separate from the DuckDB BI database.

```python
from langgraph.checkpoint.sqlite import SqliteSaver
checkpointer = SqliteSaver.from_conn_string("local_data/checkpoints.db")
```

## Rationale

- DuckDB is an OLAP engine with a single-writer model; mixing checkpoint writes with live BI queries would cause lock contention.
- `SqliteSaver` is purpose-built for this use case, ships with LangGraph, and handles concurrent access via WAL mode.
- Checkpoint data (serialised `BIState` blobs) is tiny compared to BI data — there is no benefit to co-locating it.
- Upgrading to `PostgresSaver` later is a one-line change if the team scales.

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| No automatic checkpoint pruning — DB grows unbounded | Add a scheduled cleanup job: `DELETE FROM checkpoints WHERE created_at < datetime('now', '-30 days')` |
| Single SQLite file is not horizontally scalable | Acceptable for ≤ 100 concurrent users; migrate to `PostgresSaver` if traffic grows |
| SQLite WAL mode may show stale reads under very high write throughput | Not a concern at current team size |
| Large `df_json` blobs inflate checkpoint size | `dashboard_html` removed from `BIState`; `df_json` should be capped or replaced with a file reference if query results are large |

## Checkpoint Size Estimate (100 users, moderate BI usage)

- ~15 KB average checkpoint after removing `dashboard_html`
- ~65 MB/day, ~2 GB/month at steady state with 30-day cleanup
