# Chad Plan Review — 2026-05-31

Reviewer: Chad

Scope: Logic and architecture review of `PLAN.md` for the active Polymarket Weather project.

## Summary

The plan is directionally sound: collect forecasts, station observations, and market order books, then compare model expectations, actual station readings, and market prices.

The project should keep going, but several assumptions need to be tightened before the data can be trusted for research conclusions or trading decisions.

## Logic Review

### 1. City count needs correction

`PLAN.md` says Polymarket runs daily staircase temperature markets for 16 cities, but the station table lists 17 city markets:

- Seoul
- Hong Kong
- London
- Tokyo
- NYC
- Paris
- Beijing
- Miami
- Singapore
- Madrid
- Moscow
- Munich
- Amsterdam
- Ankara
- Wellington
- Shenzhen
- Guangzhou

The implementation has 16 unique settlement stations because Hong Kong and Beijing both map to `ZBAA`.

Recommended wording:

> Polymarket currently tracks 17 city markets across 16 unique settlement stations.

### 2. Use a three-layer settlement model

The plan currently calls METAR observations “the exact same data WU uses to settle markets.” That is probably close enough operationally for live signal detection, but too strong for research-grade wording.

Better framing:

- METAR is the fastest leading proxy for airport-station outcomes.
- The market's stated rules source is the source-of-record for settlement.
- The final resolved state is the Polymarket/UMA outcome.

Do not hard-code “Weather Underground final daily summary” as universal. Some markets may specify NOAA, NWS, Weather Underground, local meteorological services, or a particular station/source. The system should parse and store the exact Rules source per market.

Recommended feed hierarchy:

- `Fast proxy`: METAR / AviationWeather, used for the trade clock.
- `Settlement proxy`: the same source or derivative named in market rules, such as a WU station daily summary.
- `Final state`: Polymarket/UMA resolution outcome.

This gives the project a clean two-clock model:

- `Trade clock`: driven by METAR and other fast station feeds.
- `Settlement clock`: driven by the rules-confirmed source and final Polymarket resolution.

Architecture implication:

Store `rules_text`, `rules_source`, `resolution_source_url`, and `final_resolution_outcome` per market. Treat METAR-derived settlement values as reconciled proxies, not final truth.

### 3. Daily high must use station-local date boundaries

The plan correctly says daily high should be computed since local midnight. The current code does not yet enforce this everywhere.

Risk:

Using UTC date boundaries will miscompute daily highs for cities outside UTC, especially around midnight. Tokyo, Wellington, NYC, Miami, Seoul, and Singapore are obvious examples.

Required schema/data changes:

- Add `timezone` per city/station in `data/city_stations.json`.
- Store `local_date` on `wx_observations`.
- Compute `daily_high_c` by `(station, local_date)`, not `DATE(ts_utc)`.

### 4. Market open should be named honestly

The plan says `open_time_utc` is the moment `discover_markets.py` first finds a market. That is useful, but it is not necessarily the actual Polymarket creation/open time.

Recommended rename or clarification:

- Use `first_seen_utc` for discovery time.
- Only use `open_time_utc` if it comes from Polymarket/Gamma metadata.

For Q1, “forecast available at market open” should mean:

> Most recent forecast fetched before `first_seen_utc`, unless a true market creation timestamp is available.

### 5. Q2 settlement logic must handle bucket types

The sample query in `PLAN.md` only works for exact buckets:

```sql
CASE WHEN wm.settlement_temp_c = wm.temp_c THEN 'YES'
     ELSE 'NO' END as resolved
```

That is incomplete because weather markets can include `above_eq` and `below_eq`.

Correct logic:

```sql
CASE
  WHEN wm.bucket_type = 'exact' AND wm.settlement_temp_c = wm.temp_c THEN 'YES'
  WHEN wm.bucket_type = 'above_eq' AND wm.settlement_temp_c >= wm.temp_c THEN 'YES'
  WHEN wm.bucket_type = 'below_eq' AND wm.settlement_temp_c <= wm.temp_c THEN 'YES'
  ELSE 'NO'
END as resolved
```

### 6. Forecast availability needs real availability timestamps

The Q1 query is logically right if `fetched_utc` is the moment the forecast became available to this system.

Keep both:

- `model_run_utc`: when the model run initialized.
- `fetched_utc`: when this repo actually acquired the forecast.

Use `fetched_utc <= first_seen_utc` for “available to us at market open.”

## Architecture Review

### 1. Create one authoritative schema/migration path

Right now, different scripts create different partial versions of the schema.

Examples:

- `fetch_weather.py` creates weather/forecast/fetch tables.
- `discover_markets.py` creates market/orderbook/alert tables and older `wx_forecasts`.
- Current `weather.db` does not yet include the planned `weather_markets`, `ob_snapshots`, or `alerts` tables unless discovery has run.

This will create drift and fragile runtime behavior.

Recommendation:

Create one schema owner:

- `scripts/init_db.py`, or
- `schema.sql` plus a small initializer.

Each operational script should call the same initializer, not define its own table layout.

### 2. Make `data/city_stations.json` the single source of truth

Station metadata is currently split:

- `data/city_stations.json` has cities, slugs, stations, WU paths, and anomaly flags.
- `fetch_weather.py` hardcodes station lat/lon and city labels.

This will drift.

Recommendation:

Move all station metadata into `data/city_stations.json`:

- city
- market slug
- station
- lat
- lon
- timezone
- country
- WU path
- anomaly flag
- anomaly note

All scripts should load this file.

### 3. Harden market discovery

`discover_markets.py` currently scrapes Polymarket HTML and matches condition IDs to questions by index.

Risk:

If page structure changes, condition IDs and questions can silently pair incorrectly.

Recommendation:

Prefer structured market metadata from Gamma/CLOB where possible. If HTML scraping remains necessary, store raw discovery payloads or extracted page snapshots for auditability.

### 4. Orderbook labels are useful, but raw snapshots matter more

The planned `snapshot_label` values are good for analysis:

- `open`
- `T-24h`
- `T-12h`
- `T-6h`
- `T-3h`
- `T-1h`
- `T-30m`
- `close`

But the system should never depend only on labeled rows. Keep every two-minute raw snapshot and derive standard interval views later if needed.

Recommendation:

Store:

- `ts_utc`
- `hours_to_close`
- nullable `snapshot_label`

Then analysis can select nearest snapshots even if polling misses the exact five-minute label window.

### 5. Scheduler needs a true next-run calculation

The plan’s fetch cadence is sensible, but the implementation should calculate “next source run plus offset” directly after each fetch.

Risk:

Naive interval rescheduling can drift away from actual model run cadence.

Recommendation:

Reuse a single scheduling function that computes the next intended fetch time from model run times and offsets every time a job completes.

### 6. Settlement deserves a dedicated module

The planned `settle_markets.py` is the right move.

Recommended output fields:

- `settlement_temp_c_proxy_metar`
- `settlement_temp_c_final`
- `settlement_source`
- `settled_at_utc`
- `resolution_status`

This separates fast intraday proxy logic from final research truth.

### 7. Add uniqueness and idempotency constraints

To keep continuous collection safe, add uniqueness where repeated fetches should not duplicate rows.

Recommended constraints:

- `wx_observations`: unique on `(station, city, source, observation_time_utc)` once actual observation time is stored.
- `taf_forecasts`: unique on `(station, issued_utc)`; already intended.
- `model_forecasts`: unique on `(station, model, model_run_utc, forecast_date)`.
- `weather_markets`: primary key on `condition_id`; already intended.
- `ob_snapshots`: probably no unique constraint unless `ts_utc` is bucketed to a collection cycle ID.

### 8. Store source timestamps separately from fetch timestamps

For observations and forecasts, fetch time is not the same as source-valid time.

Recommended fields:

- METAR: `observed_utc` and `fetched_utc`
- TAF: `issued_utc`, `valid_from_utc`, `valid_to_utc`, and `fetched_utc`
- NWP: `model_run_utc`, `forecast_date`, `fetched_utc`
- Markets: `first_seen_utc`, `close_time_utc`, and source-provided event/market timestamps if available

## Suggested Next Steps

1. Update `PLAN.md` to say 17 city markets / 16 unique stations.
2. Add `timezone`, `lat`, and `lon` to `data/city_stations.json`.
3. Create a single schema initializer.
4. Add local-date handling for METAR daily highs.
5. Fix Q2 settlement logic for `exact`, `above_eq`, and `below_eq`.
6. Add `settle_markets.py`.
7. Refactor scripts to read all station metadata from `data/city_stations.json`.
8. Harden market discovery using structured metadata where possible.

## Bottom Line

Chad assessment: the plan is good enough to continue, but not yet good enough to trust the resulting analysis without these fixes.

The two highest-risk issues are:

1. UTC date boundaries producing wrong daily highs.
2. Treating fast METAR readings as final settlement truth instead of a proxy.

Fix those first.
