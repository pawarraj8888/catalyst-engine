# Catalyst Engine

> A single-name event and catalyst tracking system for discretionary equity desks.

Tracks ~250 US large-caps across Healthcare, Consumer, Tech, and Industrials. Ingests SEC filings, earnings calendars, prices, short interest, and insider transactions into a point-in-time research warehouse. Scores upcoming catalysts (earnings, material 8-Ks, FDA events, guidance revisions) and runs honest backtests with bootstrap confidence intervals.

Built to be read by hedge-fund analysts. PIT discipline is enforced mechanically, not by convention. Every signal is inspectable down to which rules fired.

---

## Headline result

V0 + Phase 5 backtest, 5 years of history, 2,881 evaluable earnings events:

| Bucket (score) | N    | Hit rate | 95% CI       |
|---------------:|-----:|---------:|-------------:|
| 0-2            | 1,760 | 48.9%   | [46.6, 51.2] |
| 2-4            | 596   | 52.2%   | [48.2, 56.2] |
| 4-6            | 19    | 57.9%   | [36.8, 78.9] |

"Hit" = realized 1-day move > trailing 8-quarter median for that ticker.

**The honest read:** on a 250-name large-cap universe, free-data rule-based scoring is approximately at the efficient-markets baseline. The 4-6 bucket trends positive but N is too small to call a result. **This is what the data says, and pretending otherwise would not pass a hedge-fund interview.**

What the project does prove out:
- The infrastructure works (164 tests, 83% coverage, CI passing on every commit)
- The methodology is sound (SEC-sourced announcement dates, strict PIT, bootstrap CIs)
- The discipline catches false signals. Phase 4's `insider_clustering` rule hit 58% on N=19; with proper N=158 it collapsed to exactly 50.0%. That correction is in the commit history.

---

## What's in the warehouse

| Source                  | Rows   | Coverage          |
|:------------------------|-------:|:------------------|
| Universe (PIT)          | 241    | 4 sectors, ~250 names |
| SEC filings (8-K, 10-Q, 10-K, Form 4, 13F-HR) | 80,000+ | 5 years           |
| Earnings events         | 3,148  | 16Q depth, SEC-sourced dates |
| OHLCV bars              | 300,193 | 5 years daily      |
| Realized moves          | 3,148  | PIT-strict 8Q trailing baseline |
| Insider transactions    | 214,047 | 5 years Form 3/4/5 from SEC bulk dataset |
| 10b5-1 flags            | 92,974 | Detected via footnote text mining |

---

## Architecture

```
catalyst_engine/
|-- config/
|   |-- universe.yaml       # 250 tickers, 4 sectors
|   `-- scoring.yaml        # YAML-driven rule engine
|-- src/catalyst_engine/
|   |-- data/               # Ingestion pipelines
|   |   |-- universe.py     # Custom YAML loader (handles ON/OFF/F tickers)
|   |   |-- edgar.py        # SEC EDGAR with rate limiting + retries
|   |   |-- earnings.py     # Finnhub calendar
|   |   |-- earnings_from_edgar.py  # Announcement dates from 8-K item 2.02
|   |   |-- earnings_quality.py     # Audit + clean fiscal-period-end fakes
|   |   |-- prices.py       # yfinance OHLCV
|   |   |-- short_interest.py       # FINRA bulk SI snapshots
|   |   `-- insider_bulk.py # SEC quarterly Form 3/4/5 bulk dataset
|   |-- features/
|   |   |-- realized_moves.py  # PIT-strict trailing median, move_ratio
|   |   |-- positioning.py     # SI z-score, Form 4 / 13F counts
|   |   `-- insider.py         # Net buying $, cluster signals, 10b5-1 filtered
|   |-- scoring/
|   |   `-- scorer.py       # Sandboxed rule engine, deterministic, inspectable
|   |-- backtest/
|   |   |-- replay.py       # Walk every event, build PIT features, score
|   |   `-- metrics.py      # Hit rate by bucket, bootstrap CIs
|   |-- utils/
|   |   `-- pit.py          # Point-in-time contract enforced by tests
|   |-- db/
|   |   |-- schema.sql      # DuckDB warehouse schema
|   |   `-- connection.py
|   `-- cli.py              # `catalyst <verb>` command surface
|-- tests/                  # 164 tests, 83% coverage
`-- docs/
    |-- methodology.md
    |-- decisions_log.md
    `-- data_dictionary.md
```

---

## Status

| Phase | Component                                              | State |
|:-----:|:-------------------------------------------------------|:-----:|
| 0     | Repo scaffolding, CI, pre-commit, PIT contract         | done  |
| 1     | EDGAR ingestion (8-K, 10-Q, 10-K, Form 4, 13F-HR)      | done  |
| 1     | Earnings calendar (Finnhub)                            | done  |
| 1     | Prices (yfinance, 5y)                                  | done  |
| 1     | Earnings dates rebuilt from SEC 8-K item 2.02          | done  |
| 2     | Realized moves with trailing baselines                 | done  |
| 2     | Positioning features (SI z-score, Form 4 counts)       | done  |
| 2     | Insider sentiment (parsed Form 4, value-weighted, 10b5-1 filtered) | done |
| 2     | Scoring engine (YAML-driven, sandboxed)                | done  |
| 3     | Historical replay backtest with bootstrap CIs          | done  |
| 4     | One-pager generator + Streamlit dashboard              | next  |
| 4     | Intraday 8-K alerter                                   | next  |
| 5     | Live calls log (>= 30 calls before applying weights)   | next  |

---

## Quick start

```bash
# Install (uv-managed)
uv sync

# Configure
cp .env.example .env
# Set SEC_USER_AGENT="Your Name your.email@example.com"
# Set FINNHUB_API_KEY=...

# Initial data load (takes ~10 minutes)
catalyst universe sync
catalyst ingest edgar --days 1825 --forms 4,8-K,10-Q,10-K,13F-HR
catalyst ingest earnings
catalyst ingest prices
catalyst ingest insider-bulk --start 2021Q1 --end 2026Q1
catalyst ingest short-interest

# Build features
catalyst features realized-moves

# Run the backtest
catalyst backtest replay
```

---

## Methodology highlights

**Point-in-time discipline.** Every table has an `as_of` column. Feature builders filter strictly to `event_date < current_event_date`. A dedicated test (`test_trailing_median_excludes_self_and_future`) guards against future-leak. See `docs/methodology.md`.

**SEC-sourced announcement dates.** Vendor earnings calendars (Finnhub, yfinance) sometimes return fiscal-period-end dates instead of announcement dates for recent quarters. We detected this in Phase 3 (every ticker "reporting" on Dec 31 / Mar 31) and rebuilt event_date from 8-K item 2.02 filings -- the official channel for earnings releases.

**10b5-1 filtering.** 43% of insider transactions in our universe are 10b5-1 pre-planned trades carrying no current-moment information. We detect these via footnote text matching and exclude them from sentiment features.

**Inspectable scoring.** Every score traces back to which rules fired and what weight each contributed. `scored_setups.rules_fired` and `score_components` are queryable.

**Bootstrap confidence intervals.** 1,000-resample binomial bootstrap per bucket. No "62% hit rate" claims with N=20.

---

## Findings worth keeping in mind

These are written down so they are not repeated by the next person who builds something like this:

1. **Large-cap insider buys are rare.** Across 5 years and 250 names, we have 1,760 open-market buys vs 103,345 sales -- a 60-to-1 ratio. The Cohen-Malloy-Pomorski cluster-buy signal (3+ distinct insiders buying in 60d) fires only 3 times in our entire dataset. **The literature edge lives in small-caps, not the S&P 500.**

2. **Vol clustering signals are arbed out.** Rules that ask "has this name been calm for 3 quarters" or "has this name been wild for 3 quarters" produce hit rates indistinguishable from coin flips. Decades of systematic equity trading have priced this in.

3. **Net selling > $5M (excluding 10b5-1) shows weak signal.** N=29, hit rate 55.2%, CI [37%, 73%]. Below significance but directionally consistent with "insiders sell before bad news."

4. **The 90% hit rate from Phase 4 (N=10) was sample noise.** Deepening Form 4 history pushed N up; the rate collapsed to ~50%. This is in the commit history as proof that the discipline catches false positives.

Future work: extend the universe to Russell 2000 or microcap names where the academic literature suggests insider signals are detectable.

---

## Why this exists

A discretionary equity PM does the same prep work every morning:
1. What is reporting in the next 2 weeks?
2. What is the historical move profile of each name?
3. Has positioning shifted into the print?
4. What does the option market imply?
5. Where is my edge?

Catalyst Engine automates steps 1-3 honestly and shows you the data so you can answer step 5 yourself. It is not a black-box trading system. It is research infrastructure with the discipline a hedge fund would actually accept.

Live calls log will track real-time scored setups and post-mortem misses. Track record will be public.

---

## License

MIT.
