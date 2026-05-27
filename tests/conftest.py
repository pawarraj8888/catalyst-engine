"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from catalyst_engine.db import connect
from catalyst_engine.utils.logging import configure_logging


@pytest.fixture(scope="session", autouse=True)
def _configure_logging() -> None:
    """Configure structlog once per test session."""
    configure_logging()


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A fresh, isolated DuckDB warehouse per test.

    The schema is auto-applied by `connect()`. Tests can insert fixtures and
    are guaranteed not to interfere with each other or with the dev warehouse.
    """
    db_path = tmp_path / "test.duckdb"
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
