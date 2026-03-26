# ADR-003 — `chart_history` field: plain list over `operator.add` reducer

**Status:** Accepted (supersedes initial Phase 1 design)

## Context

`BIState.chart_history` stores the turn-by-turn conversation between the user
and `ChartAgent` (clarifying questions and answers) so that multi-turn chart
building can resume correctly after a LangGraph `interrupt()`.

During Phase 1, the field was defined with an `operator.add` append reducer:

```python
chart_history: Annotated[list, operator.add] = []
```

The rationale at the time was that "turns should never be overwritten by a
partial state update" — if two nodes update state simultaneously, the reducer
merges lists rather than one overwriting the other.

This design was revisited during gap analysis after mapping out the full user
interaction patterns (see `bi-chat-interactions.md`).

## Problem with `operator.add`

`operator.add` is **append-only with no reset path**. Any node that returns
`{"chart_history": []}` appends an empty list (no-op); there is no way for a
node to set `chart_history` back to `[]`.

This becomes a correctness bug in the following real session:

```
Turn 1:  "Show me sales by month"        → QueryAgent → df produced
Turn 2:  "Plot this"                     → ChartAgent asks: "Bar or line?"
Turn 3:  "Line chart please"             → ChartAgent returns chart-spec
         chart_history = [{user: "Plot this"}, {assistant: "Bar or line?"}, {user: "Line chart please"}, {assistant: <spec>}]

Turn 4:  "Show me top 10 products"       → QueryAgent → NEW df produced
Turn 5:  "Visualise it"                  → ChartAgent now receives full history from turns 2–3
                                           as context for a completely unrelated chart
```

ChartAgent's system prompt uses `history` as the active conversation — stale
history from a prior chart conversation produces nonsensical clarifying questions
or a chart-spec for the wrong data.

## Decision

Change `chart_history` to a **plain field** (no reducer annotation). Make
`query_agent_node` explicitly reset it to `[]` when it writes a new `df_json`.
Make `chart_agent_node` return the complete updated list on every execution.

```python
# state.py
chart_history: list = []   # plain field — managed explicitly by nodes

# query_agent_node — reset when new data arrives
updates["df_json"] = result.data.write_json()
updates["chart_history"] = []   # new DataFrame, new chart conversation

# chart_agent_node — return full list, not just the new turns
updates["chart_history"] = history_with_current + [{"role": "assistant", "content": response}]
```

## Rationale

**Why `operator.add` was the wrong reducer here:**
The append reducer is designed for cases where multiple nodes write to the same
list key concurrently (e.g., two parallel nodes each appending their results).
`chart_history` is written by exactly one node (`chart_agent_node`) in sequence,
so the merge semantics of `operator.add` provide no benefit and prevent reset.

**Why explicit management is correct:**
A chart conversation is scoped to a single DataFrame. When the DataFrame
changes (new `query_agent` run), the chart conversation must reset. The node
that creates a new DataFrame (`query_agent_node`) is the natural place to
enforce this invariant.

**Why this is safe with LangGraph's partial-update model:**
LangGraph merges node return dicts into state using the field's reducer. With no
reducer (plain field), the returned value replaces the current value entirely —
which is exactly the desired behaviour for an explicit full-list return.

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| If two nodes could ever write `chart_history` concurrently, last-writer-wins would lose turns | Not possible in the current graph topology — `chart_agent_node` is the only writer, and it runs after all other nodes |
| An interrupted `chart_agent` session is lost if the user starts a new query before resuming | Intentional — a new query implies abandonment of the previous chart conversation; the new `df_json` signals a clean slate |
