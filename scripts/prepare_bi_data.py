#!/usr/bin/env python3
"""Load raw BI JSON files directly into DuckDB.

Uses DuckDB's read_json_auto() to ingest all records from:
  HeaderResults.json  →  table 'orders'
  LinesResults.json   →  table 'order_lines'

No sampling, no pre-aggregation. The text-to-SQL service runs
arbitrary SQL against the full dataset at query time. DuckDB
handles 100K–500K rows in seconds without Python buffering.

Usage:
    python scripts/prepare_bi_data.py [raw_folder] [db_path]

    raw_folder   default: local_data/uploads
    db_path      default: local_data/bi.db
"""
import logging
import re
import sys
from pathlib import Path

import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def to_snake_case(name: str) -> str:
    """Convert PascalCase, camelCase, or spaced column names to snake_case."""
    # Handle transitions from lowercase/digit to uppercase (e.g. createdDate → created_Date)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    # Handle runs of uppercase followed by an uppercase+lowercase (e.g. HTMLParser → HTML_Parser)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    # Replace any whitespace runs with a single underscore
    s = re.sub(r"\s+", "_", s)
    return s.lower()


def rename_columns_to_snake_case(conn: duckdb.DuckDBPyConnection, table: str) -> None:
    """Rename all columns of *table* in-place to snake_case."""
    cols = [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    renames = [(col, to_snake_case(col)) for col in cols if col != to_snake_case(col)]
    for original, snake in renames:
        conn.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{original}" TO "{snake}"')
    if renames:
        logger.info("  → renamed %d column(s) to snake_case", len(renames))


def main() -> None:
    raw_folder = Path(sys.argv[1] if len(sys.argv) > 1 else "local_data/uploads")
    db_path = Path(sys.argv[2] if len(sys.argv) > 2 else "local_data/bi.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    header_file = raw_folder / "HeaderResults.json"
    lines_file = raw_folder / "LinesResults.json"
    items_file = raw_folder / "items.json"

    if not header_file.exists():
        logger.error("HeaderResults.json not found in %s — cannot continue.", raw_folder)
        sys.exit(1)

    conn = duckdb.connect(str(db_path))

    # Load all order headers
    logger.info("Loading %s (%.0f MB) …", header_file.name, header_file.stat().st_size / 1e6)
    conn.execute(
        f"CREATE OR REPLACE TABLE orders AS SELECT * FROM read_json_auto('{header_file.resolve()}')"
    )
    n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    logger.info("  → %d rows in 'orders'", n)
    rename_columns_to_snake_case(conn, "orders")

    # Indexes on orders: join key + common filter columns
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_ref  ON orders (original_reference)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (created_date)")
    logger.info("  → indexes created on orders (original_reference, created_date)")

    # Load all order lines (optional)
    if lines_file.exists():
        logger.info("Loading %s (%.0f MB) …", lines_file.name, lines_file.stat().st_size / 1e6)
        conn.execute(
            f"CREATE OR REPLACE TABLE order_lines AS SELECT * FROM read_json_auto('{lines_file.resolve()}')"
        )
        n = conn.execute("SELECT COUNT(*) FROM order_lines").fetchone()[0]
        logger.info("  → %d rows in 'order_lines'", n)
        rename_columns_to_snake_case(conn, "order_lines")

        # Indexes on order_lines: join key + SKU filter
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_ref  ON order_lines (original_reference)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_date ON order_lines (created_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_sku  ON order_lines (style_code)")
        logger.info("  → indexes created on order_lines (original_reference, created_date, style_code)")
    else:
        logger.warning("LinesResults.json not found in %s — skipping order lines.", raw_folder)

    # Load items reference table (optional)
    if items_file.exists():
        logger.info("Loading %s (%.0f MB) …", items_file.name, items_file.stat().st_size / 1e6)
        conn.execute(
            f"CREATE OR REPLACE TABLE items AS SELECT * FROM read_json_auto('{items_file.resolve()}')"
        )
        n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        logger.info("  → %d rows in 'items'", n)
        rename_columns_to_snake_case(conn, "items")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_sku ON items (style_code)")
        logger.info("  → index created on items (style_code)")
    else:
        logger.warning("items.json not found in %s — skipping items.", raw_folder)

    tables = conn.execute("SHOW TABLES").fetchall()
    total_rows = sum(
        conn.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0] for t in tables
    )
    conn.close()
    logger.info("Done. %d table(s), %d total rows → %s", len(tables), total_rows, db_path)


if __name__ == "__main__":
    main()
