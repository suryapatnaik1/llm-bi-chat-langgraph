# ADR-010 — Multi-layer caching strategy for performance and cost

**Status:** Accepted (Layers 1–3 in Phase 2.5; Layers 4–6 in Phase 5–6)

> **Diagram:** [diagrams.md — Diagram 9: Multi-Layer Caching Strategy](../diagrams/diagrams.md#9-multi-layer-caching-strategy) · [Diagram 2: End-to-End Request Flow](../diagrams/diagrams.md#2-end-to-end-request-flow-phase-6)

## Context

Each user turn triggers multiple expensive operations:

| Operation | Current cost | Frequency |
|-----------|-------------|-----------|
| Router Claude call | ~400 input tokens | Every turn |
| QueryAgent Claude call | ~3,500–5,500 input tokens (schema dominates) | Most turns |
| VisualizationAgent Claude call | ~3,700–5,700 input tokens | Dashboard turns |
| ChartAgent Claude call | ~3,600–6,050 input tokens | Chart turns |
| `_fetch_df()` DuckDB re-execution | 1 extra SQL query + connection open/close | Every QueryAgent turn |
| `get_async_client()` | 1 new `httpx.AsyncClient` instance | Every Claude call |

At 100 users × 10 turns/day = 1,000 turns/day, the compounded cost of re-sending
identical system prompts and re-executing queries is significant. Six independent
caching layers address different parts of this cost, each with a different
effort-to-ROI profile.

---

## Decision

Implement caching in six layers, phased by effort and dependency:

| Layer | What is cached | Where | Phase |
|-------|---------------|-------|-------|
| L1 | Anthropic server-side prompt cache | API call flags | 2.5 |
| L2 | `AsyncAnthropic` client singleton | `config.py` | 2.5 |
| L3 | Router intent LRU cache | `graph.py` | 2.5 |
| L4 | SQL result TTL cache | `services/` | 5 |
| L5 | `df_json` file reference (checkpoint size) | `graph.py`, `state.py` | 5 |
| L6 | Schema cache in direct DuckDB path | `services/duckdb_connection.py` | 6 |

---

## Layer 1 — Anthropic server-side prompt caching

### What it is

Anthropic caches the KV computation of a prompt prefix server-side for 5 minutes.
Any call that sends the same prefix within that window is charged at ~10% of the
normal input token price. Enabled by adding `"cache_control": {"type": "ephemeral"}`
to the system prompt block.

### Where to apply it

**Router node** — system prompt is 100% static (identical across all users and turns):

```python
# graph.py — router_node
response = await client.messages.create(
    model=ROUTER_MODEL,
    max_tokens=10,
    system=[
        {
            "type": "text",
            "text": _ROUTER_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ],
    messages=[{"role": "user", "content": user_content}],
)
```

**QueryAgent and VisualizationAgent** — system prompt is functionally static (schema
is DDL-stable). The prompt is regenerated on every call but the content is
identical across all users. `run_tool_loop` must accept a pre-formed system block
list instead of a plain string:

```python
# base.py — run_tool_loop signature change
async def run_tool_loop(
    client: AsyncAnthropic,
    session: ClientSession,
    model: str,
    system: list[dict] | str,   # was: system_prompt: str
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 2048,
) -> tuple[str, str | None]:
    ...
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,          # passed through unchanged
        tools=tools,
        messages=messages,
    )
```

```python
# query_agent.py — build cacheable system block once
_CACHED_SYSTEM: list[dict] | None = None

async def run(self, question, session, schema, context=None):
    global _CACHED_SYSTEM
    if _CACHED_SYSTEM is None:
        _CACHED_SYSTEM = [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT_TEMPLATE.format(schema=schema),
                "cache_control": {"type": "ephemeral"},
            }
        ]
    ...
    await run_tool_loop(..., system=_CACHED_SYSTEM, ...)
```

**ChartAgent** — system prompt is highly dynamic (`df_info`, `original_question`,
`source_sql` all vary per call). Do not apply prompt caching here; the cache
would never hit.

### Token cost impact

At 1,000 QueryAgent calls/day, assuming >60% cache hit rate after warm-up:

| Call | Tokens (no cache) | Tokens (with cache, hit) | Daily saving (600 hits) |
|------|-----------------|--------------------------|------------------------|
| Router | 400 | 400×10% = 40 | 360 tokens × 600 = 216,000 tokens |
| QueryAgent | 4,500 | 4,500×10% = 450 | 4,050 tokens × 600 = 2,430,000 tokens |
| VisualizationAgent | 4,700 | 4,700×10% = 470 | — (fewer calls, similar saving) |

At `claude-sonnet-4-6` input pricing (~$3/MTok):
- **QueryAgent alone: ~$7.29/day saved → ~$2,660/year at 100-user load**
- Router + all agents combined: **~$3,000–$4,000/year**

Latency benefit: 20–30% faster first-token time on cache hits (KV recompute
skipped server-side).

### Minimum token threshold

Anthropic prompt caching requires a minimum prefix length:
- `claude-sonnet-4-6`: ≥1,024 tokens to be eligible
- `claude-haiku-4-5`: ≥2,048 tokens

The router system prompt (~400 tokens) is **below the Sonnet threshold**. If the
router is moved to Haiku (ADR-009), the threshold is 2,048 — also not met.

**Practical implication:** Prompt caching applies to QueryAgent and
VisualizationAgent (schema pushes them to 3,500–5,700 tokens). The router is too
small to benefit — use the L3 LRU cache instead.

---

## Layer 2 — `AsyncAnthropic` client singleton

### Problem

`config.py:get_async_client()` creates a new `AsyncAnthropic()` instance on every
call. Each `AsyncAnthropic()` creates an `httpx.AsyncClient` with a new connection
pool. Under 100 concurrent users, hundreds of HTTP clients are created and
destroyed per minute — wasted memory and TCP connection overhead.

### Fix

```python
# config.py
_async_client: "AsyncAnthropic | None" = None

def get_async_client() -> "AsyncAnthropic":
    """Return the shared AsyncAnthropic client (created once per process)."""
    global _async_client
    if _async_client is None:
        from anthropic import AsyncAnthropic
        _async_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _async_client
```

Single `httpx.AsyncClient` with a persistent connection pool. Under 100 users,
connections are reused across requests, reducing TCP handshake overhead by
~50–150 ms per call on first connection.

---

## Layer 3 — Router intent LRU cache

### Problem

The router makes a Claude API call for every user message to classify intent.
For common BI phrases the classification is deterministic — "give me a chart",
"show as bar instead", "sales dashboard" always produce the same label. Paying
for a Claude call every time is wasteful.

### Decision

Cache router output in-process with an LRU cache keyed on
`(normalized_message, has_df)`:

```python
# graph.py
from functools import lru_cache

@lru_cache(maxsize=512)
def _cached_classify(message_key: str, has_df: bool) -> str:
    """Synchronous placeholder — actual async result stored in _router_cache."""
    return ""   # not used directly; see router_node below

_router_cache: dict[tuple[str, bool], str] = {}
_ROUTER_CACHE_MAX = 512

def _router_cache_key(message: str, has_df: bool) -> tuple[str, bool]:
    """Normalise message to improve hit rate (lowercase, strip whitespace)."""
    return (message.lower().strip(), has_df)

async def router_node(state: BIState) -> dict:
    last_message = state["messages"][-1].content
    has_df = bool(state.get("df_json"))
    cache_key = _router_cache_key(last_message, has_df)

    if cache_key in _router_cache:
        _logger.debug("router_node: cache hit intent=%r", _router_cache[cache_key])
        return {"intent": _router_cache[cache_key]}

    # Cache miss — call Claude
    ...  # existing router logic
    intent = ...

    # Evict oldest if at capacity, then store
    if len(_router_cache) >= _ROUTER_CACHE_MAX:
        _router_cache.pop(next(iter(_router_cache)))
    _router_cache[cache_key] = intent
    return {"intent": intent}
```

### Expected hit rate

| Message type | Cache hit rate | Notes |
|---|---|---|
| Exact repeat ("show me a chart") | ~30–50% | Common follow-up phrases |
| Unique analytical questions ("why did X happen in Y?") | ~5–10% | Low repetition |
| Short commands ("visualize this", "plot it", "dashboard") | ~60–80% | Highly reused |

Overall expected hit rate: ~25–40% across a real session mix.

At 25% hit rate on 1,000 router calls/day:
- 250 Claude calls eliminated
- Saving: 250 × 400 tokens = 100,000 tokens/day ≈ $0.30/day at Haiku pricing
- Latency: 0 ms for cache hits vs 200–400 ms for Haiku call

### Cache invalidation

The cache is in-process and ephemeral — it resets on every app restart. No
explicit invalidation needed: incorrect cached intent is a rare, recoverable
failure (user can rephrase). Do not persist across restarts.

---

## Layer 4 — SQL result TTL cache

### Problem

The BI database is read-only. Data is loaded by a batch job (`scripts/prepare_bi_data.py`)
that runs on a schedule (daily or weekly). Between runs, the same SQL query always
produces the same result. Re-executing queries costs DuckDB CPU time, disk I/O,
and result serialisation on every call.

### Gap: exact-match caching does not serve cross-user similar queries

The naive cache key `SHA256(sql + filter_context)` has two failure modes:

1. **Claude non-determinism:** The same user question asked twice may produce SQL
   with different aliases, whitespace, or column ordering — different hash, cache
   miss, identical DuckDB work repeated.

2. **Cross-user similar questions:** Two users asking "revenue by channel" and
   "sales breakdown by channel" trigger independent Claude calls and independent
   DuckDB executions even though the answer is the same. All tokens and query
   time are paid twice.

Both failure modes are addressed by combining SQL canonicalisation with a
semantic embedding cache.

### Decision: two-tier L4 cache

**Tier A — SQL canonicalisation (handles Claude non-determinism)**

Normalise SQL text before hashing so that formatting variation collapses to the
same key:

```python
# src/services/query_cache.py
import sqlglot, re, hashlib

def _canonicalise(sql: str) -> str:
    """Normalise SQL to a stable canonical form for cache key generation."""
    try:
        return sqlglot.transpile(sql.strip(), read="duckdb",
                                  write="duckdb", pretty=False)[0].upper()
    except Exception:
        return re.sub(r'\s+', ' ', sql.strip().upper())

def _sql_key(sql: str, filter_context: str) -> str:
    return hashlib.sha256(
        f"{_canonicalise(sql)}|{filter_context.strip()}".encode()
    ).hexdigest()
```

**Tier B — Semantic question cache with pgvector (handles cross-user similar questions)**

Store an embedding of the user's natural language question alongside the cached
result. On a new question, find the nearest neighbour above a cosine similarity
threshold rather than doing an exact lookup.

`pgvector` is a PostgreSQL extension — no new service needed if PostgreSQL is
already running for ADR-007:

```sql
-- One-time setup on the checkpoints PostgreSQL instance
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE semantic_query_cache (
    id           SERIAL PRIMARY KEY,
    question_vec vector(1536),
    filter_hash  TEXT NOT NULL,
    sql_text     TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON semantic_query_cache
    USING ivfflat (question_vec vector_cosine_ops) WITH (lists = 100);

-- SQL exact-match cache (Tier A)
CREATE TABLE query_cache (
    cache_key   TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
```

```python
# src/services/query_cache.py — full two-tier implementation
import hashlib, os
import asyncpg

_TTL = int(os.environ.get("QUERY_CACHE_TTL", 3600))
_SIM_THRESHOLD = float(os.environ.get("QUERY_CACHE_SIM_THRESHOLD", "0.97"))

# --- Tier A: exact SQL match ---

async def get_by_sql(pool: asyncpg.Pool, sql: str, filter_context: str) -> str | None:
    row = await pool.fetchrow(
        "SELECT result_json FROM query_cache WHERE cache_key=$1"
        " AND created_at > now() - make_interval(secs=>$2)",
        _sql_key(sql, filter_context), _TTL
    )
    return row["result_json"] if row else None

async def set_by_sql(pool: asyncpg.Pool, sql: str,
                     filter_context: str, result_json: str) -> None:
    await pool.execute(
        "INSERT INTO query_cache (cache_key, result_json) VALUES ($1,$2)"
        " ON CONFLICT (cache_key) DO UPDATE SET result_json=$2, created_at=now()",
        _sql_key(sql, filter_context), result_json
    )

# --- Tier B: semantic question similarity ---

async def get_semantic(pool: asyncpg.Pool, question: str,
                       filter_context: str, embed_fn) -> str | None:
    vec = await embed_fn(question)
    filter_hash = hashlib.sha256(filter_context.encode()).hexdigest()
    row = await pool.fetchrow("""
        SELECT result_json
        FROM semantic_query_cache
        WHERE filter_hash = $1
          AND created_at > now() - make_interval(secs=>$2)
          AND 1 - (question_vec <=> $3::vector) >= $4
        ORDER BY question_vec <=> $3::vector
        LIMIT 1
    """, filter_hash, _TTL, vec, _SIM_THRESHOLD)
    return row["result_json"] if row else None

async def set_semantic(pool: asyncpg.Pool, question: str, filter_context: str,
                       sql: str, result_json: str, embed_fn) -> None:
    vec = await embed_fn(question)
    filter_hash = hashlib.sha256(filter_context.encode()).hexdigest()
    await pool.execute(
        "INSERT INTO semantic_query_cache"
        " (question_vec, filter_hash, sql_text, result_json)"
        " VALUES ($1::vector, $2, $3, $4)",
        vec, filter_hash, sql, result_json
    )

async def invalidate_all(pool: asyncpg.Pool) -> None:
    """Call from prepare_bi_data.py after data reload."""
    await pool.execute("TRUNCATE query_cache, semantic_query_cache")
```

**Lookup order in `query_agent_node`:**

```python
# 1. Tier B — semantic question match (cross-user hit)
result = await get_semantic(pool, question, filter_context, embed_fn)
if result:
    return _state_updates_from_cache(result)

# 2. Tier A — exact SQL match (same-question repeat hit)
# (only reachable if semantic miss — Claude has already generated SQL)
sql = await _generate_sql(question, ...)
result = await get_by_sql(pool, sql, filter_context)
if result:
    return _state_updates_from_cache(result)

# 3. Full miss — execute against DuckDB
df = await _execute_sql(sql)
result_json = df.write_json()
await set_by_sql(pool, sql, filter_context, result_json)
await set_semantic(pool, question, filter_context, sql, result_json, embed_fn)
return _state_updates_from_df(df, sql)
```

### Similarity threshold tuning

| User A | User B | Similarity | Hit at 0.97? | Correct? |
|--------|--------|------------|-------------|----------|
| "Revenue by channel" | "Sales by channel" | ~0.98 | ✅ | ✅ Same data |
| "Revenue by channel" | "Revenue breakdown by channel" | ~0.99 | ✅ | ✅ Same data |
| "Top 10 products by sales" | "Best selling products" | ~0.94 | ❌ | ✅ Different intent |
| "Revenue by channel" | "Revenue by country" | ~0.88 | ❌ | ✅ Different query |
| "Revenue last quarter" | "Revenue this quarter" | ~0.96 | ❌ | ✅ Different time period |

0.97 is conservative — only near-identical phrasing hits. This is intentional:
a false cache hit (wrong data returned) is worse than a cache miss. Do not lower
below 0.95 without validating against real query pairs.

### Embedding model

| Model | Dimensions | Cost | Notes |
|-------|-----------|------|-------|
| `text-embedding-3-small` (OpenAI) | 1536 | $0.02/1M tokens | Good baseline |
| `voyage-3-lite` (Anthropic partner) | 512 | $0.02/1M tokens | Smaller vectors, lower pgvector storage |
| Local `all-MiniLM-L6-v2` (sentence-transformers) | 384 | Free | No API call; add ~150 MB model |

Recommended: `text-embedding-3-small` for Phase 5. Switch to local model if
API latency or cost becomes a concern at Phase 6 scale.

### When NOT to cache

- Queries containing `NOW()`, `CURRENT_DATE`, `CURRENT_TIMESTAMP` — results
  change intra-TTL
- Questions that include explicit time references the user typed ("today",
  "this week") — the embedding will match similar future questions incorrectly
- Invalidate both tables on data load: `await invalidate_all(pool)` in
  `prepare_bi_data.py`

### Expected impact (two-tier)

| Scenario | Single-tier (SQL exact) | Two-tier (SQL + semantic) |
|----------|------------------------|--------------------------|
| Same user repeats question | Hit (after canonicalise) | Hit |
| Same user rephrases question | Miss | Hit (Tier B) |
| Different user, same question | Miss (separate processes) | Hit (Tier B, shared PostgreSQL) |
| Different user, similar phrasing | Miss | Hit (Tier B if similarity ≥ 0.97) |

**Cross-user cache hit rate:** estimated 40–60% for a shared 100-user deployment
where users explore the same BI domain (orders, channels, products).

### Infrastructure additions

| Component | What | Already present? |
|-----------|------|-----------------|
| `pgvector` | PostgreSQL extension | `CREATE EXTENSION vector` — no new service |
| `sqlglot` | SQL normalisation | `poetry add sqlglot` |
| Embedding model API | `text-embedding-3-small` or local | New API call per cache miss |
| `asyncpg` | PostgreSQL async driver | Already required by ADR-007 |

---

## Layer 5 — Externalise `df_json` to file reference

### Problem

`df_json` stores a Polars DataFrame as inline JSON in `BIState`. A typical
DataFrame from a "top 10 products" query is 10–50 KB; a monthly trend with
all order detail can reach 500 KB–2 MB. This is written to the LangGraph
checkpoint (PostgreSQL) on every node return and read back on every graph
resume.

At 100 users, large `df_json` values cause:
- Slow checkpoint writes (PostgreSQL TOAST for large values)
- Slow checkpoint reads on interrupt/resume (all state fields read together)
- Inflated PostgreSQL storage

### Decision

Store `df_json` as a file path, not inline JSON. The graph node writes the
DataFrame to disk and stores only the path in state:

```python
# In query_agent_node
import uuid, pathlib

if result.data is not None:
    reports_dir = pathlib.Path("local_data/df_cache")
    reports_dir.mkdir(exist_ok=True)
    df_path = reports_dir / f"{uuid.uuid4()}.json"
    result.data.write_json(df_path)
    updates["df_json"] = str(df_path)   # path, not content
```

Nodes that read `df_json` (e.g., `chart_agent_node`) check whether the value
is a file path (starts with `local_data/`) and load it:

```python
df_json = state.get("df_json")
if df_json and df_json.startswith("local_data/"):
    df = pl.read_json(df_json)
else:
    df = pl.read_json(io.StringIO(df_json))  # legacy inline
```

Add a cleanup job to remove files older than 24 hours.

### Impact

| Before | After |
|--------|-------|
| 50–2,000 KB per checkpoint | ~50 bytes per checkpoint (file path string) |
| TOAST overhead on large DataFrames | No TOAST; checkpoint row is tiny |
| Full DataFrame read on every graph resume | File read only when `chart_agent_node` needs it |

---

## Layer 6 — Schema cache in direct DuckDB path (Phase 6)

### Problem

`MCPConnectionManager._schema_cache` caches the DuckDB schema across calls in
the current MCP path. When Phase 6 replaces MCP with a direct DuckDB connection
(`duckdb_connection.py`), this cache must be explicitly carried over — it is not
automatic.

### Decision

Module-level schema cache in the new `duckdb_connection.py`:

```python
# src/services/duckdb_connection.py
_schema: str | None = None

def get_schema() -> str:
    global _schema
    if _schema is None:
        conn = get_connection()
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        _schema = "\n\n".join(r[0] for r in rows if r[0])
    return _schema

def invalidate_schema_cache() -> None:
    """Call after a schema migration or data reload."""
    global _schema
    _schema = None
```

Call `invalidate_schema_cache()` at the end of `scripts/prepare_bi_data.py`
and whenever DDL changes are applied.

---

## Cost and Latency Summary

| Layer | Per-turn saving | Annual saving (100 users) | Effort |
|-------|----------------|--------------------------|--------|
| L1 Router prompt cache | — (below min threshold) | — | Low |
| L1 QueryAgent prompt cache | ~4,050 tokens/hit | **~$2,660** | Low |
| L1 VisualizationAgent prompt cache | ~4,230 tokens/hit | ~$400 | Low |
| L2 Client singleton | ~50–150 ms TCP overhead | — | Trivial |
| L3 Router LRU cache | 200–400 ms + 400 tokens | ~$110 | Low |
| L4 SQL result TTL cache | 50–500 ms DuckDB query | — | Medium |
| L5 `df_json` externalisation | Checkpoint size ↓ 95% | Storage + latency | Medium |
| L6 Schema cache (Phase 6) | Schema fetch per call | — | Low |

**Total estimated annual cost saving at 100 users: ~$3,100–$4,000**
(primarily L1 QueryAgent/VisualizationAgent prompt caching)

---

## Alternatives Considered

| Alternative | Why not chosen |
|-------------|---------------|
| Redis for all caching | Adds an ops dependency; in-process caches sufficient for single-process Phase 5; see Phase 6 amendment below for where Redis does apply |
| Semantic SQL caching (embed similar queries) | High complexity; SQL is already precise — semantic similarity adds false positives |
| Pre-compute common queries at data load | Requires knowing queries in advance; BI chat is open-ended |
| Reduce max_tokens to lower cost | Affects response quality; not a caching concern |
| Use a cheaper model for agents | Addressed in ADR-009 (model tier split); orthogonal to caching |

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| L1: Prompt cache TTL is 5 minutes — low-traffic periods (nights) get cache misses | Acceptable; overnight usage is low |
| L1: Cache miss on first call per 5-minute window — cold start cost | Same as current; no regression |
| L3: Router LRU cache is process-local — separate caches per pod in Phase 6 | Acceptable; haiku calls are cheap; per-pod cold starts add negligible cost |
| L4: SQL cache returns stale results if data is refreshed without calling `clear()` | Add `clear()` call to `prepare_bi_data.py`; document the contract |
| L4: SQL cache is process-local — multi-worker Phase 6 means N independent caches | Resolved in Phase 6 amendment: promote to PostgreSQL shared cache |
| L5: File-reference `df_json` requires disk space management | 24-hour cleanup job; typical usage is ~50 MB/day |
| L5: Per-pod ephemeral filesystem means files are invisible across pods in Phase 6 | Resolved in Phase 6 amendment: shared volume or object storage |
| L6: `invalidate_schema_cache()` must be called manually after DDL changes | Document in `prepare_bi_data.py`; add assertion in `get_schema()` if schema is unexpectedly empty |

---

## Amendment — Phase 6: shared caching for multi-worker deployment

### Context

ADR-008 deploys LangGraph Server with multiple OS worker processes
(`LANGCHAIN_WORKERS=4`). Each process has independent memory. In-process caches
(L3 router LRU, L4 SQL TTL) are invisible across workers:

```
Worker 1: User A → "revenue by channel" → SQL executed → cached in Worker 1
Worker 2: User B → "revenue by channel" → cache miss → SQL re-executed
Worker 3: User C → "revenue by channel" → cache miss → SQL re-executed
Worker 4: User D → "revenue by channel" → cache miss → SQL re-executed
```

The effective hit rate for L4 drops from ~30–50% (single process) to ~30–50% / N
workers for cross-user queries. For 4 workers, that is ~8–12% — eliminating most
of the benefit.

Similarly, L5's `df_json` files written by Worker 1 to its local disk are
invisible to Worker 2 when resuming an interrupted graph execution.

### Decision

Promote L4 to **PostgreSQL** (already running for ADR-007). Do not add Redis as
a new infrastructure dependency unless L4 PostgreSQL read latency becomes
measurable under load.

Keep L3 (router LRU) and L6 (schema) as in-process — acceptable per-worker
cold starts given low cost.

Fix L5 with a **shared volume** (Docker bind mount, NFS) or **object storage**
(S3/GCS/Azure Blob) for `df_json` files.

### L4 — Promote SQL cache to PostgreSQL

```sql
-- Run once at Phase 6 setup
CREATE TABLE query_cache (
    cache_key    TEXT PRIMARY KEY,   -- SHA256(sql || filter_context)
    result_json  TEXT NOT NULL,      -- Polars write_json() output
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX query_cache_created ON query_cache (created_at);
```

```python
# src/services/query_cache.py — Phase 6 version
import hashlib, os
import asyncpg

_TTL_SECONDS = int(os.environ.get("QUERY_CACHE_TTL", 3600))

def _key(sql: str, filter_context: str) -> str:
    return hashlib.sha256(f"{sql.strip()}|{filter_context.strip()}".encode()).hexdigest()

async def get_cached(pool: asyncpg.Pool, sql: str, filter_context: str) -> str | None:
    row = await pool.fetchrow(
        "SELECT result_json FROM query_cache WHERE cache_key=$1"
        " AND created_at > now() - make_interval(secs => $2)",
        _key(sql, filter_context), _TTL_SECONDS
    )
    return row["result_json"] if row else None

async def set_cached(pool: asyncpg.Pool, sql: str, filter_context: str, result_json: str) -> None:
    await pool.execute(
        "INSERT INTO query_cache (cache_key, result_json) VALUES ($1, $2)"
        " ON CONFLICT (cache_key) DO UPDATE SET result_json=$2, created_at=now()",
        _key(sql, filter_context), result_json
    )

async def invalidate_all(pool: asyncpg.Pool) -> None:
    """Call from prepare_bi_data.py after data reload."""
    await pool.execute("TRUNCATE query_cache")
```

**Read latency:** PostgreSQL cache hit ~2–5 ms vs DuckDB query ~50–500 ms → 10–100× win.
**Write latency:** Async write, non-blocking — does not add to response time.
**Shared across all workers** via the same PostgreSQL instance used by ADR-007.

### L5 — Shared storage for `df_json` files

In Phase 6 Docker deployment, mount a shared volume for `local_data/df_cache/`:

```yaml
# docker-compose.yml
services:
  langgraph-server:
    volumes:
      - df_cache:/app/local_data/df_cache  # shared across all replicas

volumes:
  df_cache:
```

For cloud deployments (Kubernetes), replace the Docker volume with an S3/GCS
bucket and update the write/read paths to use `boto3` or `google-cloud-storage`.

### When to add Redis

Redis becomes the right choice if any of the following are true:

| Trigger | Why Redis then |
|---------|---------------|
| SQL cache hit rate > 60% and PostgreSQL read latency is measurable under peak load | Redis sub-millisecond reads vs PostgreSQL 2–5 ms; at 60+ hits/second the difference accumulates |
| Need pub/sub invalidation across pods | `prepare_bi_data.py` publishes `INVALIDATE` event; all workers subscribe and clear immediately rather than waiting for TTL |
| Horizontal scaling beyond 10 pods | PostgreSQL connection pool saturation; Redis handles far more concurrent connections |
| Cache entries > 10 MB (large DataFrames) | PostgreSQL TEXT column becomes slow; Redis handles large blobs natively with streaming |

The minimum Redis setup for this app:

```python
# src/services/query_cache.py — Redis variant
import redis.asyncio as redis
import hashlib, os, json

_client: redis.Redis | None = None
_TTL = int(os.environ.get("QUERY_CACHE_TTL", 3600))

def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(os.environ["REDIS_URL"])
    return _client

async def get_cached(sql: str, filter_context: str) -> str | None:
    key = f"sql:{hashlib.sha256(f'{sql}|{filter_context}'.encode()).hexdigest()}"
    return await get_redis().get(key)

async def set_cached(sql: str, filter_context: str, result: str) -> None:
    key = f"sql:{hashlib.sha256(f'{sql}|{filter_context}'.encode()).hexdigest()}"
    await get_redis().setex(key, _TTL, result)

async def invalidate_all() -> None:
    await get_redis().publish("cache_invalidate", "all")
```

### Summary: per-layer Phase 6 decision

| Layer | Phase 5 (single process) | Phase 6 (multi-worker) | New dependency? |
|-------|--------------------------|------------------------|-----------------|
| L1 Prompt cache | Anthropic server-side | Unchanged | None |
| L2 Client singleton | Per-process | Per-process | None |
| L3 Router LRU | In-process dict | In-process dict (per worker — acceptable) | None |
| L4 SQL TTL cache | In-process dict | **PostgreSQL shared table** | None (PostgreSQL already running) |
| L5 `df_json` | Local file | **Shared Docker volume or object storage** | Volume mount or S3 |
| L6 Schema cache | In-process | In-process (schema fetched once at startup per worker) | None |
| **Redis** | Not needed | Optional — add if PostgreSQL L4 latency measurable under load | Redis (conditional) |
