"""3-tier schema pruning for text-to-SQL queries.

Tier 1: Select relevant tables via keyword hints.
Tier 2: Prune columns to those relevant to the question.
Tier 3: Annotate low-cardinality VARCHAR columns with distinct values.
"""
import duckdb

# ── Keyword hint sets for table selection (Tier 1) ────────────────────────────

ORDERS_HINTS: frozenset[str] = frozenset({
    "order", "orders", "header", "headers", "channel", "channels",
    "customer", "customers", "date", "dates", "month", "monthly",
    "year", "yearly", "annual", "daily", "week", "weekly", "when",
})

LINES_HINTS: frozenset[str] = frozenset({
    "sku", "skus", "stylecode", "product", "products", "style", "styles",
    "line", "lines", "name", "names",
})

ITEMS_HINTS: frozenset[str] = frozenset({
    "category", "categories", "subcategory", "subcategories", "item", "items",
    "type", "types", "segment", "segments",
})

# ── Constants ─────────────────────────────────────────────────────────────────

_MAX_DISTINCT_FOR_VALUE_HINTS: int = 20
_MIN_LINKED_COLS: int = 4
_JOIN_KEYS: frozenset[str] = frozenset({"original_reference"})


def compute_value_hints(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    col_types: dict[str, str],
) -> dict[str, str | None]:
    """Return per-column value annotations for low-cardinality VARCHAR columns."""
    hints: dict[str, str | None] = {}
    for col, dtype in col_types.items():
        if not dtype.upper().startswith("VARCHAR"):
            hints[col] = None
            continue
        n_distinct = conn.execute(
            f"SELECT COUNT(DISTINCT {col}) FROM {table}"
        ).fetchone()[0]
        if n_distinct > _MAX_DISTINCT_FOR_VALUE_HINTS:
            hints[col] = None
            continue
        vals = conn.execute(
            f"SELECT DISTINCT {col} FROM {table} "
            f"WHERE {col} IS NOT NULL ORDER BY {col} "
            f"LIMIT {_MAX_DISTINCT_FOR_VALUE_HINTS}"
        ).fetchall()
        sample = ", ".join(repr(v[0]) for v in vals)
        hints[col] = f"  -- values: {sample}"
    return hints


def link_columns(
    question: str,
    col_types: dict[str, str],
    value_hints: dict[str, str | None],
) -> list[str]:
    """Return columns relevant to the question (Tier 2)."""
    all_cols = list(col_types)
    if len(all_cols) <= _MIN_LINKED_COLS:
        return all_cols

    words = frozenset(w.strip("?.,;!\"'()[]") for w in question.lower().split())

    def _token_matches(token: str) -> bool:
        for word in words:
            if token == word:
                return True
            if len(token) > 3 and word.startswith(token):
                return True
            if len(word) > 3 and token.startswith(word):
                return True
        return False

    def _is_linked(col: str) -> bool:
        if col in _JOIN_KEYS:
            return True
        if any(_token_matches(tok) for tok in col.split("_")):
            return True
        hint = value_hints.get(col)
        if hint:
            raw = hint.split("values:")[-1]
            col_vals = {v.strip().strip("'\"") for v in raw.split(",")}
            if col_vals & words:
                return True
        return False

    linked = [c for c in all_cols if _is_linked(c)]
    return linked if len(linked) >= _MIN_LINKED_COLS else all_cols


def select_schema(
    table_ddls: dict[str, str],
    col_types: dict[str, dict[str, str]],
    value_hints: dict[str, dict[str, str | None]],
    question: str,
) -> tuple[str, list[str]]:
    """Return (annotated schema DDL, selected table names) for the given question."""
    words = frozenset(question.lower().split())
    wants_orders = bool(words & ORDERS_HINTS)
    wants_lines = bool(words & LINES_HINTS)
    wants_items = bool(words & ITEMS_HINTS)

    if wants_items and not wants_orders and not wants_lines:
        selected = [t for t in ["items", "order_lines"] if t in table_ddls]
    elif wants_orders and not wants_lines and not wants_items and "orders" in table_ddls:
        selected = ["orders"]
    elif wants_lines and not wants_orders and not wants_items and "order_lines" in table_ddls:
        selected = ["order_lines"]
    else:
        selected = sorted(table_ddls.keys())

    ddl_parts: list[str] = []
    for table in selected:
        t_col_types = col_types.get(table, {})
        if not t_col_types:
            ddl_parts.append(table_ddls.get(table, f"-- table {table} unavailable"))
            continue

        t_value_hints = value_hints.get(table, {})
        linked_cols = link_columns(question, t_col_types, t_value_hints)

        col_lines = [
            f"  {col} {t_col_types[col]}{t_value_hints.get(col) or ''}"
            for col in linked_cols
        ]
        ddl_parts.append(f"CREATE TABLE {table} (\n" + ",\n".join(col_lines) + "\n);")

    return "\n\n".join(ddl_parts), selected
