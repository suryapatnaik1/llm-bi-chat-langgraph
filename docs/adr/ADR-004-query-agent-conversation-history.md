# ADR-004 — Pass full conversation history to QueryAgent

**Status:** Accepted (pending implementation — Phase 2 amendment)

## Context

`query_agent_node` currently passes only the last user message to `QueryAgent`:

```python
question = state["messages"][-1].content  # only the last message
result = await _query_agent.run(question=question, ...)
```

`QueryAgent` builds its prompt from this single string. It has no visibility
into what was asked or answered before this turn.

This was the initial Phase 2 implementation, carried over from the existing
`OrchestratorAgent` design where each `orchestrator.query(question, context)`
call was stateless by design — the Streamlit `app.py` treated each submission
as an independent request.

The gap was identified during interaction taxonomy analysis
(`bi-chat-interactions.md`). Two of the six documented multi-turn patterns
depend on the agent understanding prior context:

**Pattern A — Drill-Down Chain:**
```
"Revenue by channel"
  → "Show me just wholesale"       ← "wholesale" has no antecedent without history
    → "Break that down by country" ← "that" undefined without prior answer
```

**Pattern B — Refinement Loop:**
```
"Top products by sales last quarter"
  → "Same but exclude clearance"   ← "same" is undefined without history
    → "And just for online channel" ← requires both prior refinements as context
```

In both patterns, the current implementation produces either an error or an
answer to a completely different question — there is no warning to the user.

## Decision

Pass `state["messages"]` (the full `BIState` message list, not just the last
item) into `query_agent_node` and format it as a multi-turn conversation for
`run_tool_loop`.

```python
async def query_agent_node(state: BIState) -> dict:
    messages = [
        {"role": "user" if m.type == "human" else "assistant", "content": m.content}
        for m in state["messages"]
    ]
    # messages[-1] is the current question; earlier entries provide context

    async def _run(session, schema):
        tools = await _mcp.get_mcp_tools(session)
        return await _query_agent.run(
            question=messages[-1]["content"],  # current question
            session=session,
            schema=schema,
            context={
                "filter_context": state.get("filter_context", ""),
                "mcp_tools": tools,
                "conversation_history": messages[:-1],  # prior turns
            },
        )
```

`QueryAgent.run()` will be updated to prepend `conversation_history` entries
before the current user message when building the `messages` list for
`run_tool_loop`.

## Rationale

**LangGraph makes this straightforward:** `BIState` extends `MessagesState`,
which accumulates every turn in `state["messages"]`. The full history is
already available inside every node — it just wasn't being used.

**The prior stateless design was an artefact of Streamlit, not a deliberate
choice:** `app.py` passed each question to `orchestrator.query()` independently
because `st.session_state` held the conversation history separately, not passed
through the agent. In LangGraph the state object is the right vehicle.

**SQL agents benefit from prior context even for simple follow-ups:**
Phrases like "same period", "that channel", "those products" are unambiguous to
a human reading the chat but produce incorrect SQL without the prior messages.
Passing history also lets the agent avoid re-querying data it already retrieved.

**Token cost is bounded:** `BIState.messages` accumulates all turns, but a
typical BI session is 5–10 exchanges. At ~500 tokens per exchange the full
history adds ~5 000 tokens to the context — well within model limits and a
small fraction of the cost of re-querying.

## Alternatives Considered

| Alternative | Why rejected |
|-------------|-------------|
| Store a separate `query_history` field in `BIState` | Redundant with `messages` — duplication creates sync problems |
| Summarise prior turns before passing to the agent | Added complexity; summaries lose the specific column names and values the agent needs to resolve references |
| Require users to re-state context on every turn | Already the current (broken) behaviour; explicitly rejected as it breaks the documented multi-turn patterns |

## Known Limitations

| Limitation | Mitigation |
|------------|------------|
| Very long sessions (50+ turns) add significant tokens to every query | Add a `max_history_turns` cap (e.g., last 10 exchanges) if latency or cost becomes a concern |
| Assistant messages contain formatted text, not raw SQL — the agent must infer prior data from text, not structured results | Acceptable for context resolution; prior SQL is available separately via `state["last_sql"]` |
| Chart agent messages in history are irrelevant to SQL generation and add noise | Filter `chart_history` turns out of the messages passed to `QueryAgent`; only pass `HumanMessage` + `AIMessage` pairs from query turns |
