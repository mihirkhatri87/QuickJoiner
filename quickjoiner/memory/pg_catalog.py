"""Postgres catalog adapter (cloud mode). Shares all SQL with the SQLite Catalog via
_SqlCatalog; only the three primitives differ: a psycopg connection pool (thread-safe
for the API threadpool), autocommit, dict rows, and `?` -> `%s` placeholder conversion.

Requires the `cloud` extra (`psycopg`, `psycopg-pool`) and a Postgres with the pgvector
extension available (only the vector store uses pgvector; the catalog uses plain tables).
"""

from __future__ import annotations

from quickjoiner.memory.catalog import _MIGRATION_STATEMENTS, _SCHEMA_STATEMENTS, _SqlCatalog


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
        # Same late-added columns as the SQLite adapter. Each runs in its own connection:
        # on Postgres a duplicate-column error aborts the surrounding transaction, so a
        # shared one would poison the statements after it.
        for stmt in _MIGRATION_STATEMENTS:
            try:
                with self._pool.connection() as conn:
                    conn.execute(stmt)
            except Exception:  # noqa: BLE001 — column already exists
                pass

    @staticmethod
    def _pg(sql: str) -> str:
        """Translate the shared `?`-SQL to psycopg's `%s` form.

        `%` is escaped FIRST (and the `?` swap therefore second, so the `%s` markers this
        very function writes are not re-escaped). psycopg treats a bare `%` anywhere in the
        statement — including inside a `--` comment — as the start of a placeholder and
        raises `incomplete placeholder`, which the SQLite adapter never sees. That is not a
        hypothetical: a `-- 57% degree-1 nodes` comment in `graph_snapshot`'s query broke
        the whole-graph view on Postgres only, and it took running these tests against a
        real Postgres to find, because this comment previously asserted that no literal `%`
        appears in the catalog SQL. It does now, so the escape is done rather than assumed.
        Query *values* never pass through here — a LIKE pattern is a bound parameter — so
        escaping the statement text cannot affect a wildcard.
        """
        return sql.replace("%", "%%").replace("?", "%s")

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
