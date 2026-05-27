"""Tests for insider sentiment features."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb

from catalyst_engine.features.insider import (
    build_insider_features,
    insider_unique_actors,
    insider_window_stats,
)

UTC = UTC


def _insert_tx(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    acc: str,
    filer: str,
    code: str,
    tx_date: date,
    shares: int,
    price: float,
    is_10b5_1: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO insider_transactions
        (accession_number, ticker, filer_name, filer_title, transaction_date,
         transaction_code, shares, price, value_usd, is_10b5_1, as_of, source)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 'test')
        """,
        [
            acc,
            ticker,
            filer,
            tx_date,
            code,
            shares,
            price,
            shares * price,
            is_10b5_1,
            datetime(tx_date.year, tx_date.month, tx_date.day),
        ],
    )


def test_window_stats_basic(warehouse: duckdb.DuckDBPyConnection) -> None:
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="a1",
        filer="Alice",
        code="P",
        tx_date=date(2025, 5, 10),
        shares=10_000,
        price=150.0,
    )
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="a2",
        filer="Bob",
        code="S",
        tx_date=date(2025, 5, 20),
        shares=5_000,
        price=160.0,
    )

    stats = insider_window_stats(
        warehouse, "AAPL", datetime(2025, 6, 1, tzinfo=UTC), window_days=30
    )
    assert stats["insider_buy_value_30d"] == 1_500_000.0
    assert stats["insider_sell_value_30d"] == 800_000.0
    assert stats["insider_net_buying_usd_30d"] == 700_000.0
    assert stats["insider_buys_count_30d"] == 1
    assert stats["insider_sells_count_30d"] == 1


def test_window_stats_excludes_10b5_1(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Pre-planned trades should not contribute to current sentiment."""
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="real",
        filer="Alice",
        code="P",
        tx_date=date(2025, 5, 10),
        shares=10_000,
        price=150.0,
    )
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="plan",
        filer="Bob",
        code="S",
        tx_date=date(2025, 5, 15),
        shares=50_000,
        price=160.0,
        is_10b5_1=True,
    )

    stats = insider_window_stats(
        warehouse,
        "AAPL",
        datetime(2025, 6, 1, tzinfo=UTC),
        window_days=30,
        exclude_10b5_1=True,
    )
    assert stats["insider_buy_value_30d"] == 1_500_000.0
    assert stats["insider_sell_value_30d"] == 0.0  # 10b5-1 sale excluded


def test_window_stats_respects_pit_window(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Transactions on/after as_of are excluded; before window-start excluded."""
    as_of = datetime(2025, 6, 1, tzinfo=UTC)
    # Just inside window (29 days back)
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="in",
        filer="A",
        code="P",
        tx_date=date(2025, 5, 3),
        shares=1_000,
        price=100.0,
    )
    # Outside window (60 days back)
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="out",
        filer="A",
        code="P",
        tx_date=date(2025, 4, 2),
        shares=1_000,
        price=100.0,
    )
    # After as_of (future) - must be excluded
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="future",
        filer="A",
        code="P",
        tx_date=date(2025, 6, 10),
        shares=1_000,
        price=100.0,
    )
    # On as_of date - excluded (strict less-than)
    _insert_tx(
        warehouse,
        ticker="AAPL",
        acc="boundary",
        filer="A",
        code="P",
        tx_date=date(2025, 6, 1),
        shares=1_000,
        price=100.0,
    )

    stats = insider_window_stats(warehouse, "AAPL", as_of, window_days=30)
    assert stats["insider_buys_count_30d"] == 1
    assert stats["insider_buy_value_30d"] == 100_000.0


def test_unique_actors_counts_distinct_filers(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """One filer with 3 transactions counts as 1; three filers with 1 each = 3."""
    # 3 buys from the same person
    for i in range(3):
        _insert_tx(
            warehouse,
            ticker="X",
            acc=f"x{i}",
            filer="Alice",
            code="P",
            tx_date=date(2025, 5, i + 1),
            shares=1_000,
            price=100.0,
        )

    actors = insider_unique_actors(warehouse, "X", datetime(2025, 6, 1, tzinfo=UTC), window_days=60)
    assert actors["insider_unique_buyers_60d"] == 1
    assert actors["insider_unique_sellers_60d"] == 0

    # Now add Bob and Carol as buyers
    _insert_tx(
        warehouse,
        ticker="X",
        acc="b1",
        filer="Bob",
        code="P",
        tx_date=date(2025, 5, 10),
        shares=2_000,
        price=100.0,
    )
    _insert_tx(
        warehouse,
        ticker="X",
        acc="c1",
        filer="Carol",
        code="P",
        tx_date=date(2025, 5, 15),
        shares=3_000,
        price=100.0,
    )

    actors = insider_unique_actors(warehouse, "X", datetime(2025, 6, 1, tzinfo=UTC), window_days=60)
    assert actors["insider_unique_buyers_60d"] == 3  # Cluster signal


def test_build_insider_features_has_all_keys(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    features = build_insider_features(warehouse, "ANY", datetime(2025, 6, 1, tzinfo=UTC))
    expected = {
        "insider_buy_value_30d",
        "insider_sell_value_30d",
        "insider_net_buying_usd_30d",
        "insider_buys_count_30d",
        "insider_sells_count_30d",
        "insider_unique_buyers_60d",
        "insider_unique_sellers_60d",
    }
    assert set(features.keys()) == expected


def test_build_insider_features_zero_when_no_data(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    f = build_insider_features(warehouse, "GHOST", datetime(2025, 6, 1, tzinfo=UTC))
    assert f["insider_buy_value_30d"] == 0.0
    assert f["insider_net_buying_usd_30d"] == 0.0
    assert f["insider_unique_buyers_60d"] == 0


def test_other_codes_excluded(warehouse: duckdb.DuckDBPyConnection) -> None:
    """A (grant), M (option exercise), F (tax withhold), G (gift) are excluded."""
    for code in ("A", "M", "F", "G", "D"):
        _insert_tx(
            warehouse,
            ticker="X",
            acc=f"noise_{code}",
            filer="X",
            code=code,
            tx_date=date(2025, 5, 10),
            shares=10_000,
            price=100.0,
        )
    # One real buy
    _insert_tx(
        warehouse,
        ticker="X",
        acc="real",
        filer="X",
        code="P",
        tx_date=date(2025, 5, 10),
        shares=1_000,
        price=100.0,
    )

    stats = insider_window_stats(warehouse, "X", datetime(2025, 6, 1, tzinfo=UTC), window_days=30)
    # Only the P contributes
    assert stats["insider_buy_value_30d"] == 100_000.0
    assert stats["insider_buys_count_30d"] == 1
    assert stats["insider_sells_count_30d"] == 0
