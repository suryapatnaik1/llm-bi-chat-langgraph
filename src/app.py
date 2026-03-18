"""Thin Streamlit UI for the agentic BI chat app."""
import base64
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import plotly.express as px
import polars as pl
import streamlit as st
import streamlit.components.v1 as _components

# Add src/ to path so bare imports work (agents, services, config)
sys.path.insert(0, str(Path(__file__).parent))

from config import DB_PATH, REPORTS_DIR
from agents.registry import AgentRegistry
from agents.query_agent import QueryAgent
from agents.visualization_agent import VisualizationAgent
from agents.orchestrator import OrchestratorAgent
from agents.chart_agent import ChartAgent, parse_chart_spec, clean_response
from services.mcp_connection import MCPConnectionManager

# ── page config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="BI Chat", page_icon="✨", layout="wide")


# ── LLM helpers ──────────────────────────────────────────────────────────────

def _make_llm_complete():
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

    if provider == "anthropic":
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")

        def llm_complete(prompt: str) -> str:
            with client.messages.stream(
                model=model,
                max_tokens=512,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                return stream.get_final_message().content[0].text

    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        model = os.getenv("LLM_MODEL", "gpt-4o")

        def llm_complete(prompt: str) -> str:
            response = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content

    else:
        st.error(f"Unknown LLM_PROVIDER '{provider}'.")
        st.stop()

    return llm_complete


# ── intent classification ────────────────────────────────────────────────────

_INTENT_PROMPT = """\
Classify the user message as one of two intents:

1. "reformat" — the user wants to change how existing data is displayed \
(different chart type, different visualisation). Examples: "show that as a bar chart", \
"change to line", "make it a pie chart", "can you use a scatter plot instead".

2. "query" — the user is asking a new data question or wants different/additional data. \
Examples: "show me sales by month", "what are the top 5 products", "filter by channel".

User message: {message}

Reply with ONLY one word — either "reformat" or "query".
"""

_YES_NO_PROMPT = """\
The user was asked whether they want to visualise some data.
User reply: {message}
Reply with ONLY "yes" or "no".
"""


def classify_intent(message: str, llm_complete, has_previous_df: bool) -> str:
    if not has_previous_df:
        return "query"
    try:
        raw = llm_complete(_INTENT_PROMPT.format(message=message))
        return "reformat" if "reformat" in raw.strip().lower() else "query"
    except Exception:
        return "query"


def _classify_yes_no(message: str, llm_complete) -> str:
    try:
        raw = llm_complete(_YES_NO_PROMPT.format(message=message)).strip().lower()
        return "yes" if "yes" in raw else "no"
    except Exception:
        lower = message.lower()
        return "yes" if any(w in lower for w in ("yes", "sure", "please", "yeah", "yep", "ok", "okay", "plot", "go ahead")) else "no"


# ── DuckDB helpers ───────────────────────────────────────────────────────────


def _execute_chart_sql(sql: str) -> pl.DataFrame | None:
    """Execute a SELECT query and return a Polars DataFrame, or None on error."""
    if not sql or not DB_PATH.exists():
        return None
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        rel = conn.execute(sql)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchall()
        conn.close()
        if not rows:
            return None
        return pl.DataFrame(rows, schema=cols, orient="row")
    except Exception as exc:
        st.warning(f"Chart SQL error: {exc}")
        return None


def _get_chart_df(spec: dict, fallback_df: pl.DataFrame) -> pl.DataFrame:
    """Execute chart-spec SQL if present, otherwise return the fallback DataFrame."""
    sql = spec.get("sql")
    if sql:
        result = _execute_chart_sql(sql)
        if result is not None:
            return result
    return fallback_df


def _get_db_schema() -> str:
    """Get a compact schema description of all tables for the ChartAgent."""
    if not DB_PATH.exists():
        return "Database not available"
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        schema_parts = []
        for table in tables:
            cols = conn.execute(f"DESCRIBE {table}").fetchall()
            col_strs = ", ".join(f"{c[0]} {c[1]}" for c in cols)
            schema_parts.append(f"{table}({col_strs})")
        conn.close()
        return "\n".join(schema_parts)
    except Exception:
        return "Schema not available"


@st.cache_data(ttl=300)
def get_db_schema_cached() -> str:
    """Cached version of _get_db_schema."""
    return _get_db_schema()


# ── Plotly chart rendering ───────────────────────────────────────────────────


def render_chart(df: pl.DataFrame, config: dict) -> None:
    """Render a Plotly chart from a DataFrame and chart-spec config."""
    chart_type = config.get("chart_type", "none")
    if chart_type == "none":
        return

    pdf = df.to_pandas()

    def _col(c):
        return c if c and c in pdf.columns else None

    x = _col(config.get("x"))
    y = _col(config.get("y"))
    color = _col(config.get("color"))
    title = config.get("title", "")

    # Auto-fill missing axis columns
    if x is None or (y is None and chart_type not in ("histogram",)):
        numeric_cols = [c for c in pdf.columns if pdf[c].dtype.kind in ("i", "f", "u")]
        non_numeric_cols = [c for c in pdf.columns if c not in numeric_cols]
        if x is None and non_numeric_cols:
            x = non_numeric_cols[0]
        elif x is None and pdf.columns.tolist():
            x = pdf.columns[0]
        if y is None and chart_type != "histogram" and numeric_cols:
            y = next((c for c in numeric_cols if c != x), numeric_cols[0])

    kwargs = dict(title=title, color_discrete_sequence=px.colors.qualitative.Pastel)

    fig = None
    if chart_type == "bar" and x and y:
        fig = px.bar(pdf, x=x, y=y, color=color, barmode="group", **kwargs)
    elif chart_type == "line" and x and y:
        fig = px.line(pdf, x=x, y=y, color=color, markers=True, **kwargs)
    elif chart_type == "area" and x and y:
        fig = px.area(pdf, x=x, y=y, color=color, **kwargs)
    elif chart_type == "scatter" and x and y:
        fig = px.scatter(pdf, x=x, y=y, color=color, **kwargs)
    elif chart_type == "pie" and x and y:
        fig = px.pie(pdf, names=x, values=y, **kwargs)
    elif chart_type == "histogram" and x:
        fig = px.histogram(pdf, x=x, color=color, **kwargs)

    if fig:
        fig.update_layout(height=380, margin=dict(t=40, b=20, l=20, r=20))
        st.plotly_chart(fig, use_container_width=True)


# ── cached service setup ─────────────────────────────────────────────────────


@st.cache_resource
def get_orchestrator() -> OrchestratorAgent:
    """Build the agent registry and orchestrator (once per app lifecycle)."""
    registry = AgentRegistry()
    registry.register(QueryAgent())
    registry.register(VisualizationAgent())

    conn_mgr = MCPConnectionManager()

    return OrchestratorAgent(registry=registry, connection_manager=conn_mgr)


@st.cache_resource
def get_chart_agent() -> ChartAgent:
    """Create the chart agent (once per app lifecycle)."""
    return ChartAgent()


# ── sidebar filters ──────────────────────────────────────────────────────────


def _get_channel_options() -> list[str]:
    if not DB_PATH.exists():
        return []
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        rows = conn.execute(
            "SELECT DISTINCT channel FROM orders WHERE channel IS NOT NULL ORDER BY channel"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


@st.cache_data
def get_date_bounds() -> tuple[date, date]:
    if not DB_PATH.exists():
        today = date.today()
        return today - timedelta(days=365), today
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        row = conn.execute(
            "SELECT MIN(created_date::DATE), MAX(created_date::DATE) FROM orders"
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0], row[1]
    except Exception:
        pass
    today = date.today()
    return today - timedelta(days=365), today


with st.sidebar:
    st.header("Filters")

    min_date, max_date = get_date_bounds()
    date_from = st.date_input("From", value=min_date, min_value=min_date, max_value=max_date)
    date_to = st.date_input("To", value=max_date, min_value=min_date, max_value=max_date)

    channel_options = _get_channel_options()
    selected_channels = st.multiselect(
        "Channel", options=channel_options, default=[], placeholder="All channels"
    )

    st.divider()
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.session_state.chart_pending = None
        st.rerun()


def _build_filter_context() -> str:
    parts = [f"Only include data where created_date is between {date_from} and {date_to}."]
    if selected_channels:
        quoted = ", ".join(f"'{c}'" for c in selected_channels)
        parts.append(f"Limit results to channel(s): {quoted}.")
    return " ".join(parts)


# ── dashboard button ─────────────────────────────────────────────────────────


def _render_dashboard_button() -> None:
    if not REPORTS_DIR.exists():
        return
    reports = sorted(REPORTS_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return
    b64 = base64.b64encode(reports[0].read_bytes()).decode()
    _components.html(
        f"""
        <script>
        function openDashboard() {{
            const bytes = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
            const blob  = new Blob([bytes], {{type: "text/html"}});
            window.open(URL.createObjectURL(blob), "_blank");
        }}
        </script>
        <button onclick="openDashboard()" style="
            width:100%;padding:8px 12px;background:#ff4b4b;color:white;
            border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:500;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
            📋 Dashboard
        </button>
        """,
        height=45,
    )


# ── main chat area ───────────────────────────────────────────────────────────

title_col, dash_col = st.columns([8, 1])
title_col.title("✨ BI Chat")
title_col.caption("Ask questions about your orders data in plain English.")
with dash_col:
    _render_dashboard_button()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "chart_pending" not in st.session_state:
    st.session_state.chart_pending = None
    # Structure:
    # {
    #   "df_json": str,               # serialized Polars DataFrame
    #   "original_question": str,      # the query that produced the DataFrame
    #   "last_sql": str,              # SQL that produced the DataFrame
    #   "phase": "ask_plot" | "charting",
    #   "history": [{"role": "user"|"assistant", "content": str}, ...]
    # }

orchestrator = get_orchestrator()
chart_agent = get_chart_agent()
llm_complete = _make_llm_complete()

# ── render history ───────────────────────────────────────────────────────────

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # Re-render saved charts from history
        if msg.get("chart_config") and msg.get("df_json"):
            df = pl.read_json(msg["df_json"].encode())
            render_chart(df, msg["chart_config"])

# ── handle new input ─────────────────────────────────────────────────────────

if question := st.chat_input("Ask a question about the data..."):
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    chart_conv = st.session_state.chart_pending
    reply = ""
    chart_config = None
    df_json = None

    with st.chat_message("assistant"):

        # ── Phase: waiting for "yes/no" to plot offer ────────────────────
        if chart_conv and chart_conv["phase"] == "ask_plot":
            answer = _classify_yes_no(question, llm_complete)

            if answer == "yes":
                # Start the ChartAgent conversation
                df = pl.read_json(chart_conv["df_json"].encode())
                original_q = chart_conv["original_question"]
                source_sql = chart_conv.get("last_sql", "")
                db_schema = get_db_schema_cached()
                first_msg = f"I want to visualize the results of: \"{original_q}\""
                history = [{"role": "user", "content": first_msg}]

                with st.spinner("Thinking about the best chart..."):
                    response = chart_agent.respond(
                        df, history,
                        original_question=original_q,
                        source_sql=source_sql,
                        db_schema=db_schema,
                    )

                spec = parse_chart_spec(response)

                if spec:
                    # ChartAgent had enough info — render immediately
                    # Execute SQL from chart-spec to get the right data
                    chart_df = _get_chart_df(spec, df)
                    render_chart(chart_df, spec)
                    clean_text = clean_response(response)
                    reply = clean_text or f"Here's your **{spec.get('chart_type', 'chart')}** chart!"
                    chart_config = spec
                    df_json = chart_df.write_json()
                    st.session_state.chart_pending = None
                else:
                    # ChartAgent wants to ask a question
                    history.append({"role": "assistant", "content": response})
                    st.session_state.chart_pending = {
                        **chart_conv,
                        "phase": "charting",
                        "history": history,
                    }
                    reply = response
            else:
                st.session_state.chart_pending = None
                reply = "No problem! Let me know if you have more questions."

            st.markdown(reply)

        # ── Phase: active ChartAgent conversation ────────────────────────
        elif chart_conv and chart_conv["phase"] == "charting":
            df = pl.read_json(chart_conv["df_json"].encode())
            original_q = chart_conv["original_question"]
            source_sql = chart_conv.get("last_sql", "")
            db_schema = get_db_schema_cached()
            chart_conv["history"].append({"role": "user", "content": question})

            with st.spinner("Thinking..."):
                response = chart_agent.respond(
                    df, chart_conv["history"],
                    original_question=original_q,
                    source_sql=source_sql,
                    db_schema=db_schema,
                )

            spec = parse_chart_spec(response)

            if spec:
                # Got chart-spec — execute SQL and render
                chart_df = _get_chart_df(spec, df)
                render_chart(chart_df, spec)
                clean_text = clean_response(response)
                reply = clean_text or f"Here's your **{spec.get('chart_type', 'chart')}** chart!"
                chart_config = spec
                df_json = chart_df.write_json()
                st.session_state.chart_pending = None
            else:
                # Still asking questions — continue conversation
                chart_conv["history"].append({"role": "assistant", "content": response})
                reply = response

            st.markdown(reply)

        # ── No active chart conversation ─────────────────────────────────
        else:
            # Check for reformat intent (e.g., "show that as a bar chart")
            last_df_msg = next(
                (m for m in reversed(st.session_state.messages) if m.get("df_json")),
                None,
            )
            intent = classify_intent(
                question, llm_complete, has_previous_df=last_df_msg is not None
            )

            if intent == "reformat" and last_df_msg:
                # Reformat: send to ChartAgent — it usually returns chart-spec immediately
                df = pl.read_json(last_df_msg["df_json"].encode())
                history = [{"role": "user", "content": question}]
                original_q = last_df_msg.get("question", question)
                source_sql = last_df_msg.get("last_sql", "")
                db_schema = get_db_schema_cached()

                with st.spinner("Reformatting chart..."):
                    response = chart_agent.respond(
                        df, history,
                        original_question=original_q,
                        source_sql=source_sql,
                        db_schema=db_schema,
                    )

                spec = parse_chart_spec(response)

                if spec:
                    chart_df = _get_chart_df(spec, df)
                    render_chart(chart_df, spec)
                    reply = f"Here's the data as a **{spec.get('chart_type', 'chart')}** chart."
                    chart_config = spec
                    df_json = chart_df.write_json()
                else:
                    # Rare: ChartAgent needs clarification for reformat
                    st.session_state.chart_pending = {
                        "df_json": last_df_msg["df_json"],
                        "original_question": original_q,
                        "last_sql": source_sql,
                        "phase": "charting",
                        "history": [
                            {"role": "user", "content": question},
                            {"role": "assistant", "content": response},
                        ],
                    }
                    reply = response

                st.markdown(reply)

            else:
                # Regular query — route through orchestrator
                filter_context = _build_filter_context()
                context = {"filter_context": filter_context}

                with st.spinner("Querying..."):
                    result = orchestrator.query(question, context=context)

                st.markdown(result.text)

                if result.dashboard_html:
                    # Dashboard generated — available via Dashboard button
                    reply = result.text

                elif result.data is not None:
                    # QueryAgent returned a DataFrame — offer to plot
                    st.markdown("Would you like me to plot this data?")
                    st.session_state.chart_pending = {
                        "df_json": result.data.write_json(),
                        "original_question": question,
                        "last_sql": result.last_sql or "",
                        "phase": "ask_plot",
                        "history": [],
                    }
                    reply = result.text + "\n\nWould you like me to plot this data?"

                else:
                    reply = result.text

    # ── Save message to history ──────────────────────────────────────────
    # Track last_sql so reformat can pass it to ChartAgent
    last_sql = ""
    if chart_config and chart_config.get("sql"):
        last_sql = chart_config["sql"]
    elif chart_conv and chart_conv.get("last_sql"):
        last_sql = chart_conv["last_sql"]

    st.session_state.messages.append({
        "role": "assistant",
        "content": reply,
        "chart_config": chart_config,
        "df_json": df_json,
        "last_sql": last_sql,
        "question": question,
    })
