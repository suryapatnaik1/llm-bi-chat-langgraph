"""MCP connection manager: subprocess lifecycle + schema caching."""
import asyncio
import logging
import sys
import threading
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import MCP_SERVER_SCRIPT

_logger = logging.getLogger(__name__)


class MCPConnectionManager:
    """Manages the asyncio event loop, MCP subprocess, and schema cache.

    Usage (sync, from Streamlit):
        mgr = MCPConnectionManager()
        result = mgr.run(some_async_fn)
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-loop"
        )
        self._loop_thread.start()
        self._schema_cache: str | None = None
        _logger.info("MCPConnectionManager: initialized")

    def run(self, coro) -> Any:
        """Run an async coroutine on the managed event loop (blocking)."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)

    async def execute_with_session(self, fn):
        """Spawn an MCP subprocess, fetch schema, and call fn(session, schema).

        Args:
            fn: async callable(session: ClientSession, schema: str) -> T
        Returns:
            The result of fn.
        """
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[MCP_SERVER_SCRIPT],
            env=None,
        )
        t0 = time.monotonic()
        async with stdio_client(server_params) as (read, write):
            _logger.info("MCP subprocess started in %.1fs", time.monotonic() - t0)
            async with ClientSession(read, write) as session:
                await session.initialize()
                _logger.info("MCP session initialized in %.1fs", time.monotonic() - t0)

                if self._schema_cache is None:
                    schema_result = await session.call_tool("get_schema", {})
                    self._schema_cache = "\n".join(
                        c.text for c in schema_result.content if hasattr(c, "text")
                    )
                    _logger.info(
                        "Schema fetched in %.1fs (%d chars)",
                        time.monotonic() - t0,
                        len(self._schema_cache),
                    )

                result = await fn(session, self._schema_cache)
                _logger.info("Total MCP session time %.1fs", time.monotonic() - t0)
                return result

    async def get_mcp_tools(self, session: ClientSession) -> list[dict[str, Any]]:
        """List available MCP tools (excluding get_schema)."""
        result = await session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in result.tools
            if tool.name != "get_schema"
        ]
