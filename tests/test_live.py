"""Tests for the live calls loop."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from catalyst_engine.live import (
    CSV_COLUMNS,
    LiveCall,
    _classify_outcome,
    compute_status,
    read_calls,
    resolve_pending_calls,
    scan_and_log,
    write_calls,
)
from catalyst_engine.scoring.scorer import load_scoring_config

# ---------------------------------------------------------------------------
# Pin live_log/ to a temp dir for each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect live_log/ to a temp dir for each test."""
    monkeypatch.chdir(tmp_path)

    # The live module reads project_root from settings, which is computed
    # once at import time from __file__. Patch the helper directly.
    from catalyst_engine import live as live_mod

    def _fake_path() -> Path:
        return tmp_path / "live_log" / "calls.csv"

    monkeypatch.setattr(live_mod, "_calls_csv_path", _fake_path)

    # Also patch postmortems directory resolution by patching get_settings
    class _FakeSettings:
        @property
        def project_root(self) -> Path:
            return tmp_path

    from catalyst_engine import config as _config

    monkeypatch.setattr(_config, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(live_mod, "get_settings", lambda: _FakeSettings())

    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_call(
    ticker: str = "AAPL",
    event_date: date | None = None,
    score: float = 3.0,
    outcome: str = "PENDING",
    realized_move_pct: float | None = None,
    trailing_baseline_pct: float | None = 2.5,
) -> LiveCall:
    return LiveCall(
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        schema_version="1",
        ticker=ticker,
        catalyst_type="earnings",
        event_date=event_date or date(2026, 6, 1),
        score=score,
        rules_fired=["rule_a", "rule_b"],
        score_components={"rule_a": 1.5, "rule_b": 1.5},
        trailing_baseline_pct=trailing_baseline_pct,
        outcome=outcome,
        realized_move_pct=realized_move_pct,
    )


# ---------------------------------------------------------------------------
# CSV round-trip
# ---------------------------------------------------------------------------


def test_csv_round_trip_preserves_fields(_tmp_project_root: Path) -> None:
    original = _make_call(ticker="MSFT", score=4.7)
    write_calls([original])
    back = read_calls()
    assert len(back) == 1
    r = back[0]
    assert r.ticker == "MSFT"
    assert r.score == 4.7
    assert r.rules_fired == ["rule_a", "rule_b"]
    assert r.score_components == {"rule_a": 1.5, "rule_b": 1.5}
    assert r.outcome == "PENDING"


def test_csv_header_matches_contract(_tmp_project_root: Path) -> None:
    write_calls([_make_call()])
    text = (_tmp_project_root / "live_log" / "calls.csv").read_text()
    header = text.splitlines()[0].split(",")
    assert header == CSV_COLUMNS


def test_read_calls_handles_empty_file(_tmp_project_root: Path) -> None:
    # Should auto-create with header, return empty list
    assert read_calls() == []


def test_read_calls_skips_comment_lines(_tmp_project_root: Path) -> None:
    path = _tmp_project_root / "live_log" / "calls.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ",".join(CSV_COLUMNS) + "\n"
        "# this is a comment row\n"
        ",,,,,,,,,,,,,,\n"  # blank row with no event_date
    )
    assert read_calls() == []


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def test_classify_outcome_hit() -> None:
    # abs move 5% > baseline 2% -> HIT
    assert _classify_outcome(5.0, 2.0) == "HIT"


def test_classify_outcome_miss() -> None:
    assert _classify_outcome(1.0, 2.0) == "MISS"
    assert _classify_outcome(2.0, 2.0) == "MISS"  # strict >


def test_classify_outcome_invalidated_on_missing() -> None:
    assert _classify_outcome(None, 2.0) == "INVALIDATED"
    assert _classify_outcome(5.0, None) == "INVALIDATED"
    assert _classify_outcome(None, None) == "INVALIDATED"


# ---------------------------------------------------------------------------
# Scan idempotency
# ---------------------------------------------------------------------------


def test_scan_is_idempotent(warehouse: duckdb.DuckDBPyConnection, _tmp_project_root: Path) -> None:
    """Running scan twice on the same data must not duplicate calls."""
    # Insert one upcoming event
    warehouse.execute(
        """
        INSERT INTO earnings_events (ticker, event_date, fiscal_period, source, as_of)
        VALUES ('AAPL', ?, '2099Q4', 'test', ?)
        """,
        [date(2099, 12, 31), datetime(2099, 1, 1)],
    )
    # And enough prior realized_moves rows so trailing_median exists
    for i in range(3):
        warehouse.execute(
            """
            INSERT INTO realized_moves
            (ticker, event_date, abs_move_1d, move_ratio,
             trailing_median_8q, as_of)
            VALUES ('AAPL', ?, 0.03, 1.0, 0.025, ?)
            """,
            [
                date(2025, 1, i + 1),
                datetime(2025, 1, i + 1, 16, 0),
            ],
        )

    config = load_scoring_config()
    n1, _ = scan_and_log(warehouse, config=config, today=date(2099, 12, 1), horizon_days=60)
    n2, _ = scan_and_log(warehouse, config=config, today=date(2099, 12, 1), horizon_days=60)

    assert n1 == 1
    assert n2 == 0  # second run finds nothing new
    assert len(read_calls()) == 1


# ---------------------------------------------------------------------------
# Resolve classification + post-mortem
# ---------------------------------------------------------------------------


def _insert_ohlcv(conn: duckdb.DuckDBPyConnection, ticker: str, d: date, close: float) -> None:
    conn.execute(
        """
        INSERT INTO prices (ticker, date, open, high, low, close, volume, as_of, source)
        VALUES (?, ?, ?, ?, ?, ?, 1000000, ?, 'test')
        """,
        [ticker, d, close, close, close, close, datetime(d.year, d.month, d.day)],
    )


def test_resolve_classifies_hit_and_writes_no_postmortem(
    warehouse: duckdb.DuckDBPyConnection, _tmp_project_root: Path
) -> None:
    # Event 2025-06-02 BMO, pre-close 2025-06-01 at $100, post-close 2025-06-02 at $108
    # = +8% move, baseline 2% -> HIT
    # (Project semantic: post-close is the day-of-event, since that day's close
    # incorporates the print under our BMO/AMC approximation.)
    _insert_ohlcv(warehouse, "AAPL", date(2025, 6, 1), 100.0)
    _insert_ohlcv(warehouse, "AAPL", date(2025, 6, 2), 108.0)

    call = _make_call(
        ticker="AAPL",
        event_date=date(2025, 6, 2),
        score=5.0,
        outcome="PENDING",
        trailing_baseline_pct=2.0,
    )
    write_calls([call])

    stats = resolve_pending_calls(warehouse, today=date(2025, 6, 10))
    assert stats["n_resolved"] == 1
    assert stats["n_hit"] == 1
    assert stats["n_miss"] == 0

    resolved = read_calls()[0]
    assert resolved.outcome == "HIT"
    assert resolved.realized_move_pct is not None
    assert resolved.realized_move_pct == pytest.approx(8.0, abs=0.01)

    # No post-mortem file for HITs
    pm_dir = _tmp_project_root / "live_log" / "postmortems"
    assert not pm_dir.exists() or not any(pm_dir.iterdir())


def test_resolve_classifies_miss_and_writes_postmortem(
    warehouse: duckdb.DuckDBPyConnection, _tmp_project_root: Path
) -> None:
    _insert_ohlcv(warehouse, "MSFT", date(2025, 6, 1), 200.0)
    _insert_ohlcv(warehouse, "MSFT", date(2025, 6, 2), 202.0)  # +1%, baseline 3% -> MISS

    call = _make_call(
        ticker="MSFT",
        event_date=date(2025, 6, 2),
        score=5.0,
        outcome="PENDING",
        trailing_baseline_pct=3.0,
    )
    write_calls([call])

    stats = resolve_pending_calls(warehouse, today=date(2025, 6, 10))
    assert stats["n_miss"] == 1
    assert stats["n_hit"] == 0

    # Post-mortem written
    pm_path = _tmp_project_root / "live_log" / "postmortems" / "2025-06-02-MSFT.md"
    assert pm_path.exists()
    content = pm_path.read_text()
    assert "MSFT" in content
    assert "MISS" in content


def test_resolve_leaves_future_calls_pending(
    warehouse: duckdb.DuckDBPyConnection, _tmp_project_root: Path
) -> None:
    call = _make_call(event_date=date(2099, 1, 1))  # far future
    write_calls([call])

    stats = resolve_pending_calls(warehouse, today=date(2025, 6, 10))
    assert stats["n_resolved"] == 0
    assert stats["n_pending_remaining"] == 1


def test_resolve_handles_invalidated_when_no_prices(
    warehouse: duckdb.DuckDBPyConnection, _tmp_project_root: Path
) -> None:
    """No OHLCV rows -> INVALIDATED, post-mortem still written."""
    call = _make_call(ticker="ZZZ", event_date=date(2025, 6, 2), trailing_baseline_pct=2.0)
    write_calls([call])
    stats = resolve_pending_calls(warehouse, today=date(2025, 6, 10))
    assert stats["n_invalidated"] == 1


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_compute_status_summarizes(_tmp_project_root: Path) -> None:
    calls = [
        _make_call(ticker="A", outcome="HIT", realized_move_pct=5.0, score=3.0),
        _make_call(ticker="B", outcome="HIT", realized_move_pct=4.0, score=3.5),
        _make_call(ticker="C", outcome="MISS", realized_move_pct=1.0, score=2.5),
        _make_call(ticker="D", outcome="PENDING", score=4.0),
        _make_call(ticker="E", outcome="INVALIDATED", score=1.0),
    ]
    write_calls(calls)

    s = compute_status()
    assert s.n_total == 5
    assert s.n_pending == 1
    assert s.n_hits == 2
    assert s.n_misses == 1
    assert s.n_invalidated == 1
    assert s.n_resolved == 3
    assert s.hit_rate_pct == pytest.approx(2.0 / 3.0 * 100, abs=0.1)


def test_compute_status_empty_log(_tmp_project_root: Path) -> None:
    s = compute_status()
    assert s.n_total == 0
    assert s.hit_rate_pct is None
