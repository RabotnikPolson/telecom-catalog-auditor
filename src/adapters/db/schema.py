import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

LATEST_SCHEMA_VERSION: int = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    shop_sku TEXT,
    title TEXT NOT NULL,
    barcode TEXT,
    brand_name TEXT,
    vendor_name TEXT,
    vendor_sku TEXT,
    manufacturer_sku TEXT,
    description TEXT,
    images_count INTEGER NOT NULL DEFAULT 0,
    current_specs TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parent_sku TEXT,
    master_key TEXT,
    is_master INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_shop_sku ON products(shop_sku);
CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
CREATE INDEX IF NOT EXISTS idx_products_brand_name ON products(brand_name);
CREATE INDEX IF NOT EXISTS idx_products_content_hash ON products(content_hash);
CREATE INDEX IF NOT EXISTS idx_products_master_key ON products(master_key);
CREATE INDEX IF NOT EXISTS idx_products_parent_sku ON products(parent_sku);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);

CREATE TABLE IF NOT EXISTS url_cache (
    query_hash TEXT PRIMARY KEY,
    resolved_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_url_cache_source ON url_cache(source_type);
CREATE INDEX IF NOT EXISTS idx_url_cache_resolved_url ON url_cache(resolved_url);
CREATE INDEX IF NOT EXISTS idx_url_cache_created_at ON url_cache(created_at);

CREATE TABLE IF NOT EXISTS specs_cache (
    resolved_url TEXT PRIMARY KEY,
    specs_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    raw_html_hash TEXT,
    status_code INTEGER DEFAULT 200,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_url_cache_expires ON url_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_specs_cache_fetched_at ON specs_cache(fetched_at);
CREATE INDEX IF NOT EXISTS idx_specs_cache_expires ON specs_cache(expires_at);

CREATE TABLE IF NOT EXISTS audit_results (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    reference_url TEXT,
    discrepancies_json TEXT,
    missing_specs_json TEXT,
    matched_specs_count INTEGER NOT NULL,
    total_specs_count INTEGER NOT NULL,
    images_count INTEGER NOT NULL DEFAULT 0,
    reference_images_count INTEGER NOT NULL DEFAULT 0,
    images_status TEXT NOT NULL DEFAULT 'OK',
    description_discrepancies_json TEXT,
    description_quality TEXT NOT NULL DEFAULT 'GOOD',
    audited_at TEXT NOT NULL,
    details TEXT,
    execution_time_sec REAL DEFAULT 0.0,
    crawler_time_sec REAL DEFAULT 0.0,
    llm_time_sec REAL DEFAULT 0.0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    search_trace_json TEXT,
    audit_parts_json TEXT,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_results_product_unique ON audit_results(product_id);
CREATE INDEX IF NOT EXISTS idx_audit_results_status ON audit_results(status);

CREATE TABLE IF NOT EXISTS crawler_state (
    category_key TEXT PRIMARY KEY,
    last_page INTEGER NOT NULL DEFAULT 1,
    total_pages INTEGER NOT NULL DEFAULT 1,
    is_completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawler_state_completed ON crawler_state(is_completed);
"""


@contextmanager
def get_sqlite_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA busy_timeout = 15000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -64000;")
    conn.execute("PRAGMA mmap_size = 268435456;")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _try_add_column(conn: sqlite3.Connection, table: str, column_def: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def};")
    except sqlite3.OperationalError as exc:
        logger.debug("Column migration skipped on %s (%s): %s", table, column_def, exc)


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_sqlite_connection(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA wal_autocheckpoint = 1000;")

        ver_row = conn.execute("PRAGMA user_version;").fetchone()
        current_version = int(ver_row[0]) if ver_row else 0

        if current_version < LATEST_SCHEMA_VERSION:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products';")
            if cursor.fetchone():
                col_cursor = conn.execute("PRAGMA table_info(products);")
                columns = [row["name"] for row in col_cursor.fetchall()]
                if "barcode" not in columns:
                    _try_add_column(conn, "products", "barcode TEXT")
                if "brand_name" not in columns:
                    _try_add_column(conn, "products", "brand_name TEXT")
                if "shop_sku" not in columns:
                    _try_add_column(conn, "products", "shop_sku TEXT")
                if "description" not in columns:
                    _try_add_column(conn, "products", "description TEXT")
                if "images_count" not in columns:
                    _try_add_column(conn, "products", "images_count INTEGER NOT NULL DEFAULT 0")

            specs_cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='specs_cache';")
            if specs_cursor.fetchone():
                s_cols = [row["name"] for row in conn.execute("PRAGMA table_info(specs_cache);").fetchall()]
                if "expires_at" not in s_cols:
                    _try_add_column(conn, "specs_cache", "expires_at TEXT")
                conn.execute("UPDATE specs_cache SET expires_at = datetime(fetched_at, '+30 days') WHERE expires_at IS NULL;")

            url_cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='url_cache';")
            if url_cursor.fetchone():
                conn.execute("UPDATE url_cache SET expires_at = datetime(created_at, '+30 days') WHERE expires_at IS NULL;")

            audit_cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_results';")
            if audit_cursor.fetchone():
                a_cols = [row["name"] for row in conn.execute("PRAGMA table_info(audit_results);").fetchall()]
                migrations = [
                    ("missing_specs_json", "missing_specs_json TEXT"),
                    ("execution_time_sec", "execution_time_sec REAL DEFAULT 0.0"),
                    ("crawler_time_sec", "crawler_time_sec REAL DEFAULT 0.0"),
                    ("llm_time_sec", "llm_time_sec REAL DEFAULT 0.0"),
                    ("input_tokens", "input_tokens INTEGER DEFAULT 0"),
                    ("output_tokens", "output_tokens INTEGER DEFAULT 0"),
                    ("estimated_cost_usd", "estimated_cost_usd REAL DEFAULT 0.0"),
                    ("search_trace_json", "search_trace_json TEXT"),
                    ("images_count", "images_count INTEGER NOT NULL DEFAULT 0"),
                    ("reference_images_count", "reference_images_count INTEGER NOT NULL DEFAULT 0"),
                    ("images_status", "images_status TEXT NOT NULL DEFAULT 'OK'"),
                    ("description_discrepancies_json", "description_discrepancies_json TEXT"),
                    ("description_quality", "description_quality TEXT NOT NULL DEFAULT 'GOOD'"),
                    ("audit_parts_json", "audit_parts_json TEXT"),
                ]
                for col_name, col_def in migrations:
                    if col_name not in a_cols:
                        _try_add_column(conn, "audit_results", col_def)

                if current_version < 3:
                    conn.execute("""
                        DELETE FROM audit_results
                        WHERE audit_id NOT IN (
                            SELECT MAX(audit_id) FROM audit_results GROUP BY product_id
                        );
                    """)
                    conn.execute("DROP INDEX IF EXISTS idx_audit_results_product;")

        conn.executescript(SCHEMA_SQL)
        if current_version < LATEST_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION};")
        conn.commit()



