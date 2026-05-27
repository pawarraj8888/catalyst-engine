-- Catalyst Engine warehouse schema (DuckDB)
-- Every table carries an `as_of` column. PIT discipline is enforced at the
-- query layer via src/catalyst_engine/utils/pit.py.

-- ------------------------------------------------------------------
-- universe — point-in-time membership in our coverage
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS universe (
    ticker         VARCHAR NOT NULL,
    cik            VARCHAR NOT NULL,
    company_name   VARCHAR,
    sector         VARCHAR NOT NULL,
    start_date     DATE NOT NULL,
    end_date       DATE,
    as_of          TIMESTAMP NOT NULL,
    source         VARCHAR NOT NULL,
    ingested_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, start_date)
);

-- ------------------------------------------------------------------
-- filings — all SEC filings (8-K, 10-Q, 10-K, 4, 13F-HR, etc.)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filings (
    accession_number  VARCHAR NOT NULL PRIMARY KEY,
    cik               VARCHAR NOT NULL,
    ticker            VARCHAR,
    filing_type       VARCHAR NOT NULL,
    filed_at          TIMESTAMP NOT NULL,
    period_of_report  DATE,
    items             VARCHAR[],
    raw_url           VARCHAR,
    primary_doc_url   VARCHAR,
    body_text         TEXT,
    as_of             TIMESTAMP NOT NULL,
    source            VARCHAR NOT NULL DEFAULT 'edgar',
    ingested_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker_filed_at ON filings (ticker, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_type_filed_at ON filings (filing_type, filed_at);

-- ------------------------------------------------------------------
-- earnings_events — past and upcoming earnings
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS earnings_events (
    ticker          VARCHAR NOT NULL,
    event_date      DATE NOT NULL,
    time_of_day     VARCHAR,                   -- BMO | AMC | DMH | UNK
    fiscal_period   VARCHAR,
    eps_est         DECIMAL(18, 4),
    eps_actual      DECIMAL(18, 4),
    revenue_est     DECIMAL(20, 2),
    revenue_actual  DECIMAL(20, 2),
    as_of           TIMESTAMP NOT NULL,
    source          VARCHAR NOT NULL,
    ingested_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, event_date, as_of)
);

-- ------------------------------------------------------------------
-- prices — daily OHLCV
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prices (
    ticker       VARCHAR NOT NULL,
    date         DATE NOT NULL,
    open         DECIMAL(18, 4),
    high         DECIMAL(18, 4),
    low          DECIMAL(18, 4),
    close        DECIMAL(18, 4),
    volume       BIGINT,
    adj_factor   DECIMAL(18, 8),
    as_of        TIMESTAMP NOT NULL,
    source       VARCHAR NOT NULL,
    ingested_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, date, as_of)
);

-- ------------------------------------------------------------------
-- options_snapshots — daily options chain snapshots
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS options_snapshots (
    ticker            VARCHAR NOT NULL,
    snapshot_time     TIMESTAMP NOT NULL,
    expiry            DATE NOT NULL,
    strike            DECIMAL(18, 4) NOT NULL,
    option_type       CHAR(1) NOT NULL,        -- C | P
    bid               DECIMAL(18, 4),
    ask               DECIMAL(18, 4),
    mid               DECIMAL(18, 4),
    last              DECIMAL(18, 4),
    iv                DECIMAL(10, 6),
    open_interest     BIGINT,
    volume            BIGINT,
    underlying_price  DECIMAL(18, 4),
    as_of             TIMESTAMP NOT NULL,
    source            VARCHAR NOT NULL,
    ingested_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, snapshot_time, expiry, strike, option_type)
);

CREATE INDEX IF NOT EXISTS idx_options_ticker_expiry ON options_snapshots (ticker, expiry);

-- ------------------------------------------------------------------
-- short_interest — FINRA bi-monthly
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS short_interest (
    ticker             VARCHAR NOT NULL,
    settlement_date    DATE NOT NULL,
    short_interest     BIGINT,
    avg_daily_volume   BIGINT,
    days_to_cover      DECIMAL(10, 4),
    shares_outstanding BIGINT,
    pct_float          DECIMAL(10, 6),
    as_of              TIMESTAMP NOT NULL,    -- publication date, NOT settlement
    source             VARCHAR NOT NULL DEFAULT 'finra',
    ingested_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, settlement_date)
);

-- ------------------------------------------------------------------
-- insider_transactions — Form 4
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insider_transactions (
    accession_number  VARCHAR NOT NULL,
    ticker            VARCHAR NOT NULL,
    filer_name        VARCHAR,
    filer_title       VARCHAR,
    transaction_date  DATE,
    transaction_code  CHAR(2),
    shares            BIGINT,
    price             DECIMAL(18, 4),
    value_usd         DECIMAL(20, 2),
    is_10b5_1         BOOLEAN,
    as_of             TIMESTAMP NOT NULL,
    source            VARCHAR NOT NULL DEFAULT 'edgar',
    ingested_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (accession_number, ticker, transaction_date, transaction_code, shares)
);

-- ------------------------------------------------------------------
-- holdings_13f — institutional positions
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS holdings_13f (
    cik_holder         VARCHAR NOT NULL,
    holder_name        VARCHAR,
    period_of_report   DATE NOT NULL,
    ticker             VARCHAR NOT NULL,
    cusip              VARCHAR,
    shares_held        BIGINT,
    value_usd          DECIMAL(20, 2),
    as_of              TIMESTAMP NOT NULL,
    source             VARCHAR NOT NULL DEFAULT 'edgar',
    ingested_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cik_holder, period_of_report, ticker)
);

-- ------------------------------------------------------------------
-- fda_events — PDUFA dates, Adcom meetings, drug catalysts
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fda_events (
    ticker        VARCHAR NOT NULL,
    event_date    DATE NOT NULL,
    event_type    VARCHAR NOT NULL,  -- pdufa | adcom | approval | crl | phase3_readout
    drug          VARCHAR,
    indication    VARCHAR,
    notes         TEXT,
    as_of         TIMESTAMP NOT NULL,
    source        VARCHAR NOT NULL,
    ingested_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, event_date, event_type, as_of)
);

-- ------------------------------------------------------------------
-- ingestion_runs — observability: what ran, when, what came back
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id        UUID NOT NULL PRIMARY KEY,
    source        VARCHAR NOT NULL,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    status        VARCHAR NOT NULL,            -- running | success | failed
    rows_written  BIGINT,
    error_message TEXT,
    metadata      JSON
);
