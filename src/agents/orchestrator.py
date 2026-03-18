"""OrchestratorAgent: LLM-based router that dispatches to specialist agents."""
import logging
import time
from typing import Any

from mcp import ClientSession

from agents.base import AgentResult
from agents.registry import AgentRegistry
from config import LLM_MODEL, get_async_client
from services.mcp_connection import MCPConnectionManager

_logger = logging.getLogger(__name__)

_ROUTER_SYSTEM_PROMPT = """\
You are a routing agent for a business intelligence system. Your job is to classify \
the user's question and delegate it to the most appropriate specialist agent.

You have access to specialist agents as tools. Analyze the user's question and call \
exactly ONE tool — the specialist best suited to handle it.

Do NOT answer the question yourself. Always delegate to a specialist.
"""


class OrchestratorAgent:
    """Routes user questions to specialist agents via Claude tool_use.

    Args:
        registry: AgentRegistry with registered specialist agents.
        connection_manager: MCPConnectionManager for MCP subprocess lifecycle.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        connection_manager: MCPConnectionManager,
    ) -> None:
        self._registry = registry
        self._conn_mgr = connection_manager

    def query(self, question: str, context: dict[str, Any] | None = None) -> AgentResult:
        """Synchronous entry point: classify and dispatch question.

        Returns the AgentResult from the selected agent.
        """
        if not question or not question.strip():
            return AgentResult(text="Please ask a question about the business data.")
        try:
            return self._conn_mgr.run(self._async_query(question.strip(), context))
        except TimeoutError:
            _logger.error("Query timed out after 120s")
            return AgentResult(text="Query timed out. The database or model took too long to respond.")
        except Exception as exc:
            _logger.exception("Unexpected error during query")
            return AgentResult(text=f"An error occurred: {exc}")

    async def _async_query(
        self, question: str, context: dict[str, Any] | None = None
    ) -> AgentResult:
        """Route the question to a specialist agent via the MCP session."""

        async def _execute(session: ClientSession, schema: str) -> AgentResult:
            t0 = time.monotonic()

            # Get MCP tools for the specialist agents
            mcp_tools = await self._conn_mgr.get_mcp_tools(session)

            # Build context for specialist agents
            agent_context = dict(context or {})
            agent_context["mcp_tools"] = mcp_tools

            # Get orchestrator tool definitions from the registry
            router_tools = self._registry.orchestrator_tools()

            # Single Claude call to classify intent
            client = get_async_client()
            messages = [{"role": "user", "content": question}]

            response = await client.messages.create(
                model=LLM_MODEL,
                max_tokens=256,
                system=_ROUTER_SYSTEM_PROMPT,
                tools=router_tools,
                messages=messages,
            )
            _logger.info(
                "Orchestrator: stop_reason=%s blocks=%d (%.1fs)",
                response.stop_reason,
                len(response.content),
                time.monotonic() - t0,
            )

            # Find the tool_use block to determine which agent to dispatch to
            for block in response.content:
                if block.type == "tool_use":
                    agent_name = block.name
                    agent_question = block.input.get("question", question)
                    _logger.info(
                        "Orchestrator: routing to %s with question=%r",
                        agent_name,
                        agent_question[:80],
                    )

                    agent = self._registry.get(agent_name)
                    result = await agent.run(
                        question=agent_question,
                        session=session,
                        schema=schema,
                        context=agent_context,
                    )

                    _logger.info(
                        "Orchestrator: %s completed in %.1fs",
                        agent_name,
                        time.monotonic() - t0,
                    )
                    return result

            # Fallback: if no tool_use, return any text response
            for block in response.content:
                if hasattr(block, "text"):
                    return AgentResult(text=block.text)

            return AgentResult(text="Could not determine how to handle your question. Please try rephrasing.")

        return await self._conn_mgr.execute_with_session(_execute)
