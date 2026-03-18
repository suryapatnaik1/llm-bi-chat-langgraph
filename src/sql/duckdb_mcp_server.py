"""DuckDB MCP Server — exposes bi.db as MCP tools over stdio transport.

Run standalone:
    python -m private_gpt.components.sql.duckdb_mcp_server

Or spawn as a subprocess from MCPBiService (stdio transport).

Tools exposed:
    get_schema    — returns CREATE TABLE DDL for all tables in bi.db
    execute_sql   — executes a SELECT query and returns a markdown table
"""
import asyncio
import logging
from pathlib import Path

import duckdb
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

# Resolve DB path relative to this file: src/sql/duckdb_mcp_server.py → 2 parents up = project root
_DB_PATH = Path(__file__).parents[2] / "local_data" / "bi.db"

_logger = logging.getLogger(__name__)

app = Server("duckdb-bi")

# Module-level singleton connection (lazy-loaded on first tool call)
_conn: duckdb.DuckDBPyConnection | None = None


def _conn_singleton() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        if _DB_PATH.exists():
            _conn = duckdb.connect(str(_DB_PATH), read_only=True)
            _logger.info("duckdb_mcp_server: connected to %s", _DB_PATH)
        else:
            _logger.warning(
                "duckdb_mcp_server: %s not found, using :memory:", _DB_PATH
            )
            _conn = duckdb.connect(":memory:")
    return _conn


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_schema",
            description=(
                "Returns the CREATE TABLE DDL for every table in the DuckDB database. "
                "Call this first to understand what columns and types are available."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="execute_sql",
            description=(
                "Executes a read-only DuckDB SQL query and returns results as a "
                "markdown table. Only SELECT statements are permitted."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A valid DuckDB SELECT statement.",
                    }
                },
                "required": ["sql"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    conn = _conn_singleton()

    if name == "get_schema":
        try:
            tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
            if not tables:
                return [types.TextContent(type="text", text="-- no tables available")]
            parts: list[str] = []
            for table_name in tables:
                cols = conn.execute(f"DESCRIBE {table_name}").fetchall()
                col_defs = ", ".join(f'"{c[0]}" {c[1]}' for c in cols)
                parts.append(f"CREATE TABLE {table_name} ({col_defs});")
            return [types.TextContent(type="text", text="\n".join(parts))]
        except Exception as exc:
            _logger.exception("get_schema failed")
            return [types.TextContent(type="text", text=f"Error reading schema: {exc}")]

    if name == "execute_sql":
        sql: str = arguments.get("sql", "").strip()
        if not sql:
            return [types.TextContent(type="text", text="Error: no SQL provided")]
        # Only allow SELECT statements
        if not sql.upper().lstrip().startswith("SELECT"):
            return [
                types.TextContent(
                    type="text",
                    text="Error: only SELECT statements are permitted",
                )
            ]
        try:
            rel = conn.execute(sql)
            cols = [desc[0] for desc in rel.description]
            rows = rel.fetchall()
        except Exception as exc:
            _logger.warning("execute_sql error: %s", exc)
            return [types.TextContent(type="text", text=f"SQL error: {exc}")]

        if not rows:
            return [types.TextContent(type="text", text="*(no rows returned)*")]

        col_widths = [
            max(len(str(c)), max((len(str(r[i])) for r in rows), default=0))
            for i, c in enumerate(cols)
        ]
        header = " | ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cols))
        sep = " | ".join("-" * w for w in col_widths)
        body = "\n".join(
            " | ".join(str(v).ljust(col_widths[i]) for i, v in enumerate(row))
            for row in rows
        )
        return [types.TextContent(type="text", text=f"{header}\n{sep}\n{body}")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def _run_server() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="duckdb-bi",
                server_version="1.0.0",
                capabilities=app.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_server())
