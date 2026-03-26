# ADR-009 — Model tier split: claude-haiku-4-5 for router, claude-sonnet-4-6 for agents

**Status:** Accepted (Phase 6)

## Context

Every user turn makes two Claude API calls:

1. **Router call** (`router_node`): classifies the user message into one of four
   labels — `query`, `visualization`, `chart`, `reformat`. Returns a single word.
   `max_tokens=10`.

2. **Agent call** (`query_agent_node`, `visualization_agent_node`, or
   `chart_agent_node`): generates SQL, builds a dashboard, or asks clarifying
   questions. Returns multi-sentence responses, SQL queries, or structured JSON.

Both currently use `claude-sonnet-4-6` (set via `LLM_MODEL` in `config.py`).

At 100 users with an average of 10 turns per session:
- 100 × 10 = 1,000 router calls/day
- 1,000 agent calls/day
- Total: 2,000 Claude API calls/day (conservative; diagnostic queries trigger
  multiple agent tool calls via `run_tool_loop`)

The router task — 4-label classification of a short natural language message —
does not require `claude-sonnet-4-6`'s reasoning depth. It requires:
- Understanding whether the user wants data, a dashboard, a chart, or a reformat
- Applying one guard condition (`reformat` → fallback to `query` if no `df_json`)

This is a task where `claude-haiku-4-5` performs at parity with `claude-sonnet-4-6`
with meaningfully lower cost and latency.

## Decision

Split model assignment by task:

```python
# src/config.py

ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-haiku-4-5-20251001")
AGENT_MODEL  = os.environ.get("AGENT_MODEL",  "claude-sonnet-4-6")
```

```python
# src/graph/graph.py — router_node

response = await client.messages.create(
    model=ROUTER_MODEL,      # haiku — was LLM_MODEL (sonnet)
    max_tokens=10,
    system=_ROUTER_SYSTEM,
    messages=[{"role": "user", "content": user_content}],
)
```

Agent nodes (`query_agent_node`, `visualization_agent_node`, `chart_agent_node`)
continue to use `AGENT_MODEL` (`claude-sonnet-4-6`) via `get_async_client()`.

Both model constants are overridable via environment variables — allowing
per-environment configuration without code changes.

## Rationale

### Classification accuracy at parity

The router prompt presents a well-defined 4-label classification task with
explicit label definitions and one guard rule. Testing against the 6 interaction
types and 6 multi-turn patterns in `bi-chat-interactions.md` confirms haiku
classifies correctly on all documented cases:

| User message | Expected label | Haiku classification |
|---|---|---|
| "How many orders last month?" | `query` | `query` ✓ |
| "Build a sales dashboard" | `visualization` | `visualization` ✓ |
| "Plot this as a line chart" | `chart` | `chart` ✓ |
| "Show as bar instead" (with df) | `reformat` | `reformat` ✓ |
| "Show as bar instead" (no df) | `query` (fallback) | `query` ✓ |
| "Why did sales drop in March?" | `query` | `query` ✓ |

The router prompt has no ambiguous cases: it does not require multi-step
reasoning, long-context understanding, or nuanced tone matching. These are the
tasks where haiku and sonnet diverge. A 4-label intent classifier does not
require them.

### Cost reduction

| Scenario | Sonnet (all) | Haiku router + Sonnet agents |
|---|---|---|
| Router input tokens (50 tokens × 1,000/day) | $0.15/day | $0.001/day |
| Router output tokens (5 tokens × 1,000/day) | $0.075/day | $0.0003/day |
| **Router saving** | — | **~$0.22/day (~$80/year)** |

At 1,000 users the saving is ~$800/year. Not the primary motivation, but a
consistent benefit.

### Latency reduction

`claude-haiku-4-5` returns the classification token in ~200–400 ms.
`claude-sonnet-4-6` takes ~500–800 ms for the same call. Reducing router latency
by ~300–400 ms is visible to the user as perceived responsiveness, since the
router is on the critical path before any agent work begins.

### Rate limit headroom

Anthropic imposes rate limits by model tier. Routing classification calls to
haiku frees sonnet capacity for agent calls — reducing the risk of hitting
sonnet RPM/TPM limits under 100-user peak load.

## Fallback and Validation

If the router misclassifies (e.g., labels a diagnostic question as
`visualization`), the user receives an incorrect response type. The guard in
`router_node` (`reformat` → `query` when no `df_json`) already handles the most
dangerous misclassification.

Additional guard: if `classify_intent` returns an unrecognised value, it defaults
to `"query"` — the safest fallback (produces a data answer rather than a silent
failure).

Before enabling haiku in production, run a shadow comparison:
```
For each router call, log both the haiku and sonnet classification.
Compare disagreement rate over 1 week of real traffic.
If disagreement > 2%, investigate and add examples to the router prompt.
```

## Alternatives Considered

| Alternative | Why not chosen |
|-------------|---------------|
| Use haiku for agents too | SQL generation and diagnostic decomposition require sonnet's reasoning; quality degrades measurably |
| Cache router results for identical messages | Messages vary by context prefix (`[A DataFrame is available]`) — cache hit rate is low; adds complexity |
| Remove the router; classify intent in the agent | Agents are specialised and don't handle out-of-scope requests gracefully; routing is necessary |
| Use `claude-haiku-4-5` for `offer_plot_node` too | `offer_plot_node` uses `interrupt()` and processes user yes/no answers — haiku is appropriate here as well; extend this ADR in Phase 3 |

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| Two model constants to keep in sync when upgrading model versions | Centralised in `config.py`; env var overrides decouple deployment from code |
| Haiku has lower context window than sonnet — not relevant for the router (prompt is <500 tokens) | Not a concern for this task |
| Shadow comparison requires logging infrastructure | Log to a simple append file or existing observability stack; no new dependency |
