# ADR-001 — Use `SqliteSaver` for LangGraph checkpointing

**Status:** Accepted — amended for 100-user target (see amendment below)

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

---

## Amendment — Phase 6: Use `AsyncPostgresSaver` for 100-user target

**Context of amendment:** The original decision accepted `SqliteSaver` with
the note "Upgrading to `PostgresSaver` later is a one-line change if the team
scales." The 100-user concurrency analysis (Phase 6 planning) determined that
SQLite's file-level write lock will serialise checkpoint writes under concurrent
load, causing queued latency spikes during busy periods. At 100 users each
completing a 3-node graph traversal, checkpoint writes overlap frequently.

**Amendment decision:** Use `AsyncPostgresSaver` from
`langgraph-checkpoint-postgres` for Phase 6 and beyond.

```python
# Phase 3 (temporary — for interrupt() support during development)
from langgraph.checkpoint.memory import MemorySaver
checkpointer = MemorySaver()

# Phase 5 (single-user / dev — acceptable for ≤ 10 users)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
checkpointer = await AsyncSqliteSaver.from_conn_string("local_data/checkpoints.db")

# Phase 6 (production — 100 concurrent users)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
async with AsyncPostgresSaver.from_conn_string(
    os.environ["CHECKPOINT_DB_URL"]
) as checkpointer:
    graph = workflow.compile(checkpointer=checkpointer)
```

**Required env var:** `CHECKPOINT_DB_URL` — a standard PostgreSQL connection
string (e.g. `postgresql://user:pass@host:5432/checkpoints`). The node must
raise a clear error at startup if this variable is missing.

**Why PostgreSQL over SQLite at 100 users:**

| Property | `SqliteSaver` | `AsyncPostgresSaver` |
|----------|--------------|---------------------|
| Concurrent writes | Serialised (file lock) | True parallel (row-level locking) |
| Horizontal scaling | No (file-local) | Yes (shared server) |
| Ops dependency | None | Managed PostgreSQL (RDS, Cloud SQL, Supabase, etc.) |
| Migration effort | — | Add `langgraph-checkpoint-postgres` + env var |

**The original decision remains valid for:**
- Local development
- Single-developer usage (≤ 10 concurrent users)
- Phase 3 and Phase 4 development and testing

`SqliteSaver` (or `AsyncSqliteSaver`) should still be used in development to
avoid requiring a running PostgreSQL instance for local iteration.
