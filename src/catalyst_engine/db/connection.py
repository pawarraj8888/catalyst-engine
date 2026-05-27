"""DuckDB connection management.

Single entry point: `connect()`. Schema is created lazily on first connect.
Tests can pass a custom path for an in-memory or temp DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from catalyst_engine.config import get_settings
from catalyst_engine.utils.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply schema.sql to the given connection. Idempotent."""
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.execute(schema_sql)
    log.debug("schema_applied", path=str(_SCHEMA_PATH))


def connect(
    path: Path | str | None = None, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection and ensure schema exists.

    Parameters
    ----------
    path : Path | str | None
        Override warehouse path. Defaults to Settings.duckdb_path.
        Use ":memory:" for an ephemeral in-memory DB (tests).
    read_only : bool
        If True, opens read-only. Schema is NOT auto-applied (the DB must
        already exist).

    Returns
    -------
    duckdb.DuckDBPyConnection
    """
    if path is None:
        db_path = get_settings().duckdb_path
    else:
        db_path = Path(path) if path != ":memory:" else Path(":memory:")

    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path), read_only=read_only)

    if not read_only:
        ensure_schema(conn)

    log.debug("warehouse_connected", path=str(db_path), read_only=read_only)
    return conn
