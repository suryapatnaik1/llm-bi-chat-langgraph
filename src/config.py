"""Centralized configuration for the agentic BI chat app."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic").lower()

DB_PATH: Path = Path(__file__).parent.parent / "local_data" / "bi.db"
REPORTS_DIR: Path = Path(__file__).parent / "static" / "reports"

MCP_SERVER_SCRIPT: str = str(Path(__file__).parent / "sql" / "duckdb_mcp_server.py")


def get_async_client():
    """Return a cached AsyncAnthropic client."""
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
