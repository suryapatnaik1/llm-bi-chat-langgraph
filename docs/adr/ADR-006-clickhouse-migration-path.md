# ADR-006 — ClickHouse as database for scale beyond 100 users / large data

**Status:** Accepted — deferred to Phase 7

## Context

The app currently uses DuckDB (`local_data/bi.db`) as its analytical database.
ADR-005 (Phase 6) replaces the MCP subprocess with a shared DuckDB connection,
which is sufficient for 100 concurrent users on a single server with moderate
data volumes.

Two scenarios make DuckDB the wrong long-term choice:

**Scenario A — Data volume exceeds server RAM:**
DuckDB processes data in-process. For datasets that don't fit in RAM, it spills
to disk; under concurrent load this causes IO contention. At >100 GB of BI data
with 100 concurrent analytical queries, query latency degrades unpredictably.

**Scenario B — Multiple app servers:**
DuckDB is a file-based embedded database. It cannot be shared across processes
on different machines. Any horizontal scaling of the app layer (multiple
replicas, LangGraph Server pods) requires all instances to share a database
— DuckDB cannot serve this role.

ClickHouse is a purpose-built OLAP server that handles both scenarios:
- Column-store with vectorised execution — 10–100× faster than DuckDB on large
  aggregations at high concurrency
- Server model — multiple app servers connect to one ClickHouse cluster
- Native HTTP and Python client (`clickhouse-connect`) — no subprocess required
- Mature query queue management, per-query resource limits, and user quotas

## Decision

Defer migration to ClickHouse to **Phase 7**, after the LangGraph migration
(Phases 1–5) and the direct connection refactor (Phase 6) are complete.

The trigger for Phase 7 is any of:
- Data volume in `bi.db` exceeds ~50 GB
- p95 query latency under load exceeds 5 seconds
- Horizontal scaling of app servers is required

## What Changes

### Connection layer (`src/services/duckdb_connection.py` → `src/services/db_connection.py`)

```python
# Before (ADR-005)
import duckdb
_conn = duckdb.connect(str(DB_PATH), read_only=True)

# After (Phase 7)
import clickhouse_connect
_client = clickhouse_connect.get_client(
    host=os.environ["CH_HOST"],
    port=int(os.environ.get("CH_PORT", 8123)),
    username=os.environ["CH_USER"],
    password=os.environ["CH_PASSWORD"],
    database=os.environ["CH_DATABASE"],
)
```

### Agent system prompts

Both `QueryAgent` and `VisualizationAgent` receive the SQL dialect in their
system prompt. A one-line change enables ClickHouse SQL generation:

```python
# Before
"Write a precise DuckDB SELECT query..."

# After
"Write a precise ClickHouse SELECT query. Use ClickHouse date functions:
toStartOfMonth(), toYear(), toMonth(), dateDiff(). Avoid DuckDB-only syntax
such as DATE_TRUNC, EXTRACT, and INTERVAL literals."
```

Claude has strong ClickHouse SQL knowledge and adapts automatically from the
updated prompt. No changes are required to `run_tool_loop` or agent logic.

### Schema introspection

```python
# Before (DuckDB)
conn.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall()

# After (ClickHouse)
client.query("SHOW CREATE TABLE {table}").result_rows
```

### Data load (`scripts/prepare_bi_data.py`)

The data load script must be updated to write to ClickHouse using the
`MergeTree` table engine. This is a one-time migration step; the schema
(orders, order_lines, items) is unchanged.

```sql
-- ClickHouse DDL (replacing DuckDB CREATE TABLE)
CREATE TABLE orders (
    original_reference  String,
    created_date        Date,
    channel             String,
    net_sales           Float64,
    total_sales         Float64,
    gross_margin        Float64
) ENGINE = MergeTree()
ORDER BY (created_date, channel);
```

## SQL Dialect Gaps to Validate

Because SQL is LLM-generated, dialect gaps surface as runtime errors or silent
wrong results rather than build failures. The following patterns must be tested
against the interaction taxonomy in `bi-chat-interactions.md` before Phase 7
is declared complete:

| Pattern | DuckDB | ClickHouse | Risk |
|---------|--------|------------|------|
| Date truncation | `DATE_TRUNC('month', d)` | `toStartOfMonth(d)` | Claude may mix dialects |
| Date arithmetic | `d + INTERVAL 30 DAY` | `d + toIntervalDay(30)` | Common in trend queries |
| Year/month extract | `EXTRACT(YEAR FROM d)` | `toYear(d)` | Common in time-series |
| Large JOIN | Hash join, automatic | Right side must fit in memory | Orders × order_lines at scale |
| `GROUP BY` strictness | Strict (error on unaggregated cols) | Permissive (picks arbitrary value) | Silent wrong results |
| NULL sort order | NULLs last by default | NULLs first by default | Affects top-N queries |

**Validation approach:** run all 6 interaction types and all 6 multi-turn
patterns from `bi-chat-interactions.md` against both DuckDB and ClickHouse
in parallel, diffing results before cutting over.

## Alternatives Considered

| Alternative | Why not chosen now |
|-------------|-------------------|
| **MotherDuck** (managed DuckDB) | Same DuckDB SQL dialect — zero prompt changes; resolves multi-server sharing; best choice if DuckDB performance is adequate but horizontal scaling is needed | Deferred — add as intermediate step before ClickHouse if data stays < 100 GB |
| **PostgreSQL** | Battle-tested, excellent concurrency, but 5–50× slower than ClickHouse on GROUP BY / window functions over millions of rows | Wrong engine for OLAP workload |
| **Snowflake / BigQuery** | Unlimited scale, but high operational overhead, cost unpredictability, and vendor lock-in | Premature — revisit if ClickHouse becomes insufficient |

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| ClickHouse requires a running server — local dev needs Docker | Add `docker-compose.yml` with ClickHouse service; document in README |
| ClickHouse is not drop-in SQL compatible with DuckDB | Comprehensive dialect testing before cutover (see table above) |
| MergeTree table engine is append-optimised — updates/deletes are expensive | Acceptable; BI data is read-only after initial load |
| ClickHouse HTTP client adds network latency vs in-process DuckDB | < 5 ms on localhost; negligible compared to query time |
