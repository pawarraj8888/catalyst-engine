"""Tests for universe loading and CIK resolution."""

from __future__ import annotations

from pathlib import Path

from catalyst_engine.data.universe import Universe, UniverseEntry, load_universe, resolve_ciks


def test_load_universe_real_yaml() -> None:
    """The committed universe.yaml loads cleanly and has the expected shape."""
    u = load_universe()
    assert len(u.entries) == 250
    assert len(u.by_sector("healthcare")) == 60
    assert len(u.by_sector("consumer")) == 61
    assert len(u.by_sector("tech")) == 79
    assert len(u.by_sector("industrials")) == 50

    # Spot-check well-known names
    tickers = set(u.tickers)
    assert {"AAPL", "MSFT", "JNJ", "LLY", "AMZN", "CAT"} <= tickers


def test_universe_yaml_does_not_misparse_bool_tickers(tmp_path: Path) -> None:
    """YAML 1.1 turns bare ON/OFF/YES/NO/Y/N into booleans; we must override that.

    Regression test for the ON (ON Semiconductor) case.
    """
    yaml_text = """
version: 1
healthcare: []
consumer:
  - F
tech:
  - ON
  - "Y"
  - NO
  - YES
industrials: []
"""
    yaml_path = tmp_path / "universe.yaml"
    yaml_path.write_text(yaml_text)
    u = load_universe(yaml_path)
    tickers = u.tickers
    assert "F" in tickers
    assert "ON" in tickers
    assert "Y" in tickers
    assert "NO" in tickers
    assert "YES" in tickers
    # And none of them silently became "True" / "False" strings
    assert not any(t.lower() in {"true", "false"} for t in tickers)


def test_resolve_ciks_attaches_cik_and_company_name() -> None:
    universe = Universe(entries=[UniverseEntry(ticker="AAPL", sector="tech")])
    ticker_map = {
        "AAPL": {"cik": "0000320193", "company_name": "Apple Inc."},
    }
    resolved = resolve_ciks(universe, ticker_map=ticker_map)
    assert resolved.entries[0].cik == "0000320193"
    assert resolved.entries[0].company_name == "Apple Inc."


def test_resolve_ciks_keeps_unresolved_without_dropping() -> None:
    """Unresolved tickers stay in the universe so we can surface the gap."""
    universe = Universe(
        entries=[
            UniverseEntry(ticker="AAPL", sector="tech"),
            UniverseEntry(ticker="XXNOTREAL", sector="tech"),
        ]
    )
    ticker_map = {"AAPL": {"cik": "0000320193", "company_name": "Apple Inc."}}
    resolved = resolve_ciks(universe, ticker_map=ticker_map)
    assert len(resolved.entries) == 2
    assert resolved.entries[0].cik == "0000320193"
    assert resolved.entries[1].cik is None
