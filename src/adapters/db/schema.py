from pathlib import Path
import sqlite3
from typing import Union

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    barcode TEXT,
    vendor_name TEXT,
    vendor_sku TEXT,
    manufacturer_sku TEXT,
    current_specs TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parent_sku TEXT,
    master_key TEXT,
    is_master INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
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

CREATE TABLE IF NOT EXISTS specs_cache (
    resolved_url TEXT PRIMARY KEY,
    specs_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    raw_html_hash TEXT,
    status_code INTEGER DEFAULT 200
);

CREATE TABLE IF NOT EXISTS audit_results (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    reference_url TEXT,
    discrepancies_json TEXT,
    matched_specs_count INTEGER NOT NULL,
    total_specs_count INTEGER NOT NULL,
    audited_at TEXT NOT NULL,
    details TEXT,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audit_results_product ON audit_results(product_id);
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


def get_sqlite_connection(db_path: Union[str, Path]) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    return conn


def init_db(db_path: Union[str, Path]) -> None:
    with get_sqlite_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        cursor = conn.execute("PRAGMA table_info(products);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "barcode" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN barcode TEXT;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);")
        conn.commit()

