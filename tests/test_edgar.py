"""Tests for EDGAR ingestion.

Network calls are not made here — we use fixture JSON shaped like a real
EDGAR submissions response. The integration test that hits SEC live is
marked @pytest.mark.integration and is skipped by default.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from catalyst_engine.data.edgar import (
    FilingRecord,
    parse_submissions_to_records,
    upsert_filings,
)


# Synthetic but realistically shaped submissions payload
SUBMISSIONS_FIXTURE = {
    "cik": "320193",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-25-000100",
                "0000320193-25-000099",
                "0000320193-24-000050",
            ],
            "form": ["8-K", "10-Q", "8-K"],
            "filingDate": ["2025-08-01", "2025-07-15", "2024-11-01"],
            "acceptanceDateTime": [
                "2025-08-01T16:31:00.000Z",
                "2025-07-15T16:00:00.000Z",
                "2024-11-01T16:30:00.000Z",
            ],
            "primaryDocument": ["aapl-20250801.htm", "aapl-10q.htm", "aapl-20241101.htm"],
            "reportDate": ["2025-08-01", "2025-06-29", "2024-11-01"],
            "items": ["2.02,9.01", "", "5.02,9.01"],
        }
    },
}


def test_parse_submissions_basic() -> None:
    records = parse_submissions_to_records(
        SUBMISSIONS_FIXTURE,
        ticker="AAPL",
        cik="0000320193",
    )
    assert len(records) == 3
    r0 = records[0]
    assert r0.accession_number == "0000320193-25-000100"
    assert r0.filing_type == "8-K"
    assert r0.items == ["2.02", "9.01"]
    assert r0.ticker == "AAPL"
    assert r0.cik == "0000320193"
    assert r0.filed_at == datetime(2025, 8, 1, 16, 31, 0, tzinfo=timezone.utc)
    assert r0.primary_doc_url and r0.primary_doc_url.endswith("aapl-20250801.htm")
    assert "320193" in r0.primary_doc_url  # cik integer in path


def test_parse_submissions_filters_by_form() -> None:
    records = parse_submissions_to_records(
        SUBMISSIONS_FIXTURE,
        ticker="AAPL",
        cik="0000320193",
        filing_types={"8-K"},
    )
    assert len(records) == 2
    assert all(r.filing_type == "8-K" for r in records)


def test_parse_submissions_filters_by_since() -> None:
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    records = parse_submissions_to_records(
        SUBMISSIONS_FIXTURE,
        ticker="AAPL",
        cik="0000320193",
        since=since,
    )
    assert len(records) == 2  # the 2024 filing is excluded


def test_parse_submissions_handles_empty_items() -> None:
    records = parse_submissions_to_records(
        SUBMISSIONS_FIXTURE,
        ticker="AAPL",
        cik="0000320193",
        filing_types={"10-Q"},
    )
    assert records[0].items == []  # 10-Q has no items field


def test_parse_submissions_empty_payload() -> None:
    """Companies with no recent filings yield zero records, not an error."""
    empty_payload = {"filings": {"recent": {}}}
    assert parse_submissions_to_records(empty_payload, ticker="X", cik="0000000001") == []


def test_upsert_filings_idempotent(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Calling upsert twice with the same records writes only once."""
    records = parse_submissions_to_records(
        SUBMISSIONS_FIXTURE, ticker="AAPL", cik="0000320193"
    )

    n1 = upsert_filings(warehouse, records)
    assert n1 == 3

    # Same records again — should not double-insert
    n2 = upsert_filings(warehouse, records)
    assert n2 == 0

    count = warehouse.execute("SELECT COUNT(*) FROM filings").fetchone()
    assert count is not None
    assert count[0] == 3


def test_upsert_filings_empty_list_is_noop(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_filings(warehouse, []) == 0


def test_upsert_filings_sets_as_of_to_filed_at(warehouse: duckdb.DuckDBPyConnection) -> None:
    """PIT contract: as_of must equal filed_at for filings."""
    records = parse_submissions_to_records(
        SUBMISSIONS_FIXTURE, ticker="AAPL", cik="0000320193"
    )
    upsert_filings(warehouse, records)

    rows = warehouse.execute(
        "SELECT accession_number, filed_at, as_of FROM filings ORDER BY filed_at DESC"
    ).fetchall()
    for _acc, filed_at, as_of in rows:
        assert filed_at == as_of, f"as_of {as_of} != filed_at {filed_at}"


@pytest.mark.integration
def test_live_sec_fetch() -> None:
    """Live call to SEC. Skipped unless integration tests are enabled.

    Run with: pytest -m integration
    """
    from catalyst_engine.data.edgar import RateLimiter, _build_client, fetch_submissions

    with _build_client() as client:
        result = fetch_submissions("0000320193", client, RateLimiter())  # Apple
        assert "filings" in result
        assert "recent" in result["filings"]
