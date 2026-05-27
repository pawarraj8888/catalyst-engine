"""Point-in-time (PIT) discipline.

This module is the heart of the project's research validity. Every feature
computation and every backtest query must go through `pit_query()` or
`PITContext`. Direct SQL against the warehouse without an `as_of` filter is
forbidden and will fail review.

The contract
------------
1. Every row in the warehouse has an `as_of` column (the earliest moment the
   row was observable to a real-time consumer).
2. To compute a feature "as it would have looked at time T", we filter every
   joined row to `as_of <= T`.
3. Backtests run inside a `PITContext(as_of=T)`; any feature module that
   ignores the context is broken.

If you find yourself writing `WHERE as_of <= ?` by hand, use this module instead.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    import duckdb


@dataclass(frozen=True)
class PITContext:
    """The point in time at which features should be evaluated.

    `as_of` is timezone-aware UTC. Construct via `PITContext.at(...)` or
    `PITContext.now()` rather than the bare constructor when possible.
    """

    as_of: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError(
                "PITContext.as_of must be timezone-aware. "
                "Use PITContext.at(dt) which UTC-normalizes for you."
            )

    @classmethod
    def at(cls, dt: datetime) -> PITContext:
        """Construct from a datetime, normalizing to UTC."""
        if dt.tzinfo is None:
            raise ValueError("Naive datetime is not allowed. Specify a timezone.")
        return cls(as_of=dt.astimezone(timezone.utc))

    @classmethod
    def now(cls) -> PITContext:
        """Construct with the current UTC moment."""
        return cls(as_of=datetime.now(timezone.utc))


# Process-wide current PIT. Set by enter_pit(); read by pit_query().
_current_pit: ContextVar[PITContext | None] = ContextVar("_current_pit", default=None)


@contextmanager
def enter_pit(ctx: PITContext) -> Iterator[PITContext]:
    """Set the active PIT context for the duration of the block.

    Usage
    -----
    >>> with enter_pit(PITContext.at(datetime(2025, 6, 1, tzinfo=timezone.utc))):
    ...     setup = build_earnings_setup("AAPL")  # sees only data <= 2025-06-01

    Nesting raises — backtests should establish exactly one PIT per query.
    """
    if _current_pit.get() is not None:
        raise RuntimeError(
            "PIT contexts cannot be nested. The outer context is already active."
        )
    token = _current_pit.set(ctx)
    try:
        yield ctx
    finally:
        _current_pit.reset(token)


def current_pit() -> PITContext:
    """Return the active PIT, or raise if none is set.

    Feature modules call this before every query. If it raises, the caller
    forgot to wrap their backtest in `enter_pit(...)`.
    """
    ctx = _current_pit.get()
    if ctx is None:
        raise RuntimeError(
            "No active PIT context. Wrap your query in `with enter_pit(PITContext.at(...))`."
        )
    return ctx


def pit_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: tuple[object, ...] = (),
    *,
    as_of: datetime | None = None,
) -> list[tuple[object, ...]]:
    """Execute a SQL query with the active PIT filter applied.

    The SQL string MUST contain the literal token `{AS_OF}`. This token is
    substituted with a properly quoted UTC timestamp before execution. Queries
    that don't contain `{AS_OF}` raise — that's the lint.

    Parameters
    ----------
    conn : duckdb connection
    sql  : query string with `{AS_OF}` placeholder
    params : positional parameters (passed through to DuckDB)
    as_of  : override the active PIT. Mostly used in tests.

    Returns
    -------
    List of tuples (DuckDB rowset).

    Examples
    --------
    >>> sql = '''
    ...     SELECT ticker, eps_est
    ...     FROM earnings_events
    ...     WHERE ticker = ? AND as_of <= {AS_OF}
    ...     ORDER BY as_of DESC LIMIT 1
    ... '''
    >>> rows = pit_query(conn, sql, ("AAPL",))
    """
    if "{AS_OF}" not in sql:
        raise ValueError(
            "pit_query requires `{AS_OF}` in the SQL string. "
            "If your query truly does not need an as_of filter (e.g. a static "
            "reference table), use conn.execute() directly and justify it in a comment."
        )

    ctx = PITContext.at(as_of) if as_of is not None else current_pit()
    as_of_literal = f"TIMESTAMP '{ctx.as_of.isoformat()}'"
    bound_sql = sql.replace("{AS_OF}", as_of_literal)
    return conn.execute(bound_sql, params).fetchall()
