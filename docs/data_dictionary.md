# Data Dictionary

Every table in the warehouse, every column, every source. Updated as the schema
evolves.

## Universal columns

These appear on every table:

| Column | Type | Description |
|--------|------|-------------|
| `as_of` | TIMESTAMP | The earliest moment this row was observable to a real-time consumer. PIT discipline depends on this. |
| `source` | VARCHAR | Vendor or feed (e.g. `edgar`, `finnhub`, `yfinance`, `tradier`). |
| `ingested_at` | TIMESTAMP | When the row was written to our warehouse. `as_of <= ingested_at` always. |

---

## `universe`

The list of tickers we cover, with point-in-time membership.

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | VARCHAR | Equity ticker (current). |
| `cik` | VARCHAR | SEC Central Index Key, zero-padded to 10 digits. Primary join key for filings. |
| `company_name` | VARCHAR | Legal name. |
| `sector` | VARCHAR | One of: healthcare, consumer, tech, industrials. |
| `start_date` | DATE | First date in our coverage. |
| `end_date` | DATE | NULL if still covered; date of removal otherwise. |

**Sources:** Russell 1000 constituent list + SEC company tickers JSON.
**Refresh:** Quarterly (Russell rebalance) + ad-hoc on delistings.

---

## `filings`

All SEC filings ingested for the universe.

| Column | Type | Description |
|--------|------|-------------|
| `cik` | VARCHAR | Issuer CIK. |
| `ticker` | VARCHAR | Resolved at ingestion time. |
| `accession_number` | VARCHAR | EDGAR unique identifier, format `0001234567-25-000001`. Primary key. |
| `filing_type` | VARCHAR | `8-K`, `10-Q`, `10-K`, `4`, `13F-HR`, etc. |
| `filed_at` | TIMESTAMP | Filing acceptance time per EDGAR (Eastern time, UTC-normalized on ingest). |
| `period_of_report` | DATE | The reporting period this filing relates to. |
| `items` | VARCHAR[] | For 8-Ks: list of item codes (e.g. `["2.02", "9.01"]`). NULL otherwise. |
| `raw_url` | VARCHAR | EDGAR URL to the filing index page. |
| `primary_doc_url` | VARCHAR | URL to the primary HTML document. |
| `body_text` | TEXT | Extracted plain text. May be NULL if not yet parsed. |

**Source:** SEC EDGAR (free, no key required; User-Agent header required).
**Refresh:** Hourly during market hours; daily off-hours.
**PIT note:** `as_of = filed_at`. Filings cannot be backdated.

---

## `earnings_events`

Earnings releases past and future.

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | VARCHAR | |
| `event_date` | DATE | Report date in ET. |
| `time_of_day` | VARCHAR | `BMO` (before market open), `AMC` (after market close), `DMH` (during market hours, rare), or `UNK`. |
| `fiscal_period` | VARCHAR | E.g. `Q3 2025`. |
| `eps_est` | DECIMAL | Consensus estimate at `as_of`. |
| `eps_actual` | DECIMAL | NULL until reported. |
| `revenue_est` | DECIMAL | |
| `revenue_actual` | DECIMAL | |

**Source:** Finnhub earnings calendar (free tier).
**Refresh:** Daily.
**PIT note:** Estimates are revised over time. Each estimate revision is a new row with its own `as_of`.

---

## `prices`

Daily OHLCV.

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | VARCHAR | |
| `date` | DATE | Trading date. |
| `open` | DECIMAL | Split- and dividend-adjusted. |
| `high` | DECIMAL | |
| `low` | DECIMAL | |
| `close` | DECIMAL | |
| `volume` | BIGINT | |
| `adj_factor` | DECIMAL | Cumulative adjustment factor as of `as_of`. |

**Source:** yfinance (V1); Polygon ($30/mo) in V2.
**Refresh:** Daily after 16:30 ET.
**PIT note:** Adjustment factors change over time as corporate actions happen. Each refresh writes a new vintage; backtests must select the vintage current at the query date.

---

## `options_snapshots`

Daily snapshot of option chains, taken at 15:55 ET.

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | VARCHAR | |
| `snapshot_time` | TIMESTAMP | When we took the snapshot. |
| `expiry` | DATE | Option expiration date. |
| `strike` | DECIMAL | |
| `option_type` | CHAR(1) | `C` or `P`. |
| `bid` | DECIMAL | |
| `ask` | DECIMAL | |
| `mid` | DECIMAL | (bid + ask) / 2. |
| `last` | DECIMAL | Last trade price. |
| `iv` | DECIMAL | Implied vol from vendor (decimal, e.g. 0.32 = 32%). |
| `open_interest` | BIGINT | |
| `volume` | BIGINT | Today's volume. |
| `underlying_price` | DECIMAL | Spot at snapshot time. |

**Source:** Tradier (sandbox, free, ~15min delayed).
**Refresh:** Daily at 15:55 ET via cron.
**PIT note:** Snapshots are immutable. We do not have historical options data — we are *building* it day by day starting at project initiation.

---

## `short_interest`

Bi-monthly short interest from FINRA.

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | VARCHAR | |
| `settlement_date` | DATE | FINRA settlement date (mid-month and end-of-month). |
| `short_interest` | BIGINT | Shares short. |
| `avg_daily_volume` | BIGINT | |
| `days_to_cover` | DECIMAL | `short_interest / avg_daily_volume`. |
| `shares_outstanding` | BIGINT | At `as_of`. |
| `pct_float` | DECIMAL | `short_interest / float`. |

**Source:** FINRA bi-monthly CSV.
**Refresh:** Twice monthly, published ~8 business days after settlement.
**PIT note:** The publication lag means `as_of = publication_date`, not `settlement_date`. Positioning signal is inherently 1-3 weeks stale.

---

## `insider_transactions`

Form 4 filings (insider buys/sells).

| Column | Type | Description |
|--------|------|-------------|
| `accession_number` | VARCHAR | Form 4 accession. |
| `ticker` | VARCHAR | |
| `filer_name` | VARCHAR | |
| `filer_title` | VARCHAR | E.g. "Chief Executive Officer", "Director". |
| `transaction_date` | DATE | When the insider transacted. |
| `transaction_code` | CHAR(2) | Standard SEC codes: `P` = open-market purchase, `S` = open-market sale, `A` = grant, `M` = option exercise, etc. |
| `shares` | BIGINT | Signed: positive for buys, negative for sales. |
| `price` | DECIMAL | |
| `value_usd` | DECIMAL | `shares * price`. |
| `is_10b5_1` | BOOLEAN | Was this a planned 10b5-1 trade? Affects signal interpretation. |

**Source:** EDGAR Form 4.
**Refresh:** Real-time (Form 4 must be filed within 2 business days).
**PIT note:** `as_of = filed_at`. `transaction_date` is when the trade happened; that's not when the market knew.

---

## `holdings_13f`

Institutional holdings from 13F-HR filings.

| Column | Type | Description |
|--------|------|-------------|
| `cik_holder` | VARCHAR | Filer CIK (the institution). |
| `holder_name` | VARCHAR | |
| `period_of_report` | DATE | End of reported quarter. |
| `ticker` | VARCHAR | Held security. |
| `cusip` | VARCHAR | |
| `shares_held` | BIGINT | |
| `value_usd` | DECIMAL | |

**Source:** EDGAR 13F-HR.
**Refresh:** Quarterly, 45 days after quarter-end.
**PIT note:** Inherent 45-day lag. Holdings as of Mar 31 are not public until ~May 15.

---

## `fda_events`

PDUFA dates, Adcom meetings, and material FDA actions for healthcare names.

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | VARCHAR | |
| `event_date` | DATE | Expected or scheduled event date. |
| `event_type` | VARCHAR | `pdufa`, `adcom`, `approval`, `crl`, `phase3_readout`. |
| `drug` | VARCHAR | Drug name. |
| `indication` | VARCHAR | Disease/condition. |
| `notes` | TEXT | |

**Source:** Manual + scraped from FDA.gov + parsed from 8-Ks.
**Refresh:** Weekly review.
**PIT note:** Date drift is common; FDA frequently extends PDUFA dates. Each revision is a new row.
