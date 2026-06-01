# Tests To Run

Owner: Chad

Purpose: define how we should analyze the weather-market data once collection is running.

## Test 1 — Stale Observation Strategy

Hypothesis:

Fast airport observations can reveal that the market is stale before prices fully adjust.

Core idea:

If METAR shows the station-local daily high has already crossed or invalidated a temperature bucket, but Polymarket still prices that bucket as live, there may be a tradeable gap.

Data needed:

- Market question and bucket type: `exact`, `range`, `above_eq`, `below_eq`
- Bucket lower/upper temperatures and bucket unit: `C` or `F`
- Market Rules text and stated resolution source
- Station, city, timezone, and settlement date
- METAR observation time, fetched time, temperature, and station-local daily high
- Settlement-source reading, if available
- Final Polymarket/UMA resolution outcome
- Order book bid/ask/mid/size at signal time
- Time to close

Signal examples:

- Exact or range bucket is impossible:
  - Station-local high is already `28C`
  - `exact 25C`, `exact 26C`, `exact 27C`, or any range below the observed high still has YES value
  - Candidate action: buy NO on impossible bucket

- Above bucket is underpriced:
  - Station-local high is already `28C`
  - `above_eq 28C` or lower trades below near-certain value
  - Candidate action: buy YES, if spread and liquidity allow

- Below bucket is impossible:
  - Station-local high is already above the `below_eq` threshold
  - Candidate action: buy NO

Analysis after data comes in:

1. Reconstruct every moment when a METAR observation changed the station-local daily high.
2. For each changed high, find all active markets for that city/date.
3. Determine which buckets became logically resolved or materially more likely.
4. Pull the nearest order book snapshot after the observation became available to us.
5. Compute theoretical edge using bucket resolution logic.
6. Subtract spread and estimated slippage.
7. Track whether the trade would have settled correctly under:
   - METAR proxy
   - Rules-confirmed settlement source
   - Final Polymarket/UMA outcome

Metrics:

- Number of signals per city/day
- Average seconds/minutes between observation and market repricing
- Best executable price after signal
- Worst executable price after signal
- Fillable size at positive edge
- Gross edge before spread
- Net edge after spread
- Win rate by city
- Win rate by station/source
- False-positive rate caused by METAR vs settlement-source mismatch
- Average time from signal to close

Pass/fail criteria:

- Pass if stale-observation signals remain profitable after spread/slippage in paper trading.
- Fail if most apparent edge disappears once using actual executable bid/ask or final settlement source.
- Special watch item: any repeated mismatch between METAR proxy and Rules source should downgrade or disable that station.

## Test 2 — Market-Open Forecast Accuracy

Hypothesis:

At market open, some weather models may be closer to the eventual resolution value than the market-implied distribution.

Core idea:

For each city/date, compare each forecast model's predicted daily high at market open against the final settlement temperature. Then compare those forecasts to the market's opening implied distribution.

Data needed:

- `first_seen_utc` for each market
- Forecasts fetched before `first_seen_utc`
- Model name, model run time, fetched time, forecast date, predicted high
- Opening order book snapshots across all buckets in the market's neg-risk group
- Final settlement temperature
- Bucket resolution outcome

Forecasts to compare:

- TAF, where TX/TN is present
- GFS
- ICON
- GEM
- ARPEGE / Météo-France
- ECMWF direct, once implemented

Analysis after data comes in:

1. For each city/date, define market open as `first_seen_utc`.
2. For each model, select the most recent forecast with `fetched_utc <= first_seen_utc`.
3. Record each model's predicted high for the settlement local date.
4. Build the market-implied distribution from opening exact-bucket prices.
5. Estimate the market-implied expected high.
6. Compare each model and the market-implied expected high against final settlement.
7. Rank models by absolute error and directional usefulness.

Metrics:

- Mean absolute error by model
- Median absolute error by model
- Hit rate within 0C, 1C, 2C, and 3C of settlement
- Bias by model: consistently too hot or too cold
- Accuracy by city
- Accuracy by region/timezone
- Accuracy by forecast horizon
- Accuracy of market-implied expected high
- Model vs market error difference
- Frequency where best model beat the market-implied estimate

Trading interpretation:

If a model is consistently closer than market-open pricing, test a paper strategy:

- Buy YES on the bucket or range closest to the best forecast when underpriced.
- Buy NO on buckets/ranges far from model consensus when overpriced.
- Avoid trades unless model consensus edge exceeds spread/slippage.
- Prefer cities where historical model error is low and liquidity is usable.

Pass/fail criteria:

- Pass if one or more models beat the market-implied opening distribution after enough samples.
- Fail if model advantage is small relative to spread/slippage.
- Do not trade forecast-only signals until there is a meaningful sample by city.

## Test 3 — Forecast Consensus vs Market Distribution

Hypothesis:

The market may misprice days where independent models strongly agree.

Core idea:

When GFS, ICON, GEM, ARPEGE, and ECMWF cluster tightly around one temperature, compare that consensus to the opening market distribution.

Analysis:

1. At market open, compute model consensus high:
   - mean
   - median
   - min/max spread
2. Flag cases where model spread is small, for example <= `1.5C`.
3. Compare consensus to the market-implied expected high.
4. Track whether tight model consensus beats market pricing more often than loose consensus.

Metrics:

- Consensus error vs settlement
- Market-implied error vs settlement
- Model spread vs eventual error
- Profitability of consensus-aligned paper trades
- Results by city and forecast horizon

Pass/fail criteria:

- Pass if tight consensus reliably identifies mispriced buckets.
- Fail if the market already prices consensus correctly or if spread removes the edge.

## Test 4 — Best Forecast Timing By Station

Hypothesis:

Each station may have a repeatable window when forecast data is most accurate for that station's eventual daily high.

Core idea:

Do not assume the market-open forecast is always the most useful forecast. For each station, test which forecast age and horizon best predicts the final settlement temperature.

Examples of timing buckets:

- T-48h
- T-36h
- T-24h
- T-18h
- T-12h
- T-6h
- T-3h
- T-1h
- Last forecast before market close

Analysis:

1. For each station/date, collect all model forecasts available before settlement.
2. Convert every forecast to `hours_before_close`.
3. Group forecasts into timing buckets.
4. For each station/model/timing bucket, compare predicted high to final settlement temperature.
5. Look for stable patterns:
   - station where GFS is best at T-24h
   - station where ICON is best late day
   - station where forecast accuracy improves sharply after local sunrise
   - station where forecasts remain unreliable until close
6. Compare each station's best timing bucket to market prices at that same time.

Metrics:

- Mean absolute error by station/model/timing bucket
- Median absolute error by station/model/timing bucket
- Hit rate within 0C, 1C, 2C, and 3C
- Bias by station/model/timing bucket
- Best model per station
- Best timing bucket per station
- Whether best timing is stable week to week
- Whether accuracy improves enough to create a tradeable market edge

Trading interpretation:

If a station has a consistent best forecast window, use that as the preferred signal time for forecast-based trades.

Example:

- If `EGLC` is most accurate at T-12h using ICON/GFS consensus, then ignore weaker T-48h signals and only evaluate London trades around T-12h.
- If `KLGA` forecasts are already accurate at T-36h, market-open trades may be more viable.
- If `ZBAA` accuracy improves only near close, use it mainly for late stale-observation or convergence trades.
- Treat HKO/Hong Kong separately from `ZBAA`; its fast proxy and settlement source are different.

Pass/fail criteria:

- Pass if station-specific timing patterns repeat across multiple weeks.
- Fail if best timing buckets are random or the improvement is smaller than spread/slippage.
- Do not generalize one station's best timing to all stations.

## Test 5 — Market-Open Entry With Dynamic Rebalancing

Hypothesis:

The best strategy may be to enter at market open based on forecast mispricing, then buy, sell, hedge, or exit as better forecasts and live observations arrive.

Core idea:

Treat the trade as a managed position, not a one-shot bet. Market open may offer the best early mispricing, but the position should be updated as the information set changes.

Information updates to react to:

- New model runs
- TAF updates
- METAR observations changing the station-local daily high
- Settlement-source updates
- Order book repricing
- Time decay as market close approaches
- Model consensus tightening or breaking apart

Position actions to test:

- Enter at market open
- Add to a position when later data confirms the thesis
- Reduce when edge shrinks
- Exit when the market catches up
- Flip if later data contradicts the opening thesis
- Hedge across adjacent buckets
- Hold to resolution only when remaining edge justifies settlement risk

Analysis:

1. At market open, create a paper position using the best available forecast or consensus edge.
2. At every new forecast, METAR, or order book snapshot, recalculate fair value.
3. Compare recalculated fair value to current executable bid/ask.
4. Decide whether the simulated strategy should hold, add, trim, exit, flip, or hedge.
5. Track the full path of the position until close/resolution.
6. Compare dynamic management against a simple buy-and-hold-at-open baseline.

Policy variants to test:

- `Hold`: enter at open and hold to settlement.
- `Take profit`: exit when edge compresses below a threshold.
- `Stop loss`: exit when updated fair value moves against the position.
- `Add on confirmation`: increase position after model consensus or METAR confirms.
- `Late hedge`: buy adjacent bucket protection near close.
- `Flip on contradiction`: reverse position when updated data crosses a confidence threshold.

Metrics:

- PnL of dynamic strategy vs open-only hold
- Maximum drawdown per position
- Number of trades per market-day
- Edge captured before settlement
- Slippage paid from over-trading
- Improvement from adds/trims/exits
- Frequency of bad flips
- Best policy by station
- Best policy by forecast timing bucket
- Best policy by market liquidity

Pass/fail criteria:

- Pass if dynamic rebalancing improves risk-adjusted paper PnL after spread/slippage.
- Fail if extra trading costs erase the benefit of better information.
- Watch for overfitting: a policy that works only for one day or one city should not be trusted.

## Test 6 — Negative-Risk Temperature Gaps

Hypothesis:

Temperature bucket structures may create temporary pricing gaps between aggregate markets and exact buckets.

Core idea:

Compare aggregate bucket pricing against the synthetic price implied by the mutually exclusive buckets in the same `neg_risk_market_id`.

For Celsius exact-bucket cities, this means:

- `above_eq X` vs sum of exact buckets at or above `X`
- `below_eq X` vs sum of exact buckets at or below `X`

For Fahrenheit range-bucket cities, the synthetic sum must use bucket intervals, not exact integer buckets.

Analysis:

1. For each `neg_risk_market_id` snapshot, group all exact, range, above, and below buckets.
2. Compute synthetic prices from mutually exclusive exact/range buckets.
3. Compare synthetic prices to actual aggregate market prices.
4. Flag gaps above a threshold, such as `1.5c`.
5. Track duration and fillable size.

Metrics:

- Gap frequency per city/day
- Average gap size
- Median gap duration
- Maximum fillable edge
- Whether gaps close before settlement
- Whether gaps are executable after spread

Pass/fail criteria:

- Pass if gaps are frequent, persistent enough, and executable.
- Fail if gaps exist only on midpoint math but not at executable bid/ask.

## Test 7 — Resolution Source Mismatch Audit

Hypothesis:

Some apparent edges will disappear because fast proxy data, the stated Rules source, and final Polymarket resolution do not always agree perfectly.

Core idea:

For every market, compare:

- Fast proxy station-local high, such as METAR/VHHH/UUWW where available
- Settlement-source daily high: Wunderground, HKO, NOAA, or another stated source
- Any stated Rules source value
- Final Polymarket / UMA outcome

Metrics:

- Match rate: fast proxy vs settlement source
- Match rate: settlement source vs final resolution
- Match rate: fast proxy vs final resolution
- Average difference in native settlement unit and normalized Celsius
- Number of markets where the trade outcome flips depending on source
- Stations with repeated mismatch

Why it matters:

The stale-observation strategy is only as good as the proxy. A fast wrong source is worse than a slow correct source.

## Test 8 — Liquidity / Executability Filter

Hypothesis:

Many apparent edges are not actually tradable because the order book is too thin or the spread is too wide.

Core idea:

Before evaluating strategy performance, classify every signal by whether it was actually executable.

Suggested filters:

- Maximum spread, for example <= `5c` or <= `10c`
- Minimum available size at target price
- Minimum open interest or volume
- Time to close
- Avoid markets where the best ask/bid is stale or tiny

Metrics:

- Percent of signals executable
- Edge before liquidity filter
- Edge after liquidity filter
- PnL by spread bucket
- PnL by available depth
- PnL by time-to-close

Why it matters:

This prevents beautiful-backtest, impossible-trade syndrome.

## Test 9 — Forecast Revision Momentum

Hypothesis:

A forecast revision, especially a late revision, may predict both settlement and future market repricing.

Core idea:

Track when model forecasts move materially, for example from `26C` to `28C`, then test whether the market lags that revision.

Metrics:

- Forecast revision size
- Time between model update and market repricing
- Market price movement after revision
- Accuracy of revised forecast vs previous forecast
- PnL from trading in the direction of the revision

Trading interpretation:

This could become a cleaner signal than raw forecast level. We are not just asking whether GFS is right. We are asking whether GFS just changed in a way the market has not priced yet.

## Test 10 — Local-Time Weather Path Test

Hypothesis:

Markets become much easier to trade after certain local weather milestones: sunrise, midday, peak heating window, or after the likely daily high has passed.

Core idea:

Group signals by local time at the station, not only by hours-before-close.

Timing buckets:

- Before sunrise
- Morning ramp
- Late morning
- Peak heating window
- Post-peak afternoon
- Evening / after likely high

Metrics:

- Forecast error by local-time bucket
- Stale observation signal quality by local-time bucket
- Market repricing speed by local-time bucket
- PnL by local-time bucket

Why it matters:

A 3-hour-before-close signal in London may mean something different than a 3-hour-before-close signal in another city, depending on local weather dynamics and market close rules.

## Test 11 — Station Microclimate Reliability

Hypothesis:

Forecast edges are station-specific because airport geography, coastal effects, elevation, urban heat, and observation practices differ.

Core idea:

Build a station reliability score.

Inputs:

- Historical forecast error
- METAR vs settlement-source mismatch rate
- Intraday volatility
- Frequency of late-day new highs
- Liquidity quality
- Spread quality
- Repricing lag

Output:

- `Tradeable`
- `Watch-only`
- `Avoid`

Why it matters:

We probably do not want one global strategy. We want a whitelist of stations where the proxy, forecast models, and liquidity all behave well.

## Test 12 — Already Priced In Test

Hypothesis:

Some model or METAR signals may be real weather information but already priced in by the market.

Core idea:

For every signal, measure the market move before the signal was available to this system.

Example:

- METAR observation time: `12:50`
- System fetch time: `12:53`
- Market already repriced between `12:50` and `12:53`

That signal should not be counted as a tradeable edge unless there was executable price left after actual fetch time.

Metrics:

- Price movement before signal availability
- Price movement after signal availability
- Edge remaining at first executable snapshot
- Percent of signals already priced in

Why it matters:

This keeps the analysis honest about latency.

## Test 13 — Bucket Adjacency / Hedge Quality

Hypothesis:

Adjacent buckets may offer better risk-adjusted trades than all-or-nothing exact predictions.

Core idea:

Instead of only buying the most likely exact/range bucket, test bundles.

Structures to test:

- Buy `exact 27` + `exact 28`, or adjacent Fahrenheit ranges like `72-73°F` + `74-75°F`
- Buy YES on `above_eq 27` and NO on `above_eq 29`
- Buy center bucket and hedge adjacent
- Sell far tails when model consensus is tight

Metrics:

- PnL by single-bucket vs basket
- Maximum loss by structure
- Capital required
- Edge captured after fees/spread
- Settlement error tolerance

Why it matters:

Weather forecasts are often directionally good but off by `1C`. Basket structures may outperform exact-bucket sniping.

## Test 14 — Data Delay / Source Latency Benchmark

Hypothesis:

Different sources have repeatable latency profiles, and the fastest source may not always be the most settlement-relevant source.

Core idea:

For each observation or forecast, store:

- Observation/model valid time
- Source publish time, if available
- System fetched time
- First time value changed in the database
- First order book snapshot after fetch

Metrics:

- Median latency by source
- 90th percentile latency by source
- Latency by station/source
- PnL sensitivity to latency
- Signal decay curve after publication

Why it matters:

This tells us whether the edge is actually weather insight or source-speed advantage.

## Research Layers

Split analysis into three layers.

### 1. Truth Layer

Question:

What actually resolves the market?

Track:

- Rules source
- Weather Underground or official settlement source
- UMA / Polymarket outcome
- METAR mismatch audit

### 2. Signal Layer

Question:

What predicted or detected the outcome first?

Track:

- METAR stale observation
- Model forecasts
- Model consensus
- Forecast revisions
- Station timing

### 3. Tradeability Layer

Question:

Could we actually make money?

Track:

- Bid/ask
- Size
- Slippage
- Fees
- Latency
- Repricing speed
- Settlement mismatch risk

This structure prevents the classic mistake: proving a signal was right without proving it was settleable, timely, and executable.

## Minimum Sample Before Conclusions

Do not declare a strategy valid from one or two good days.

Minimum suggested sample:

- At least 2 weeks of data
- At least 100 market-days across cities
- At least 50 stale-observation candidate signals
- At least 100 market-open forecast comparisons

## Reporting Format

Each weekly report should include:

- Data coverage: markets, cities, stations, observations, forecasts, order book snapshots
- Missing data by source
- Strategy results by test
- Best and worst cities
- Biggest false positives
- Settlement-source mismatches
- Paper PnL before and after spread/slippage
- Recommendation: continue, pause, or refine

## Current Priority

Run these in order:

1. Resolution source mismatch audit
2. Liquidity / executability filter
3. Already priced in test
4. Data delay / source latency benchmark
5. Forecast revision momentum
6. Stale observation strategy
7. Market-open forecast accuracy
8. Forecast consensus vs market distribution
9. Best forecast timing by station
10. Market-open entry with dynamic rebalancing
11. Negative-risk gaps

The first five priority tests make the whole research plan more robust. They answer whether signals settle correctly, whether they are executable, whether they were already priced in, how much latency matters, and whether forecast changes move the market. After that, the strategy tests can be trusted more.

---

## Audit — Plan Alignment Review (2026-06-01)

This section records findings from cross-checking TESTS_TO_RUN.md against the current
PLAN.md. Each item notes what the test assumes vs what the plan now specifies.

---

### Test 1 — Stale Observation Strategy

**Issue 1.1 — Type C cities excluded after close, not noted here**
The test describes "buy NO on impossible bucket" generically. The plan specifies that
obs_mismatch alerts must NOT fire for Type C cities (NYC, Miami, Wellington) after
12:00 UTC — trading is already closed and no action is possible on afternoon METAR.
Test 1 must explicitly exclude Type C post-close signals from its signal count and PnL.

**Issue 1.2 — Unit conversion not specified**
Examples use Celsius ("Station-local high is already 28C") but NYC and Miami markets
use Fahrenheit buckets. Before comparing `daily_high_c` against a bucket threshold,
apply `round(daily_high_c * 9/5 + 32)` for F-unit markets. Failing to round may flag
the wrong bucket (e.g. 35.28°C = 95.5°F rounds to 96°F, not 95°F). Add this as an
explicit step in the signal logic.

**Issue 1.3 — `daily_high_c` is a query-time aggregate, not a stored field**
The data needed section lists "station-local daily high" as a data field. Per the plan,
`daily_high_c` must be computed at query time as `MAX(temp_c) WHERE station=? AND local_date=?`
— it is not reliably stored as a column (METAR corrections would corrupt it). Queries
for this test must compute the running max fresh, not read a pre-stored value.

**Issue 1.4 — Proxy vs settlement-source mismatch risk for HKO**
The VHHH (Hong Kong) proxy has an unquantified systematic offset vs HKO. Until ≥14
days of VHHH vs HKO settlement comparisons are measured and the offset characterised,
all Hong Kong stale-observation signals must be flagged as "uncalibrated proxy" and
excluded from PnL totals. Add this as an explicit Hong Kong caveat in the test results.

---

### Test 2 — Market-Open Forecast Accuracy

**Issue 2.1 — TAF is not a T-48h source**
Test 2 lists TAF as one of the forecasts to compare at market open. TAF's 30h horizon
means it cannot cover the settlement date for markets discovered ≥30h before close.
For a T-48h market open, TAF contributes zero data. Revise: "TAF is only available
for Q1 when `(close_time_utc - first_seen_utc) <= 30h`. For market-open comparisons,
TAF is absent and only 5 NWP models are available."

**Issue 2.2 — `first_seen_utc` batch bias**
The test uses `first_seen_utc` as market open. Per the plan, this timestamp was
previously set once per batch run (all markets in one discovery run got the same
timestamp), systematically biasing Q1 for markets processed late in the loop. After
the per-market stamping fix is applied, verify that `first_seen_utc` values within
a single run differ by at least a few seconds. Flag results from pre-fix data as
potentially biased.

**Issue 2.3 — ECMWF direct still not built**
"ECMWF direct, once implemented" — remains unbuilt. Do not include ECMWF in model
comparisons until it is collecting and at least 14 days of data exist. The open-meteo
ECMWF mirror adds 1–3h delay and should be labelled `ecmwf_via_openmeteo` not
`ecmwf_direct` to avoid confusion in results.

**Issue 2.4 — Markets discovered after close must be excluded**
Q1 queries must include `AND wm.first_seen_utc < wm.close_time_utc`. Markets
discovered after trading closed will return post-close forecasts as "available at open."
Apply this filter before computing any accuracy metric.

---

### Test 3 — Forecast Consensus vs Market Distribution

**Issue 3.1 — Blocked: only GFS currently collecting**
Test 3 requires GFS, ICON, GEM, ARPEGE, and ECMWF. Only GFS is currently running.
This test cannot produce meaningful consensus results until ≥3 models are collecting.
Mark as **blocked** until ICON and MF/GEM are running and have at least 14 days of data.

**Issue 3.2 — Consensus spread threshold not defined for F-unit markets**
"Flag cases where model spread <= 1.5C" — for NYC/Miami Fahrenheit markets, apply the
equivalent threshold: 1.5°C ≈ 2.7°F. Define the threshold in native bucket units per
city rather than universally in Celsius.

---

### Test 4 — Best Forecast Timing By Station

**Issue 4.1 — model_run_utc is estimated for open-meteo, not actual**
"Group forecasts into timing buckets" using `model_run_utc` — but for GFS/ICON/MF/GEM
via open-meteo, `model_run_utc` is estimated from the known schedule plus fetch offset.
It can be wrong when a model run is delayed. Filter to `model_run_is_estimated=0` for
clean timing analysis, or treat open-meteo model times as ±2h approximations and use
`fetched_utc` as the primary timing signal.

**Issue 4.2 — Hong Kong timing note incomplete**
"Treat HKO/Hong Kong separately from ZBAA" — correct, but the test should also note
that VHHH-HKO proxy offset is unquantified. Treat Hong Kong timing results as provisional
until the proxy offset is characterised from settlement data.

---

### Test 5 — Market-Open Entry With Dynamic Rebalancing

**Issue 5.1 — "Fair value" is undefined**
The test says "recalculate fair value" at every new data point but never defines it.
Specify the computation: fair value for a bucket = model-ensemble probability that the
settlement temperature falls in that bucket's range, converted to a probability using
the model high's distribution (e.g. assume ±1.5°C Gaussian error, compute bucket
probability). Without a definition, "fair value" is not reproducible.

**Issue 5.2 — Polymarket taker fees not included**
The metrics list "PnL by dynamic strategy" but do not include Polymarket taker fees
(typically 0.2–0.5% of notional per fill, deducted from winnings). Every simulated
PnL must subtract fees per trade. Over-trading policies will appear profitable before
fees but lose money after. Add "Net PnL after fees" alongside gross PnL in metrics.

---

### Test 6 — Negative-Risk Temperature Gaps

**Issue 6.1 — Gap threshold changed to 2¢ on executable prices**
The test uses "1.5c" as the gap flag threshold. The plan now specifies 2¢ on executable
prices (bid for the sell leg, ask for the buy leg), not midpoints. Update the threshold
and note that midpoint gaps must be ≥ ~4¢ to produce a 2¢ executable gap after spread.

**Issue 6.2 — Both gap directions required**
The test only describes the forward direction (above_eq underpriced vs bucket sum).
The plan now requires checking the reverse direction too (bucket sum overpriced vs
above_eq). Add reverse gap analysis and use separate metric rows for each direction.

**Issue 6.3 — Multi-group neg_risk_market_id not handled**
"For each `neg_risk_market_id` snapshot" — the scanner must first group by
`(city, settlement_date)` and check for multiple `neg_risk_market_id` values. If
two groups exist for the same city+date, run the consistency check across all buckets
combined and flag cross-group gaps separately. The test does not address this case.

**Issue 6.4 — NULL `neg_risk_market_id` must be excluded**
Markets with NULL `neg_risk_market_id` must not participate in the scan. The test
does not mention this filter. Add `WHERE neg_risk_market_id IS NOT NULL` to all
scanner queries.

---

### Test 7 — Resolution Source Mismatch Audit

**Issue 7.1 — Currently blocked: WU adapter not built**
This test requires `settlement_observations` rows for WU markets (~82% of all markets).
Until `wunderground_daily` adapter is built and `WU_API_KEY` is configured, this test
can only run for Hong Kong (HKO) and Moscow (NOAA). Mark as **partially blocked**.

---

### Test 8 — Liquidity / Executability Filter

**Issue 8.1 — Spread threshold not reconciled with gap threshold**
The test suggests "maximum spread <= 5c or <= 10c" as a liquidity filter, but the
neg-risk gap threshold is 2¢ on executable prices. A market with a 5¢ spread produces
a 2.5¢ cost per leg, which eliminates any gap below 5¢. The liquidity filter threshold
and the gap threshold must be chosen consistently. Recommend: use spread ≤ 4¢ as the
executability filter so that a 2¢ gap survives after spread on both legs.

---

### Test 9 — Forecast Revision Momentum

**Issue 9.1 — model_run_utc estimated; revision detection unreliable**
"Track when model forecasts move materially" requires comparing successive model runs.
For open-meteo models, `model_run_utc` is estimated (±2h). Two rows with different
`model_run_utc` estimates may actually be from the same run. Filter to
`model_run_is_estimated=0` (ECMWF direct only, once built) for reliable revision
detection, or accept that open-meteo revision analysis has ~2h ambiguity in run timing.

---

### Test 10 — Local-Time Weather Path

**Issue 10.1 — Peak heating window hours are city-type dependent**
The timing buckets (sunrise, morning ramp, peak heating, post-peak) must use station
local time, not UTC. The plan's Type A/B/C typology defines when the peak window
occurs per city. Use `local_hour` from `wx_observations` for this bucketing, and
align bucket edges with the Type A/B/C peak windows in PLAN.md.

---

### Priority Order — Revised

The original priority order below is updated to reflect current blocked status:

| Priority | Test | Status | Blocker |
|---|---|---|---|
| 1 | Liquidity / Executability Filter (Test 8) | ✅ Ready | — |
| 2 | Already Priced In (Test 12) | ✅ Ready | — |
| 3 | Data Delay / Source Latency (Test 14) | ✅ Ready | — |
| 4 | Local-Time Weather Path (Test 10) | ✅ Ready | — |
| 5 | Stale Observation Strategy (Test 1) | ⚠️ Partial | HKO/NOAA only until WU built; Type C caveat required |
| 6 | Negative-Risk Gaps (Test 6) | ✅ Ready | — |
| 7 | Resolution Source Mismatch (Test 7) | ⛔ Partial | WU adapter missing |
| 8 | Market-Open Forecast Accuracy (Test 2) | ⚠️ Partial | Only GFS; TAF T-48h limitation |
| 9 | Forecast Revision Momentum (Test 9) | ⚠️ Partial | model_run_utc estimated for open-meteo |
| 10 | Forecast Consensus vs Market (Test 3) | ⛔ Blocked | Requires ≥3 models |
| 11 | Best Forecast Timing (Test 4) | ⛔ Blocked | Requires ≥3 models + 14 days |
| 12 | Dynamic Rebalancing (Test 5) | ⛔ Blocked | Requires settlement + multiple models |
| 13 | Station Microclimate Reliability (Test 11) | ⛔ Blocked | Requires 2+ weeks + settlement |
| 14 | Bucket Adjacency / Hedge Quality (Test 13) | ⛔ Blocked | Requires settlement data |

Run tests in priority order. Do not advance to blocked tests before their prerequisites
are met. See PREPRODUCTION_TESTS.md for system validation tests that must pass before
any analytical tests can be trusted.
