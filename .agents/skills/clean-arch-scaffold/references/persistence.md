# Persistence and streaming

## SQLite

- Use `with get_sqlite_connection(db_path) as conn:` for normal repository work.
- Keep per-connection PRAGMAs and one-time WAL setup in their existing responsibilities.
- Migrations are conditional on `PRAGMA user_version`, work on an existing database and preserve unique indexes backing `ON CONFLICT`.
- Product and audit result are written atomically by `save_product_and_audit_result` only in the JIT audit flow.
- URL cache keys use the product and normalized query; specs cache keys use the resolved URL.

## MySQL

- MySQL is read-only and streamed with `fetchmany`; never use `fetchall` for the catalog.
- Keep keyset pagination and release the batch connection before yielding.
- Category or main-category selection never falls back to SQLite.
