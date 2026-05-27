# Catalyst Engine

> A single-name catalyst tracker that logs its own calls publicly. The track record builds over time, in the open.

Most "trading model" repos show you a backtest. This one shows you what it actually called and what happened. Every weekday morning a GitHub Action scans the next 14 days of earnings events across ~250 large-caps, scores each one, and appends rows to [`live_log/calls.csv`](./live_log/calls.csv). Every evening it resolves yesterday's calls and writes post-mortems for the misses.

The backtest sits ~50% on free large-cap data. That's a feature, not a bug -- it's the efficient-markets baseline. **The value of the artifact is the public, time-stamped track record that accumulates from here.** Show it to a PM in three months and they can see whether the calls played out, whether the post-mortems are honest, whether the discipline holds.

---

## Live calls

| Metric                       | Value                            |
|:-----------------------------|:---------------------------------|
| Calls logged                 | _(auto-updated by CI)_           |
| Resolved                     | _(auto-updated by CI)_           |
| Hits / Misses                | _(auto-updated by CI)_           |
| Hit rate                     | _(needs N >= 30 to be trusted)_  |
| Last scan                    | _(auto-updated by CI)_           |

- Full log: [`live_log/calls.csv`](./live_log/calls.csv)
- Miss post-mortems: [`live_log/postmortems/`](./live_log/postmortems/)

Scoring weights stay frozen until N >= 30 resolved calls. That's the discipline line: no tuning weights mid-flight to fit the live results.

```
catalyst live scan       # Score upcoming events, log new calls
catalyst live resolve    # Fill in realized outcomes for past calls
catalyst live status     # Print current track record
```

A GitHub Actions workflow runs these on cron Mon-Fri at 09:00 ET (scan) and 17:00 ET (resolve), committing changes back to `live_log/`.

---

## What's in the warehouse

| Source                   | Rows     | Coverage                                  |
|:-------------------------|---------:|:------------------------------------------|
| Universe (PIT)           | 241      | 4 sectors, ~250 large-cap names           |
| SEC filings              | 80,000+  | 5 years (8-K, 10-Q, 10-K, Form 4, 13F-HR) |
| Earnings events          | 3,148    | 16Q depth, SEC-sourced dates              |
| OHLCV bars               | 300,193  | 5 years daily                             |
| Realized moves           | 3,148    | PIT-strict 8Q trailing baseline           |
| Insider transactions     | 214,047  | 5 years Form 3/4/5 from SEC bulk dataset  |
| 10b5-1 flags             | 92,974   | Detected via footnote text mining         |

178 tests, 83% coverage, CI green on every commit.

---

## Architecture

```
catalyst_engine/
|-- config/
|   |-- universe.yaml       # 250 tickers, 4 sectors
|   `-- scoring.yaml        # YAML-driven rule engine
|-- src/catalyst_engine/
|   |-- data/               # Ingestion: EDGAR, Finnhub, yfinance, FINRA, SEC bulk
|   |-- features/           # Realized moves, positioning, insider sentiment
|   |-- scoring/            # Sandboxed rule engine, deterministic
|   |-- backtest/           # Walk-forward replay with bootstrap CIs
|   |-- live/               # The track record loop (scan / resolve / status)
|   `-- cli.py              # `catalyst <verb>` command surface
|-- live_log/
|   |-- calls.csv           # Append-only, public, the artifact
|   `-- postmortems/        # One markdown per MISS or INVALIDATED
|-- .github/workflows/
|   |-- ci.yml              # Tests on every PR
|   `-- live.yml            # Daily scan + resolve cron
`-- tests/                  # 178 tests
```

---

## Methodology

**Point-in-time discipline.** Every table has an `as_of` column. Feature builders filter strictly to `event_date < current_event_date`. A dedicated test guards against future-leak.

**SEC-sourced announcement dates.** Vendor earnings calendars (Finnhub, yfinance) sometimes return fiscal-period-end dates instead of announcement dates. We detected this (every ticker "reporting" on Dec 31) and rebuilt `event_date` from 8-K item 2.02 filings -- the official channel for earnings releases.

**10b5-1 filtering.** 43% of insider transactions in our universe are 10b5-1 pre-planned trades carrying no current-moment information. We detect these via footnote text matching and exclude them from sentiment features.

**Inspectable scoring.** Every score traces back to which rules fired and what weight each contributed. `scored_setups.rules_fired` and `score_components` are queryable; the same fields are in every row of `live_log/calls.csv`.

**Bootstrap confidence intervals.** 1,000-resample binomial bootstrap per score bucket. No "62% hit rate" claims with N=20.

**Append-only public log.** Calls.csv is never edited in place. Schema is versioned (`schema_version` column). Misses get a markdown post-mortem committed alongside the row change.

---

## Backtest result (supporting context)

V0 + Phase 5 backtest, 5 years of history, 2,881 evaluable earnings events:

| Bucket (score) | N    | Hit rate | 95% CI       |
|---------------:|-----:|---------:|-------------:|
| 0-2            | 1,760 | 48.9%   | [46.6, 51.2] |
| 2-4            | 596   | 52.2%   | [48.2, 56.2] |
| 4-6            | 19    | 57.9%   | [36.8, 78.9] |

"Hit" = realized 1-day move > trailing 8-quarter median for that ticker.

Honest read: on this universe with free data, rule-based scoring lands at the efficient-markets baseline. The 4-6 bucket trends positive but N is too small to call. The point of the live log is to see what happens to these calls in reality, not to claim alpha from historical fit.

---

## Findings worth keeping

Empirical results from the build, written down so the next person doesn't have to re-derive them:

1. **Large-cap insider buys are rare.** 1,760 open-market buys vs 103,345 sales across 5 years and 250 names -- a 60-to-1 ratio. The Cohen-Malloy-Pomorski cluster-buy signal (3+ distinct insiders) fires only 3 times in the entire dataset. The literature edge lives in small-caps, not the S&P 500.

2. **Vol clustering signals are arbed out.** Rules that key on "name has been calm for 3 quarters" or "wild for 3 quarters" produce hit rates indistinguishable from coin flips.

3. **Net selling > $5M (excluding 10b5-1) shows weak signal.** N=29, hit rate 55.2%, CI [37, 73]. Below significance but directionally consistent with "insiders sell before bad news."

4. **An earlier insider-clustering rule hit 58% on N=19.** Deepening Form 4 history pushed N to 158; the rate collapsed to exactly 50.0%. This false positive is preserved in the commit history as evidence the discipline catches its own mistakes.

Future work: extend the universe down-cap (Russell 2000 or microcap) where the academic insider literature suggests signals are detectable.

---

## Quick start

```bash
# Install (uv-managed)
uv sync

# Configure
cp .env.example .env
# Set SEC_USER_AGENT="Your Name your.email@example.com"
# Set FINNHUB_API_KEY=...

# Initial data load (~10 minutes)
catalyst universe sync
catalyst ingest edgar --days 1825 --forms 4,8-K,10-Q,10-K,13F-HR
catalyst ingest earnings
catalyst ingest prices
catalyst ingest insider-bulk --start 2021Q1 --end 2026Q1

# Build features
catalyst features realized-moves

# Run the backtest
catalyst backtest replay

# Start logging live calls
catalyst live scan
catalyst live status
```

---

## Why this exists

A discretionary equity PM does the same prep every morning:

1. What's reporting in the next 2 weeks?
2. What's the historical move profile of each name?
3. Has positioning shifted into the print?
4. What does the option market imply?
5. Where's my edge?

Catalyst Engine automates steps 1-3 honestly and shows the data so a PM can answer step 5. It is not a black box. It is research infrastructure with the discipline a hedge fund would actually accept, plus a public track record that proves the discipline holds over time.

---

## License

MIT.
