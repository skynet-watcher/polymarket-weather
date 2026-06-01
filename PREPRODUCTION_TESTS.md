# Pre-Production Test Plan

Owner: Chad  
Purpose: Verify every system component works correctly before live data collection begins.
These tests must all pass before Phase 1 data collection can be trusted for research.

These are not analytical strategy tests (see TESTS_TO_RUN.md). They are correctness and
integration tests — does the system collect, store, and compute data accurately?

Each test specifies: what to run, what to check, and the pass/fail criterion.

---

## Group 1 — Schema and Database

### P-01: Schema initialises cleanly on a fresh database

**Run**: `python scripts/init_db.py` against an empty file.

**Check**:
- All tables exist: `wx_observations`, `taf_forecasts`, `model_forecasts`, `weather_markets`,
  `ob_snapshots`, `fetch_log`, `alerts`, `settlement_observations`, `market_resolutions`.
- All indexes exist (UNIQUE, covering, lookup).
- `taf_forecasts` contains `forecast_local_date` column.
- `weather_markets` contains `temp_window_start_utc`, `cancelled_at_utc`, `active`.
- `alerts` contains `status`, `opened_utc`, `last_seen_utc`, `closed_utc`, `settlement_date`.
- `PRAGMA journal_mode` returns `wal`.
- `PRAGMA busy_timeout` returns `10000`.

**Pass**: All tables, columns, indexes, and PRAGMA values correct. Zero errors.  
**Fail**: Any missing column, wrong PRAGMA, or CREATE TABLE error.

---

### P-02: Migration adds missing columns to a legacy database

**Run**: Create a database with the old schema (missing `forecast_local_date`,
`cancelled_at_utc`, `temp_window_start_utc`). Run `init_db.py`.

**Check**:
- `_migrate_existing_tables()` adds each missing column without error.
- Existing rows are preserved intact.
- No duplicate column errors on a second `init_db.py` run.

**Pass**: All missing columns added; existing data intact; idempotent.  
**Fail**: Error on migration; data loss; column added twice.

---

### P-03: Concurrent write safety under WAL mode

**Run**: Start three processes simultaneously:
1. A loop inserting 1000 rows into `wx_observations`.
2. A loop inserting 1000 rows into `ob_snapshots`.
3. A loop reading all active markets.

**Check**:
- No `OperationalError: database is locked` in any process.
- Final row counts match expected inserts exactly.
- No partial rows or corrupt data.

**Pass**: All inserts succeed; counts match; no lock errors.  
**Fail**: Any lock error; any missing row; any data corruption.

---

### P-04: `INSERT OR IGNORE` idempotency for model and TAF forecasts

**Run**: Insert the same `model_forecasts` row twice (same `station`, `model`,
`model_run_utc`, `forecast_date`). Repeat for `taf_forecasts` (same `station`, `issued_utc`).

**Check**:
- Second insert is silently skipped.
- Row count = 1 for each table.
- No exception raised.

**Pass**: Second insert ignored; count = 1; no error.  
**Fail**: Exception raised; duplicate row inserted.

---

## Group 2 — Discovery

### P-05: `discover_markets.py` finds known live markets

**Run**: `python scripts/discover_markets.py --days-ahead 1` against live Gamma API.

**Check**:
- At least 10 of 17 configured cities return markets for today or tomorrow.
- Each stored market has non-NULL: `condition_id`, `city`, `station`, `settlement_date`,
  `bucket_type`, `close_time_utc`, `temp_window_start_utc`, `neg_risk_market_id`,
  `yes_token_id`, `no_token_id`, `first_seen_utc`.
- `bucket_type` is never `'unknown'` for at least 15 of 17 cities (check `alerts`
  table for any `unparseable_bucket` entries and investigate).
- No city has `settlement_date` that disagrees with the date in the event slug.
- `first_seen_utc` differs per market (not all identical — per-market stamping confirmed).

**Pass**: ≥10 cities discovered; all required fields populated; no timestamp collisions.  
**Fail**: <10 cities; any required field NULL; all `first_seen_utc` identical.

---

### P-06: `settlement_date` derivation is correct for all 17 cities

**Run**: After discovery, query `weather_markets` and compare `settlement_date` to the
date embedded in `event_slug` for all stored markets.

**Check**:
- For every market: `settlement_date` matches the date in `event_slug`.
- Wellington markets: `settlement_date` is the NZST local date, not the UTC date
  (i.e., for `close_time_utc = '2026-XX-XXT12:00:00Z'`, Wellington's `settlement_date`
  is the prior calendar day in NZST).
- No market has `settlement_date = NULL`.
- `temp_window_start_utc` when converted to station local timezone gives a date
  matching `settlement_date` for all 17 stations.

**Pass**: All `settlement_date` values match slug date; Wellington correct; no NULLs.  
**Fail**: Any mismatch; Wellington one day off; any NULL.

---

### P-07: Post-close discovery guard works

**Run**: Manually insert a market with `close_time_utc` 2 hours in the past into
`weather_markets`. Re-run `discover_markets.py` and observe the upsert.

**Check**:
- Market is set `active=0` if `first_seen_utc >= close_time_utc`.
- A warning is logged.
- No `ob_snapshots` are collected for it after the guard triggers.

**Pass**: `active=0` set; warning logged.  
**Fail**: Market stays `active=1`; no warning; snapshots continue.

---

### P-08: Slug failure detection alerts

**Run**: Temporarily corrupt one city slug in `city_stations.json` (e.g. add a typo).
Run `discover_markets.py`.

**Check**:
- The broken city returns 0 markets silently (HTTP 404).
- If ≥3 cities return 0 markets, an `alert_type='discovery_slug_failure'` row
  is inserted into `alerts`.
- The alert includes which cities returned zero.

**Pass**: Alert inserted when ≥3 cities fail.  
**Fail**: Failure silent; no alert; run continues as if normal.

---

## Group 3 — METAR and Weather Collection

### P-09: METAR batch fetch returns all 17 stations

**Run**: `python scripts/fetch_weather.py --metar` (single pass).

**Check**:
- Response covers all 17 configured ICAO codes.
- Each stored row has `observed_utc` ending in `Z` or `+00:00` (UTC validation).
- `local_date` for each row matches the station's local calendar date
  (verify Tokyo: `local_date` should be JST date, not UTC date after 15:00 UTC).
- `fetched_utc > observed_utc` for all rows (fetch is always after observation).
- `fetch_log` records `n_records=17` for the batch.

**Pass**: 17 stations; UTC-encoded timestamps; correct local dates; fetch > observed.  
**Fail**: Any station missing; missing UTC suffix; wrong local date; n_records ≠ 17.

---

### P-10: Station coverage alert fires on partial response

**Run**: Mock the METAR API to return only 15 of 17 stations (omit UUWW and NZWN).

**Check**:
- A per-station `alert_type='fetch_failure'` is inserted for UUWW and NZWN.
- `fetch_log.n_records = 15`, not 17.
- Remaining 15 stations are stored correctly.

**Pass**: Alert per missing station; n_records correct; 15 rows stored.  
**Fail**: Missing stations silently skipped; n_records shows 17; no alert.

---

### P-11: `daily_high_c` is a correct query-time aggregate

**Run**: Insert three `wx_observations` rows for RJTT (Tokyo) on `local_date='2026-06-01'`
with `temp_c` values of 28.0, 31.5, 29.0 (in that order). Then insert a corrected row
for the 31.5 observation (same `observed_utc`) with `temp_c=29.5` (COR METAR).

**Check**:
- After correction, `SELECT MAX(temp_c) WHERE station='RJTT' AND local_date='2026-06-01'`
  returns 29.5 (not 31.5).
- UNIQUE index conflict resolves via `ON CONFLICT REPLACE`.
- No stale 31.5 value remains anywhere.

**Pass**: Corrected max = 29.5; no stale value.  
**Fail**: Max still 31.5 after correction; correction silently ignored.

---

### P-12: `local_date` is computed in station timezone, not UTC

**Run**: Insert a METAR observation for RJTT with `observed_utc = '2026-06-01T23:45:00Z'`
(which is 2026-06-02T08:45 JST).

**Check**:
- Stored `local_date = '2026-06-02'` (JST date, not UTC date).
- Stored `local_hour = 8` (JST hour).

**Pass**: `local_date` = JST date; `local_hour` = 8.  
**Fail**: `local_date = '2026-06-01'` (UTC date); `local_hour` based on UTC.

---

### P-13: DST transition day window uses ZoneInfo next-midnight, not +24h

**Run**: For NYC on the spring-forward date (2026-03-08):
- `temp_window_start_utc = '2026-03-08T05:00:00Z'` (midnight EST).
- Compute `window_end` using both methods:
  - Wrong: `window_start + timedelta(hours=24)` = `2026-03-09T05:00:00Z`
  - Correct: ZoneInfo next-local-midnight = `2026-03-09T04:00:00Z`

**Check**:
- System uses the ZoneInfo method.
- `window_end = '2026-03-09T04:00:00Z'` (23h window on spring-forward day).
- No METAR or ECMWF steps between 04:00 and 05:00 UTC on March 9 are included
  in the March 8 daily high.

**Pass**: Window end = 04:00 UTC; 23h window.  
**Fail**: Window end = 05:00 UTC; 24h window captures wrong temperatures.

---

## Group 4 — Order Book Collection

### P-14: Orderbook snapshots store all required fields

**Run**: `python scripts/log_orderbooks.py --once` against live CLOB.

**Check**:
- At least one snapshot per active market.
- Every row has: `condition_id`, `ts_utc`, `yes_bid`, `yes_ask`, `yes_bid_size`,
  `yes_ask_size`, `raw_book_json`, `hours_to_close`, `snapshot_label`.
- `hours_to_close` is correctly computed as `(close_time_utc - ts_utc)` in hours.
- `snapshot_label` follows the defined bin table (T-30min, T-1h, T-3h, T-6h, T-12h,
  T-24h, open_window, post_close).
- No label value outside the defined set.

**Pass**: All fields present; correct label values; correct hours_to_close.  
**Fail**: Any field NULL when unexpected; wrong label; hours_to_close sign error.

---

### P-15: Empty-book post-close snapshots are stored, not dropped

**Run**: Mock the CLOB to return empty books (`{"bids":[],"asks":[]}`) for a market
with `hours_to_close < 0`.

**Check**:
- A row IS inserted into `ob_snapshots`.
- All price fields (yes_bid, yes_ask, etc.) are NULL.
- `raw_book_json = '{}'` or similar empty representation.
- `snapshot_label = 'post_close'`.

**Pass**: Row inserted; prices NULL; label = post_close.  
**Fail**: Row not inserted (None returned and discarded).

---

### P-16: `open` label fires exactly once per market

**Run**: Run the orderbook logger continuously for 10 minutes on a newly discovered market.

**Check**:
- Exactly one snapshot has `snapshot_label = 'open'` for this market.
- Its `ts_utc` is within 5 minutes of the market's `first_seen_utc`.
- No subsequent snapshot overwrites the `open` label.
- `first_seen_utc` is loaded into the market dict (not NULL in the query).

**Pass**: Exactly one `open` label; within 5 min of `first_seen_utc`.  
**Fail**: Zero `open` labels; multiple `open` labels; `open` on wrong snapshot.

---

## Group 5 — Settlement Pipeline

### P-17: Bucket parser handles all known question formats

**Run**: Unit-test `_parse_bucket()` against the following question strings:

| Question | Expected bucket_type | Expected lower | Expected upper | Expected unit |
|---|---|---|---|---|
| `Will the highest temperature in Seoul be 31°C on June 1?` | `exact` | 31.0 | 31.0 | C |
| `Will the highest temperature in NYC be between 88-89°F on June 1?` | `range` | 88.0 | 89.0 | F |
| `Will the highest temperature in Tokyo be 35°C or above on June 1?` | `above_eq` | 35.0 | None | C |
| `Will the highest temperature in London be 18°C or below on June 1?` | `below_eq` | None | 18.0 | C |
| `Will the highest temperature in Miami be between 92-93°F on June 1?` | `range` | 92.0 | 93.0 | F |

**Check**: All five return correct `(bucket_type, lower, upper, unit)` tuples.  
Add any question format seen in `raw_market_json` that differs from the above.

**Pass**: All formats parsed correctly; no `'unknown'` returns for known formats.  
**Fail**: Any `'unknown'` for a known format; wrong unit; wrong threshold.

---

### P-18: `bucket_type='unknown'` triggers alert and skip

**Run**: Insert a market with `bucket_type='unknown'` into `weather_markets`. Run
`settle_markets.py`.

**Check**:
- `settle_markets.py` skips this market (no `settlement_value_proxy` written).
- An `alert_type='unparseable_bucket'` row exists in `alerts` for this `condition_id`.
- The alert `detail_json` includes the `question` text.

**Pass**: Market skipped; alert inserted with question text.  
**Fail**: `settle_markets.py` crashes or writes a wrong `proxy_outcome`.

---

### P-19: Settlement post-condition: exactly one YES per city+date

**Run**: After running `settle_markets.py` on a day with known settlement data,
query for the count of YES `proxy_outcome` per `(city, settlement_date)`.

**Check**:
- Every `(city, settlement_date)` group has exactly 1 `proxy_outcome = 'YES'`.
- No group has 0 YES (settlement value out of all bucket ranges) or >1 YES
  (overlapping bucket definitions).
- Any violations have `resolution_status = 'disputed'` and an
  `alert_type='settlement_integrity_error'` in `alerts`.

**Pass**: Exactly 1 YES per city+date; no disputes for well-formed staircases.  
**Fail**: 0 or >1 YES with no `disputed` flag; no alert on violation.

---

### P-20: C→F conversion uses whole-degree rounding before bucket comparison

**Run**: Unit-test the obs_mismatch comparison with `daily_high_c = 35.28` and
`bucket_unit = 'F'`.

**Check**:
- Converted value = `round(35.28 * 9/5 + 32)` = `round(95.504)` = 96 (not 95.504).
- Bucket comparison uses 96, not 95.504.
- Alert fires for the 95–96°F bucket (lower=95, upper=96), not the 94–95°F bucket.

**Pass**: Rounded value = 96; comparison uses 96; correct bucket flagged.  
**Fail**: Raw float 95.504 used; wrong bucket; alert mismatch.

---

### P-21: `settle_markets.py` trigger fires at correct UTC time per city

**Run**: Verify the polling trigger condition against known per-city local midnight times.

**Check** for each city that `datetime(temp_window_start_utc, '+26 hours') < datetime('now')`
fires at the correct UTC wall-clock time (cross-reference the settle_markets schedule table
in PLAN.md):

| City | temp_window_start_utc | Expected trigger UTC |
|---|---|---|
| Seoul/Tokyo | `T15:00:00Z` day D | `T17:00:00Z` day D |
| Wellington | `T12:00:00Z` day D | `T14:00:00Z` day D |
| NYC/Miami | `T05:00:00Z` day D | `T07:00:00Z` day D (approx, EDT) |
| London | `T23:00:00Z` day D | `T01:00:00Z` day D+1 |

**Pass**: Trigger fires within ±5 minutes of expected UTC time for each city.  
**Fail**: Any city settles 12h early (wrong `temp_window_start_utc`) or never triggers.

---

## Group 6 — Neg-Risk Scanner

### P-22: Scanner correctly identifies a seeded gap

**Run**: Insert synthetic `ob_snapshots` for a Seoul staircase with a known forward gap:
- `above_eq 32°C`: `yes_bid = 0.65`, `yes_ask = 0.67`
- `exact 32°C`: `yes_ask = 0.20`
- `exact 33°C`: `yes_ask = 0.20`
- `exact 34°C` and above: `yes_ask = 0.05`
- Sum of asks for buckets ≥ 32°C = 0.45. Gap = bid(0.65) − sum_ask(0.45) = 0.20 > 2¢.

**Check**:
- Scanner detects `forward_gap = 0.20` and inserts `alert_type='neg_risk_gap_forward'`.
- Alert `detail_json` includes `city`, `threshold_temp`, `gap_size`, `condition_ids`.

**Pass**: Alert inserted; gap size correct; direction correct.  
**Fail**: No alert; wrong gap size; wrong direction.

---

### P-23: Scanner correctly identifies a seeded reverse gap

**Run**: Insert synthetic snapshots where `P_sum(bid) − P_above(ask) > 2¢`:
- `above_eq 32°C`: `yes_bid = 0.40`, `yes_ask = 0.42`
- `exact 32°C`: `yes_bid = 0.25`
- `exact 33°C`: `yes_bid = 0.20`
- Sum bids ≥ 32°C = 0.45. Reverse gap = 0.45 − 0.42 = 0.03 > 2¢.

**Check**:
- Scanner detects `reverse_gap` and inserts `alert_type='neg_risk_gap_reverse'`.
- Forward gap check does NOT fire (forward gap is negative).

**Pass**: Reverse alert inserted; forward alert absent.  
**Fail**: No alert; forward alert fires incorrectly.

---

### P-24: Scanner excludes NULL `neg_risk_market_id` markets

**Run**: Insert a market with `neg_risk_market_id = NULL`. Run the scanner.

**Check**:
- Market is excluded from all scanner group queries.
- Scanner does not crash.
- An `alert_type='incomplete_market_data'` exists for this market in `alerts`.

**Pass**: Market excluded; no crash; incomplete_market_data alert present.  
**Fail**: Market included in a NULL-grouped staircase; scanner crashes.

---

### P-25: Alert deduplication — persistent gap does not generate unbounded rows

**Run**: Run the scanner 10 times with the same seeded gap still present.

**Check**:
- `alerts` table contains exactly 1 row for this gap (not 10).
- `last_seen_utc` updates on each scan.
- `status = 'open'`.
- After removing the seeded gap and running once more, `status = 'closed'`
  and `closed_utc` is set.

**Pass**: 1 alert row; `last_seen_utc` advances; closes correctly.  
**Fail**: 10 rows inserted; no deduplication; never closes.

---

## Group 7 — Fetch Reliability and Alerting

### P-26: Consecutive METAR failure triggers `fetch_failure` alert

**Run**: Mock the METAR API to return HTTP 500 for 3 consecutive cycles.

**Check**:
- Each cycle logs 3 attempts with `status='failed'` in `fetch_log`.
- After the 3rd consecutive failed cycle (within a 2h window),
  an `alert_type='fetch_failure'` row is inserted into `alerts`.
- `detail_json` includes source name, station list, first failure time.

**Pass**: Alert inserted after 3rd consecutive failure cycle.  
**Fail**: No alert; alert fires after first failure (not 3); alert after too many.

---

### P-27: GFS stale-run detection fires on delayed model

**Run**: Mock `fetch_weather.py` to return GFS data where `fetched_utc - configured_offset`
is 3 hours past the nominal run time (simulating a 3h GFS delay).

**Check**:
- `fetch_log` records `status='stale_run'` for this fetch.
- `model_forecasts.model_run_is_estimated = 1`.
- `raw_payload_json` contains a note about the suspected stale run.
- No alert for a delay of 1.5h (within the 2h tolerance).

**Pass**: `stale_run` logged at 3h delay; not logged at 1.5h.  
**Fail**: No stale run detection; logged at 1.5h (too sensitive).

---

## Group 8 — End-to-End Smoke Test

### P-28: Full pipeline run — one city, one day

**Run**: Against live APIs, run the full pipeline for a single city (Seoul recommended,
Type A, Celsius, clear bucket structure):

1. `discover_markets.py --days-ahead 1`
2. `fetch_weather.py --metar`
3. `log_orderbooks.py --once`
4. `fetch_weather.py --forecasts --model gfs_seamless`
5. Wait for Seoul local midnight + 2h, then `settle_markets.py --date <Seoul date>`

**Check**:
- `weather_markets` has Seoul markets with all required fields.
- `wx_observations` has Seoul RKSI rows with correct `local_date` (KST).
- `ob_snapshots` has Seoul rows with correct `hours_to_close` and `snapshot_label`.
- `model_forecasts` has Seoul GFS rows with `forecast_date` in KST.
- After settlement: exactly one bucket has `proxy_outcome = 'YES'`.
- `resolution_status = 'proxy_only'`.
- No `settlement_integrity_error` alerts.

**Pass**: All five tables populated; correct dates; exactly 1 YES; clean alerts.  
**Fail**: Any field wrong, missing, or wrong date reference frame.

---

## Pass/Fail Summary

All 28 pre-production tests must pass before Phase 1 collection is considered reliable.

| Group | Tests | Focus |
|---|---|---|
| 1 — Schema | P-01 to P-04 | Schema correctness, WAL mode, idempotency |
| 2 — Discovery | P-05 to P-08 | Market discovery, settlement dates, slug validation |
| 3 — METAR | P-09 to P-13 | Timestamps, local dates, station coverage, DST |
| 4 — Order Books | P-14 to P-16 | Snapshot fields, empty books, open label |
| 5 — Settlement | P-17 to P-21 | Bucket parsing, YES integrity, trigger timing |
| 6 — Scanner | P-22 to P-25 | Gap detection both directions, deduplication |
| 7 — Reliability | P-26 to P-27 | Failure alerting, stale run detection |
| 8 — End-to-End | P-28 | Full pipeline smoke test |

**Blocking failures** (system cannot collect reliably until fixed):
- P-01: WAL mode not configured
- P-03: Concurrent write contention
- P-09: METAR missing stations
- P-12: Wrong `local_date` timezone
- P-19: Multiple YES per city+date

**Non-blocking but required before analysis**:
- All remaining tests

---

## Test Execution Order

Run in this sequence — each group depends on the previous:

```
P-01 → P-02 → P-03 → P-04          (schema must be correct before anything else)
P-05 → P-06 → P-07 → P-08          (discovery before collection)
P-09 → P-10 → P-11 → P-12 → P-13  (METAR correctness before order books)
P-14 → P-15 → P-16                  (order book collection)
P-17 → P-18 → P-19 → P-20 → P-21  (settlement pipeline)
P-22 → P-23 → P-24 → P-25          (scanner)
P-26 → P-27                          (reliability)
P-28                                  (full smoke test — run last)
```
