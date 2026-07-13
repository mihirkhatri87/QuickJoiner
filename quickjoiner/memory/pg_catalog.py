"""Postgres catalog adapter (cloud mode). Shares all SQL with the SQLite Catalog via
_SqlCatalog; only the three primitives differ: a psycopg connection pool (thread-safe
for the API threadpool), autocommit, dict rows, and `?` -> `%s` placeholder conversion.

Requires the `cloud` extra (`psycopg`, `psycopg-pool`) and a Postgres with the pgvector
extension available (only the vector store uses pgvector; the catalog uses plain tables).
"""

from __future__ import annotations

from quickjoiner.memory.catalog import _SCHEMA_STATEMENTS, _SqlCatalog


class PostgresCatalog(_SqlCatalog):
    workspace = None  # no local config.yaml to migrate in cloud mode

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Cloud mode needs the 'cloud' extra: pip install -e \".[cloud]\""
            ) from exc

        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        with self._pool.connection() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)

    @staticmethod
    def _pg(sql: str) -> str:
        # No literal '?' or '%' appears in the catalog SQL, so this swap is safe.
        return sql.replace("?", "%s")

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._pool.connection() as conn:
            conn.execute(self._pg(sql), params)

    def _read_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._pool.connection() as conn:
            return conn.execute(self._pg(sql), params).fetchone()  # dict_row -> dict | None

    def _read_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._pool.connection() as conn:
            return conn.execute(self._pg(sql), params).fetchall()

    def close(self) -> None:
        self._pool.close()
