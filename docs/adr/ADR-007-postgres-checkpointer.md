# ADR-007 — AsyncPostgresSaver for production checkpointing at 100 users

**Status:** Accepted (Phase 6)

> **Diagram:** [diagrams.md — Diagram 11: Checkpointer Evolution](../diagrams/diagrams.md#11-checkpointer-evolution) · [Diagram 8: Phase 6 Deployment Architecture](../diagrams/diagrams.md#8-phase-6-deployment-architecture)

## Context

ADR-001 chose `SqliteSaver` for LangGraph checkpointing and noted: "Upgrading to
`PostgresSaver` later is a one-line change if the team scales." The ADR-001
amendment confirmed this upgrade is needed at 100 concurrent users. This ADR
captures the full decision, rationale, and migration detail as a standalone record.

LangGraph checkpointing writes serialised `BIState` blobs to persistent storage
at the end of every node execution. For a 3-node graph traversal (router →
query_agent → offer_plot), each user turn produces 3 checkpoint writes.

At 100 concurrent users with typical BI session cadence:
- Peak: ~50 checkpoint writes/second
- SQLite WAL mode serialises concurrent writers at the file level —
  writes queue behind each other, adding latency to every graph node return
- A single slow checkpoint write (e.g., large `df_json` blob) blocks all
  other users' checkpoint writes for that moment

SQLite's limitation is not correctness — WAL mode prevents corruption — it is
latency: writes are serialised, not parallel.

## Decision

Use `AsyncPostgresSaver` from `langgraph-checkpoint-postgres` as the production
checkpointer. Retain `AsyncSqliteSaver` for local development to avoid requiring
a running PostgreSQL instance during iteration.

```python
# src/graph/graph.py

import os
from contextlib import asynccontextmanager

def get_checkpointer():
    """
    Returns the appropriate checkpointer based on environment.
    Dev (no CHECKPOINT_DB_URL): AsyncSqliteSaver
    Production (CHECKPOINT_DB_URL set): AsyncPostgresSaver
    """
    db_url = os.environ.get("CHECKPOINT_DB_URL")
    if db_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        return AsyncPostgresSaver.from_conn_string(db_url)
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        return AsyncSqliteSaver.from_conn_string("local_data/checkpoints.db")
```

**Required env var:** `CHECKPOINT_DB_URL` — a standard PostgreSQL connection
string (`postgresql+asyncpg://user:pass@host:5432/checkpoints`). If missing,
the app falls back to SQLite. Do not fail at startup if missing — allow dev
usage without PostgreSQL.

**Schema initialisation:** `AsyncPostgresSaver` creates its own tables on first
use via `checkpointer.setup()`. Call this once at app startup.

## Rationale

**SQLite serialises writes; PostgreSQL parallelises them:**
PostgreSQL uses row-level locking (MVCC). Each `thread_id` (one per user) is an
independent row in the checkpoints table. 100 concurrent users writing 100
different `thread_id` rows do not block each other.

**PostgreSQL enables horizontal scaling:**
When the app runs on multiple servers (LangGraph Server pods, multiple uvicorn
workers — see ADR-008), all instances must share checkpoint state so that a user's
conversation can be resumed by any server. SQLite is file-local; PostgreSQL is
a shared server. This is the stronger argument: even if write contention were
acceptable, SQLite cannot serve a multi-server deployment.

**The migration is genuinely a one-line swap in production config:**
The `AsyncPostgresSaver` API is identical to `AsyncSqliteSaver`. The only change
is the connection string and import.

**Managed PostgreSQL is not disproportionate overhead at 100 users:**
The original ADR-001 rejected PostgreSQL as "disproportionate to the team size."
At 100 users this calculus changes — managed PostgreSQL (RDS, Cloud SQL, Supabase,
Railway) is a single configuration item, not a DBA workload.

## Checkpoint Size and Storage Estimate

| Field | Typical size | Notes |
|-------|-------------|-------|
| `messages` (10 turns) | ~5 KB | Compressed by PostgreSQL TOAST for blobs > 2 KB |
| `df_json` | 10–500 KB | Largest field; consider capping at Phase 5 |
| `chart_history` | ~2 KB | After ADR-003 fix |
| Other fields | < 1 KB | |
| **Total per checkpoint** | **~20 KB average** | |

At 100 users × 10 turns × 3 nodes = 3,000 checkpoint writes/day.
With 30-day retention: ~1.8 GB. Negligible on any managed PostgreSQL tier.

## Migration Steps (Phase 6)

1. Add `langgraph-checkpoint-postgres` to `pyproject.toml`
2. Add `asyncpg` driver (`poetry add asyncpg`)
3. Implement `get_checkpointer()` as above in `graph.py`
4. Call `await checkpointer.setup()` once at graph compile time
5. Pass `CHECKPOINT_DB_URL` in production environment (`.env`, Kubernetes secret, etc.)
6. Provision a `checkpoints` database on managed PostgreSQL (separate from BI data)

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| `asyncpg` driver requires compiled C extension — adds to Docker image size | ~2 MB; acceptable |
| Connection pool exhaustion if too many concurrent graph invocations | Set `max_size` on `asyncpg` pool; LangGraph Server manages this automatically (see ADR-008) |
| PostgreSQL adds a network round-trip per checkpoint write (~1–5 ms) | Negligible vs agent query time (hundreds of ms to seconds) |
| `df_json` blobs can be large — TOAST storage handles transparently but affects backup size | Cap `df_json` at Phase 5 using a file reference instead of inline JSON |
| Checkpoint pruning must be scheduled manually | `DELETE FROM checkpoints WHERE thread_ts < now() - interval '30 days'` — add as a cron job or pg_cron rule |
