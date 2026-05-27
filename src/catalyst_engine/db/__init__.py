"""Warehouse access — DuckDB connection management and schema bootstrap."""

from catalyst_engine.db.connection import connect, ensure_schema

__all__ = ["connect", "ensure_schema"]
