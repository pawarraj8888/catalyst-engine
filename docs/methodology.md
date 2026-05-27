# Catalyst Engine — Methodology

> Last updated: 2026-05-27
> Author: Raj Pawar

## What this is

A single-name event and catalyst tracking system designed to compress the daily
prep work of a junior analyst at a discretionary equity long/short fund. It
ingests SEC filings, options markets, earnings calendars, and positioning data
across a defined universe, scores each upcoming catalyst for setup quality, and
generates a one-page thesis for high-conviction setups.

Live calls are logged publicly with timestamps. Misses are post-mortemed.

## Who this is for

Discretionary equity L/S funds (sub-$2B AUM), event-driven shops, sector pods
at multi-strats. Specifically: PMs running 30-80 names who cannot personally
track every catalyst, and junior analysts whose workflow this directly
augments.

## Scope (V1)

### Universe
- ~250 US-listed equities from Russell 1000
- Sectors: Healthcare (60), Consumer (60), Tech (80), Industrials (50)
- Universe is point-in-time: includes delisted names sourced via CRSP
  historical constituents

### Catalyst types
1. **Earnings releases** — quarterly, calendar-driven
2. **Material 8-K filings** — event-driven, intraday
3. **FDA / regulatory events** — PDUFA dates, Adcom meetings (healthcare names)
4. **Guidance revisions** — extracted from 8-K item 7.01 / 2.02

## Core hypothesis

Implied moves in single-name options frequently misprice realized event
outcomes when conditioned on (i) positioning (short interest, insider activity,
13F flows), (ii) skew structure, and (iii) the firm's own historical reaction
distribution. A rule-based scoring system, applied with strict point-in-time
discipline, can identify asymmetric setups at a hit rate materially above 50%.

This is not a price-prediction claim. It is a *setup-quality* claim: when the
score is high, the realized 1-5 day move is more likely to exceed the implied
move in the favored direction.

## Method

### Data layer
- All sources written to a DuckDB warehouse with strict `as_of` discipline
- Every row carries the timestamp it was first observable
- Point-in-time correctness is enforced by tests, not by convention

### Feature layer
For each upcoming catalyst:
- **Implied move** from ATM straddle on the bracketing expiry
- **Historical realized move** distribution from prior same-type events for the
  ticker and its sector peers
- **Skew** at 25Δ put vs 25Δ call, z-scored vs ticker's 90-day baseline
- **Short interest** as % float and days-to-cover, z-scored vs 1y history
- **Insider activity** net buying/selling over 90d window, clustered filers
- **13F deltas** quarter-over-quarter for top holders (where available)

### Scoring layer
Rule-based, configured in YAML, deterministic and inspectable. Each rule fires
on a feature condition and contributes a weighted score. Final score is 0-10,
accompanied by the explicit list of rules that fired.

ML calibration is deferred until live track record reaches N > 100 events.
With current sample sizes, ML on this dataset overfits.

### Output layer
- **Daily onepager** generated per high-conviction setup (score ≥ threshold)
- **Streamlit dashboard** with watchlist, scores, calls log, post-mortems
- **Intraday alerts** on material 8-Ks filed for watchlist names
- **Public calls log** in `live_log/calls.csv` with timestamp, thesis, outcome

## Evaluation

### Backtest
- Historical replay of all catalysts in universe, last 2 years
- Walk-forward by quarter
- Bootstrap confidence intervals on hit rate
- Calibration plot: score bucket vs realized hit rate
- Ablation: drop each feature, measure score quality degradation

### Live
- Every flagged setup posted publicly before the event
- Outcome and P&L logged after the event
- Misses post-mortemed within 7 days

### Success thresholds (pre-registered)
- Score ≥ 8 setups: hit rate > 55% with N > 30 over 6-month live period
- Implied vs realized correlation > 0.4
- Pipeline uptime > 98%
- Zero PIT test failures

## Out of scope (V1)

- Macro overlays, cross-asset signals
- News sentiment, transcript NLP
- Real-time alt data (web traffic, credit card panels)
- M&A arbitrage
- Index rebalancing trades
- Live broker execution

These may enter V2. Deliberately excluded from V1 to keep the artifact tight
and the pitch focused.

## Known limitations

1. **Sample size per catalyst type.** Earnings has N > 500 historical events
   in-universe; FDA events have N < 100. Score weights reflect this.
2. **Free options data is delayed 15min.** Implied moves computed at close are
   stale by minutes; flagged on every onepager.
3. **Form 4 filings have a 2-business-day delay** by SEC rule. Insider signals
   are inherently lagged.
4. **Short interest is bi-monthly.** Positioning signals can be stale by up to
   3 weeks.
5. **No alt data.** Setups driven by data not in our feed (e.g. drug pricing
   leaks, satellite-derived retail traffic) will be missed.

All of these are documented on every onepager. They are features of the system,
not bugs to hide.

## Why this can be trusted

The system is designed so that a skeptical PM can audit any specific call:
- Pull the date of the call
- Re-run scoring with `--as-of=<date>` flag
- See the exact feature values that were available at that moment
- See which rules fired and with what weights
- Compare against the realized outcome and any post-mortem

This is the standard. Anything less is a backtest fairy tale.
