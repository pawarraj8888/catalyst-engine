"""Point-in-time correctness tests.

These tests are the contract. They enforce that:
1. PITContext rejects naive datetimes.
2. pit_query() refuses SQL that lacks an {AS_OF} placeholder.
3. Queries return only rows whose as_of <= the active PIT.
4. Nesting PIT contexts is an error.
5. Querying without an active PIT is an error.

Every test in this file is marked @pytest.mark.pit. CI fails if any of them
fail. This is non-negotiable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import duckdb
import pytest

from catalyst_engine.utils.pit import (
    PITContext,
    current_pit,
    enter_pit,
    pit_query,
)

# ----------------------------------------------------------------------------
# PITContext construction
# ----------------------------------------------------------------------------


@pytest.mark.pit
def test_pit_context_rejects_naive_datetime() -> None:
    naive = datetime(2025, 6, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        PITContext(as_of=naive)


@pytest.mark.pit
def test_pit_context_at_rejects_naive() -> None:
    with pytest.raises(ValueError, match="Naive datetime"):
        PITContext.at(datetime(2025, 6, 1))


@pytest.mark.pit
def test_pit_context_at_normalizes_to_utc() -> None:
    # 09:00 in EST (-05:00) == 14:00 UTC
    est = timezone(timedelta(hours=-5))
    dt = datetime(2025, 6, 1, 9, 0, tzinfo=est)
    ctx = PITContext.at(dt)
    assert ctx.as_of.tzinfo is UTC
    assert ctx.as_of.hour == 14


@pytest.mark.pit
def test_pit_context_now_is_utc() -> None:
    ctx = PITContext.now()
    assert ctx.as_of.tzinfo is UTC


# ----------------------------------------------------------------------------
# enter_pit / current_pit / nesting
# ----------------------------------------------------------------------------


@pytest.mark.pit
def test_current_pit_raises_without_active_context() -> None:
    with pytest.raises(RuntimeError, match="No active PIT"):
        current_pit()


@pytest.mark.pit
def test_enter_pit_sets_then_clears() -> None:
    ctx = PITContext.now()
    with enter_pit(ctx):
        assert current_pit() is ctx
    with pytest.raises(RuntimeError):
        current_pit()


@pytest.mark.pit
def test_nested_pit_contexts_raise() -> None:
    outer = PITContext.now()
    inner = PITContext.at(datetime(2025, 1, 1, tzinfo=UTC))
    with enter_pit(outer), pytest.raises(RuntimeError, match="cannot be nested"), enter_pit(inner):
        pass


# ----------------------------------------------------------------------------
# pit_query — the SQL contract
# ----------------------------------------------------------------------------


@pytest.mark.pit
def test_pit_query_requires_as_of_token() -> None:
    conn = duckdb.connect(":memory:")
    with enter_pit(PITContext.now()), pytest.raises(ValueError, match=r"requires `{AS_OF}`"):
        pit_query(conn, "SELECT 1")  # no {AS_OF} → forbidden


@pytest.mark.pit
def test_pit_query_filters_future_rows(warehouse: duckdb.DuckDBPyConnection) -> None:
    """A row with as_of in the future relative to the PIT must NOT be returned.

    This is the entire point of the PIT layer. If this test fails, the project
    is leaking data.
    """
    # Insert two universe rows with different as_of timestamps
    past_as_of = datetime(2025, 1, 1, tzinfo=UTC)
    future_as_of = datetime(2026, 1, 1, tzinfo=UTC)

    warehouse.execute(
        """
        INSERT INTO universe (ticker, cik, company_name, sector, start_date, as_of, source)
        VALUES
            ('PAST', '0000000001', 'Past Co', 'tech', '2024-01-01', ?, 'test'),
            ('FUT',  '0000000002', 'Future Co', 'tech', '2024-01-01', ?, 'test')
        """,
        [past_as_of, future_as_of],
    )

    # Query at a PIT between the two
    mid_pit = datetime(2025, 6, 1, tzinfo=UTC)
    with enter_pit(PITContext.at(mid_pit)):
        rows = pit_query(
            warehouse,
            "SELECT ticker FROM universe WHERE as_of <= {AS_OF} ORDER BY ticker",
        )

    tickers = [row[0] for row in rows]
    assert tickers == ["PAST"], f"PIT leak: future row visible. Got {tickers}"


@pytest.mark.pit
def test_pit_query_allows_override(warehouse: duckdb.DuckDBPyConnection) -> None:
    """The `as_of` override parameter bypasses the active context.

    This is intentional for tests and for explicit retrospective queries.
    """
    early = datetime(2024, 1, 1, tzinfo=UTC)
    late = datetime(2025, 1, 1, tzinfo=UTC)

    warehouse.execute(
        """
        INSERT INTO universe (ticker, cik, sector, start_date, as_of, source)
        VALUES ('X', '0000000003', 'tech', '2024-01-01', ?, 'test')
        """,
        [late],
    )

    # Active PIT is "before" the row's as_of
    with enter_pit(PITContext.at(early)):
        rows_default = pit_query(warehouse, "SELECT ticker FROM universe WHERE as_of <= {AS_OF}")
        assert rows_default == []

        # Override to "after" — should see it
        rows_override = pit_query(
            warehouse,
            "SELECT ticker FROM universe WHERE as_of <= {AS_OF}",
            as_of=late + timedelta(days=1),
        )
        assert rows_override == [("X",)]
