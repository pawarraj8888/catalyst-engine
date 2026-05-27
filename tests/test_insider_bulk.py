"""Tests for the SEC bulk insider transactions ingest module."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import pytest

from catalyst_engine.data.insider_bulk import (
    InsiderTransactionRow,
    _bulk_url,
    parse_quarter_zip,
    quarters_in_range,
    upsert_insider_transactions,
)

# ---------------------------------------------------------------------------
# URL + quarter math
# ---------------------------------------------------------------------------


def test_bulk_url_pattern() -> None:
    assert _bulk_url(2024, 1).endswith("/2024q1_form345.zip")
    assert _bulk_url(2026, 4).endswith("/2026q4_form345.zip")


def test_bulk_url_rejects_invalid_quarter() -> None:
    with pytest.raises(ValueError):
        _bulk_url(2024, 5)
    with pytest.raises(ValueError):
        _bulk_url(2024, 0)


def test_quarters_in_range_within_year() -> None:
    assert quarters_in_range(2024, 2, 2024, 4) == [(2024, 2), (2024, 3), (2024, 4)]


def test_quarters_in_range_crosses_years() -> None:
    qs = quarters_in_range(2023, 3, 2024, 2)
    assert qs == [(2023, 3), (2023, 4), (2024, 1), (2024, 2)]


def test_quarters_in_range_single_quarter() -> None:
    assert quarters_in_range(2024, 1, 2024, 1) == [(2024, 1)]


# ---------------------------------------------------------------------------
# parse_quarter_zip — synthetic ZIP fixture
# ---------------------------------------------------------------------------


def _make_synthetic_zip(tmp_path: Path) -> Path:
    """Build a tiny in-memory ZIP that mimics SEC's bulk structure."""
    zip_path = tmp_path / "test_form345.zip"

    submissions_rows = [
        # AAPL Form 4 (in universe)
        {
            "ACCESSION_NUMBER": "0000000001",
            "DOCUMENT_TYPE": "4",
            "ISSUERTRADINGSYMBOL": "AAPL",
            "FILING_DATE": "10-May-2024",
            "PERIOD_OF_REPORT": "08-May-2024",
        },
        # GOOG Form 4 (not in test universe — should be filtered)
        {
            "ACCESSION_NUMBER": "0000000002",
            "DOCUMENT_TYPE": "4",
            "ISSUERTRADINGSYMBOL": "GOOG",
            "FILING_DATE": "11-May-2024",
            "PERIOD_OF_REPORT": "10-May-2024",
        },
        # AAPL Form 3 (initial holdings — wrong form, should be filtered)
        {
            "ACCESSION_NUMBER": "0000000003",
            "DOCUMENT_TYPE": "3",
            "ISSUERTRADINGSYMBOL": "AAPL",
            "FILING_DATE": "12-May-2024",
            "PERIOD_OF_REPORT": "10-May-2024",
        },
    ]
    reporter_rows = [
        {"ACCESSION_NUMBER": "0000000001", "RPTOWNERNAME": "Alice", "RPTOWNER_TITLE": "CEO"},
        {"ACCESSION_NUMBER": "0000000002", "RPTOWNERNAME": "Bob", "RPTOWNER_TITLE": "CFO"},
    ]
    trans_rows = [
        # AAPL: Alice buys 10k @ 150
        {
            "ACCESSION_NUMBER": "0000000001",
            "TRANS_DATE": "08-May-2024",
            "TRANS_CODE": "P",
            "TRANS_SHARES": "10000",
            "TRANS_PRICEPERSHARE": "150.00",
        },
        # GOOG (filtered) — sanity, shouldn't appear in output
        {
            "ACCESSION_NUMBER": "0000000002",
            "TRANS_DATE": "10-May-2024",
            "TRANS_CODE": "S",
            "TRANS_SHARES": "5000",
            "TRANS_PRICEPERSHARE": "2800.00",
        },
    ]
    _ = [
        # No footnotes for AAPL filing (no 10b5-1 indicator)
    ]

    def _tsv_bytes(rows: list[dict[str, str]]) -> bytes:
        if not rows:
            return b""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SUBMISSION.tsv", _tsv_bytes(submissions_rows))
        zf.writestr("REPORTINGOWNER.tsv", _tsv_bytes(reporter_rows))
        zf.writestr("NONDERIV_TRANS.tsv", _tsv_bytes(trans_rows))
        # Empty DERIV_TRANS and FOOTNOTES
        zf.writestr("DERIV_TRANS.tsv", b"ACCESSION_NUMBER\n")
        zf.writestr("FOOTNOTES.tsv", b"ACCESSION_NUMBER\tFOOTNOTE_TXT\n")
    return zip_path


def test_parse_quarter_zip_filters_to_universe(tmp_path: Path) -> None:
    zip_path = _make_synthetic_zip(tmp_path)
    rows = parse_quarter_zip(zip_path, universe_tickers={"AAPL"})
    # Only AAPL Form 4 transaction survives (GOOG out-of-universe, Form 3 wrong type)
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker == "AAPL"
    assert r.transaction_code == "P"
    assert r.shares == 10_000
    assert r.price == 150.0
    assert r.value_usd == 1_500_000.0
    assert r.filer_name == "Alice"
    assert r.filer_title == "CEO"
    assert r.is_10b5_1 is False


def test_parse_quarter_zip_detects_10b5_1(tmp_path: Path) -> None:
    """A footnote containing '10b5-1' marks the transaction as planned."""
    zip_path = tmp_path / "with_plan.zip"
    submissions = [
        {
            "ACCESSION_NUMBER": "plan1",
            "DOCUMENT_TYPE": "4",
            "ISSUERTRADINGSYMBOL": "AAPL",
            "FILING_DATE": "10-May-2024",
            "PERIOD_OF_REPORT": "08-May-2024",
        }
    ]
    reporters = [{"ACCESSION_NUMBER": "plan1", "RPTOWNERNAME": "Alice", "RPTOWNER_TITLE": "CEO"}]
    transactions = [
        {
            "ACCESSION_NUMBER": "plan1",
            "TRANS_DATE": "08-May-2024",
            "TRANS_CODE": "S",
            "TRANS_SHARES": "5000",
            "TRANS_PRICEPERSHARE": "150",
        }
    ]
    footnotes = [
        {
            "ACCESSION_NUMBER": "plan1",
            "FOOTNOTE_TXT": "Sale executed pursuant to Rule 10b5-1 trading plan dated Feb 2024.",
        }
    ]

    def _tsv(rows: list[dict[str, str]]) -> bytes:
        if not rows:
            return b""
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=rows[0].keys(), delimiter="\t")
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SUBMISSION.tsv", _tsv(submissions))
        zf.writestr("REPORTINGOWNER.tsv", _tsv(reporters))
        zf.writestr("NONDERIV_TRANS.tsv", _tsv(transactions))
        zf.writestr("DERIV_TRANS.tsv", b"ACCESSION_NUMBER\n")
        zf.writestr("FOOTNOTES.tsv", _tsv(footnotes))

    rows = parse_quarter_zip(zip_path, universe_tickers={"AAPL"})
    assert len(rows) == 1
    assert rows[0].is_10b5_1 is True


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------


def test_upsert_writes_and_dedupes(warehouse: duckdb.DuckDBPyConnection) -> None:
    rows = [
        InsiderTransactionRow(
            accession_number="a1",
            ticker="AAPL",
            filer_name="Alice",
            filer_title="CEO",
            transaction_date=date(2024, 5, 8),
            transaction_code="P",
            shares=10_000,
            price=150.0,
            value_usd=1_500_000.0,
            is_10b5_1=False,
        ),
    ]
    assert upsert_insider_transactions(warehouse, rows) == 1
    # Idempotent on re-run
    assert upsert_insider_transactions(warehouse, rows) == 0


def test_upsert_handles_empty(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert upsert_insider_transactions(warehouse, []) == 0
