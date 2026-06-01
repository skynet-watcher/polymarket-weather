# Chad Full Audit — 2026-06-01

Scope: audit the updated plan, current repo implementation, current SQLite schema, and live Polymarket weather market rules to determine whether the system will log data correctly for analysis.

## Verdict

The updated plan is logically much stronger than the previous version, but the repo is **not yet internally consistent** and would not produce analysis-grade data if run as-is.

The biggest issue is not the research design. The biggest issue is that the plan describes a future architecture, while the current scripts and database still implement the older, partial schema.

Do not rely on collected data for strategy conclusions until the blockers below are fixed.

## Status After Overnight Fix Pass

Resolved:

- `scripts/init_db.py` now exists as the shared schema owner.
- `data/city_stations.json` now has timezone, lat/lon, units, source metadata, and corrected stations for Hong Kong and Moscow.
- `fetch_weather.py` now stores `observed_utc`, `fetched_utc`, station-local `local_date`, local hour, local-day highs, and raw METAR/model payloads.
- `discover_markets.py` now performs rules-first Gamma/CLOB discovery and stores rules/source/unit/bucket/token/close/neg-risk metadata.
- `log_orderbooks.py` now stores best prices, sizes, spread, raw YES/NO order books, hours-to-close, and timing labels.
- Verification pass on the June 1, 2026 pilot found 187 markets across 17 city/date events, with zero missing core market fields.
- A complete live order-book snapshot captured all 187 markets in one timestamp.

Still open:

- Build source-specific settlement adapters for WU, HKO, NOAA, and Polymarket/UMA final outcomes.
- Build final settlement reconciliation and bucket-resolution logic.
- Add ICON, GEM, Météo-France, and direct ECMWF collection.
- Run the scheduled loop continuously long enough to produce a real multi-day backtest sample.

## Blocking Findings

### 1. Live rules do not match the current station assumptions for every configured city

I checked current Polymarket Gamma event payloads for the configured weather cities.

Findings:

- Most configured non-US markets resolve from Wunderground daily summaries.
- `NYC` resolves in **degrees Fahrenheit**, not Celsius.
- `Miami` resolves in **degrees Fahrenheit**, not Celsius.
- `Hong Kong` resolves from the **Hong Kong Observatory**, not Wunderground/ZBAA.
- `Moscow` resolves from **NOAA Vnukovo `UUWW`**, not Helsinki `EFHK`.

This means the current plan/config would mis-log or misinterpret several markets.

Examples from live rules:

- Seoul: Wunderground `RKSI`, whole degrees Celsius.
- NYC: Wunderground `KLGA`, whole degrees Fahrenheit.
- Miami: Wunderground `KMIA`, whole degrees Fahrenheit.
- Hong Kong: Hong Kong Observatory “Absolute Daily Max (deg. C)”, one decimal place.
- Moscow: NOAA `UUWW`, whole degrees Celsius.

Required fix:

`data/city_stations.json` needs per-market resolution metadata:

- `market_unit`: `C` or `F`
- `bucket_unit`: `C` or `F`
- `station`
- `resolution_source_name`
- `resolution_source_url`
- `resolution_precision`
- `resolution_rounding_rule`
- `resolution_station_name`
- `rules_text`
- `rules_checked_at_utc`

Do not assume the station table is correct until refreshed from live rules.

### 2. Current scope is not all Polymarket weather

The active Polymarket weather page currently exposes many more city slugs than this repo tracks.

Live page showed 51 unique city slugs, including:

`atlanta`, `austin`, `buenos-aires`, `busan`, `cape-town`, `chicago`, `dallas`, `denver`, `helsinki`, `houston`, `istanbul`, `jeddah`, `karachi`, `kuala-lumpur`, `los-angeles`, `manila`, `mexico-city`, `milan`, `panama-city`, `san-francisco`, `seattle`, `taipei`, `tel-aviv`, `toronto`, `warsaw`, `wuhan`, `zhengzhou`, and more.

The repo currently tracks only 17 city markets.

A broader live sweep found 163 current weather event slugs on the weather page. Source mix by first market in each event:

- 146 Wunderground events
- 6 Hong Kong Observatory events
- 11 NOAA events

That confirms the logger must be source-type-aware. Wunderground covers most markets, but not all.

Required decision:

- Either explicitly declare the repo scope as “selected 17-city pilot”, or
- Expand metadata collection to every active Polymarket weather city.

Do not describe this as full Polymarket weather coverage unless the city list is expanded.

### 3. The planned single schema owner does not exist

`PLAN.md` says `scripts/init_db.py` is the single schema owner.

Actual repo state:

- `scripts/init_db.py` does not exist.
- `discover_markets.py` defines one partial schema.
- `fetch_weather.py` defines another partial schema.
- Existing `weather.db` only has the old weather/forecast tables.

Current `weather.db` is missing:

- `weather_markets`
- `ob_snapshots`
- `alerts`
- new `wx_observations` fields
- settlement fields
- rules fields

Required fix:

Create `scripts/init_db.py` or `schema.sql`, then make every script call it. Stop defining table layouts separately inside operational scripts.

### 4. METAR observations currently store fetch time as observation time

The plan correctly requires:

- `observed_utc`
- `fetched_utc`
- `local_date`

Actual code stores:

- `ts_utc`

That `ts_utc` is the system fetch time, not the METAR report time.

Why this breaks analysis:

- Latency tests cannot be run.
- “Already priced in” tests cannot be trusted.
- Daily high timing is shifted by up to a polling interval.
- Market repricing after observation cannot be measured correctly.

Required fix:

Parse METAR `reportTime` / `obsTime` into `observed_utc`. Store system retrieval time separately in `fetched_utc`.

### 5. Daily highs currently use UTC day boundaries

The updated plan correctly requires station-local day boundaries.

Actual code still computes:

```sql
WHERE station=? AND source='metar' AND DATE(ts_utc)=?
```

This is wrong for almost every station.

Required fix:

For every observation:

1. Convert `observed_utc` to station timezone.
2. Store `local_date`.
3. Compute `daily_high_c` by `(station, local_date)`.

Never use `dt.date.today()` or `DATE(ts_utc)` for station-day logic.

### 6. `data/city_stations.json` is not the single source of truth yet

The plan says all station metadata lives in `data/city_stations.json`.

Actual file currently has:

- city
- slug
- station
- country
- wu_path
- anomaly flags

It is missing:

- latitude
- longitude
- timezone
- unit
- resolution source
- settlement precision
- source-specific station code

Meanwhile `fetch_weather.py` hardcodes station metadata separately.

Required fix:

Move lat/lon/timezone/unit/resolution metadata into `data/city_stations.json` and make all scripts load it.

### 7. US markets are bucket ranges in Fahrenheit, not exact Celsius buckets

The current parser handles:

- `be X°C or below`
- `be X°C or higher`
- `be X°C on`

Live NYC/Miami questions look like:

- `67°F or below`
- `between 68-69°F`
- `between 70-71°F`

Required fix:

Market buckets need a richer representation:

- `bucket_type`: `exact`, `range`, `above_eq`, `below_eq`
- `lower_temp`
- `upper_temp`
- `unit`
- `resolution_precision`

`temp_c` alone is insufficient.

### 8. `discover_markets.py` is still HTML-regex based and does not store rules

The updated plan requires:

- `rules_text`
- `rules_source`
- `resolution_source_url`
- `first_seen_utc`
- `close_time_utc`
- `settlement_rounding_rule`

Actual `discover_markets.py`:

- scrapes Polymarket HTML
- regexes condition IDs and questions
- pairs them by index
- fetches CLOB tokens separately
- does not store rules/description/resolution source
- does not use Gamma event metadata

Required fix:

Use Gamma event-by-slug API as primary discovery source. Store raw description/rules fields. Use `markets[].clobTokenIds`, `markets[].conditionId`, `markets[].question`, `markets[].endDate`, `markets[].resolutionSource`, and `markets[].description`.

### 9. Orderbook snapshots do not store size/depth

The tests require liquidity/executability analysis.

Current `ob_snapshots` stores only:

- best bid
- best ask
- derived mid

It does not store:

- bid size
- ask size
- depth at target price
- full ladder
- spread
- stale-book status

Required fix:

At minimum add:

- `yes_bid_size`
- `yes_ask_size`
- `no_bid_size`
- `no_ask_size`
- `spread`
- `raw_book_json`

Without this, the Liquidity / Executability Filter test cannot be run honestly.

### 10. Forecast collection does not request station-local timezone

The plan correctly says open-meteo must be called with station timezone.

Actual code does not pass `timezone`.

Required fix:

Pass `timezone=<station timezone>` on every open-meteo request so `forecast_date` matches `settlement_date` in station-local calendar time.

### 11. Forecast model run timestamps are estimates

The plan acknowledges this, but the schema does not distinguish actual vs estimated run time.

Required fix:

Add:

- `model_run_utc`
- `model_run_is_estimated`
- optionally `source_metadata_json`

For open-meteo models, set `model_run_is_estimated = 1` unless actual metadata is available.

### 12. Scheduler still drifts from model cadence

The plan says scheduling should compute next source run plus offset after each job.

Actual `fetch_weather.py` reschedules models with a naive interval derived from run-hour lists.

Required fix:

Use the same “next run plus configured delay” calculation after every completed fetch.

### 13. No settlement-source fetchers exist

The plan requires the settlement proxy layer:

- Wunderground daily summary
- Hong Kong Observatory daily extract
- NOAA WRH timeseries
- final Polymarket/UMA outcome

Actual repo has none of these fetchers.

Required fix:

Add source-specific settlement fetchers and a normalized `settlement_observations` table.

Suggested table:

```sql
CREATE TABLE settlement_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT,
    city TEXT NOT NULL,
    station TEXT,
    source_name TEXT NOT NULL,
    source_url TEXT,
    local_date TEXT NOT NULL,
    value REAL,
    unit TEXT,
    precision TEXT,
    fetched_utc TEXT NOT NULL,
    raw_payload_json TEXT
);
```

### 14. No final Polymarket/UMA outcome collection exists

The plan’s “final state” layer is right, but there is no code yet to collect final outcomes.

Required fix:

Store final resolved outcome per condition:

- `condition_id`
- `resolved_outcome`
- `resolved_value`
- `resolution_status`
- `resolved_at_utc`
- `final_source_payload_json`

This is needed for all strategy pass/fail metrics.

## Source-Specific Rule Findings For Configured Cities

Observed from Gamma event payloads for `June 1, 2026` events.

| City | Live rule source | Unit / precision | Config issue |
|------|------------------|------------------|--------------|
| Seoul | Wunderground `RKSI` | Celsius, whole degrees | OK conceptually |
| Hong Kong | Hong Kong Observatory Daily Extract | Celsius, one decimal | Config says `ZBAA` / WU-style; wrong |
| London | Wunderground `EGLC` | Celsius, whole degrees | OK conceptually |
| Tokyo | Wunderground `RJTT` | Celsius, whole degrees | OK conceptually |
| NYC | Wunderground `KLGA` | Fahrenheit, whole degrees, ranged buckets | Parser/schema wrong |
| Paris | Wunderground `LFPB` | Celsius, whole degrees | OK conceptually |
| Beijing | Wunderground `ZBAA` | Celsius, whole degrees | OK conceptually |
| Miami | Wunderground `KMIA` | Fahrenheit, whole degrees, ranged buckets | Parser/schema wrong |
| Singapore | Wunderground `WSSS` | Celsius, whole degrees | OK conceptually |
| Madrid | Wunderground `LEMD` | Celsius, whole degrees | OK conceptually |
| Moscow | NOAA `UUWW` Vnukovo | Celsius, whole degrees | Config says `EFHK`; wrong |
| Munich | Wunderground `EDDM` | Celsius, whole degrees | OK conceptually |
| Amsterdam | Wunderground `EHAM` | Celsius, whole degrees | OK conceptually |
| Ankara | Wunderground `LTAC` | Celsius, whole degrees | URL encoding differs; verify path |
| Wellington | Wunderground `NZWN` | Celsius, whole degrees | OK conceptually |
| Shenzhen | Wunderground `ZGSZ` | Celsius, whole degrees | OK conceptually |
| Guangzhou | Wunderground `ZGGG` | Celsius, whole degrees | OK conceptually |

## Internal Consistency Requirements

Before this repo can produce clean analysis, each record must preserve these invariants.

### Market invariant

For every `weather_markets` row:

- `condition_id` is unique.
- `event_slug` is stored.
- `market_slug` is stored.
- `question` is stored exactly.
- `bucket_type`, `lower_temp`, `upper_temp`, and `unit` are parsed from `question`.
- `rules_text` is stored from Gamma `description` or rules field.
- `rules_source` and `resolution_source_url` are parsed or stored from Gamma.
- `first_seen_utc` is system discovery time.
- `close_time_utc` is Gamma `endDate`.
- `settlement_date` is parsed from the market question/event slug, not guessed from server date.

### Observation invariant

For every METAR row:

- `observed_utc` comes from the source report time.
- `fetched_utc` comes from system retrieval time.
- `local_date` comes from `observed_utc` converted through station timezone.
- raw observation payload is stored or recoverable.
- duplicate polls do not duplicate the same station/report.

### Forecast invariant

For every model forecast:

- `fetched_utc` is system retrieval time.
- `forecast_date` is station-local date.
- `model_run_utc` is actual if available, estimated if not.
- estimate status is explicit.
- duplicate fetches do not duplicate the same station/model/run/date.

### Orderbook invariant

For every orderbook snapshot:

- `ts_utc` is system retrieval time.
- bid/ask prices and sizes are stored.
- spread is derivable.
- raw book is preserved.
- `hours_to_close` is computed from `close_time_utc`.
- snapshots still persist if label assignment fails.

### Settlement invariant

For every final settlement:

- proxy settlement and final settlement are separate.
- unit conversion is explicit.
- precision/rounding is explicit.
- source payload is preserved.
- outcome resolution is computed with bucket-aware logic.

## Concrete Fix Order

1. Create `scripts/init_db.py` as the only schema owner.
2. Replace existing `weather.db` schema or migrate it to the planned schema.
3. Expand `data/city_stations.json` with lat, lon, timezone, unit, and live-rule-derived resolution metadata.
4. Refactor `fetch_weather.py` to load metadata from JSON.
5. Fix METAR inserts: `observed_utc`, `fetched_utc`, `local_date`, unique key, raw payload.
6. Fix open-meteo calls to pass station timezone.
7. Replace `discover_markets.py` HTML scraping with Gamma event-by-slug discovery.
8. Store rules/source/description/endDate/token IDs from Gamma.
9. Add bucket parser for Celsius, Fahrenheit, exact, range, above, below.
10. Expand orderbook snapshots to include sizes and raw book JSON.
11. Add settlement-source fetchers for WU, HKO, NOAA WRH, and final Polymarket/UMA outcome.
12. Add audit tests that fail if any script uses `dt.date.today()` for station-local logic.

## Can We Have Perfect Internal Consistency?

Yes, but not with the current implementation.

The plan is now close to the right model. The code must be brought up to the plan, and the metadata must be regenerated from live Polymarket rules.

The minimum bar for consistency is:

- one schema owner
- source-specific rules per market
- explicit units and bucket ranges
- local-date observations
- separate observed/fetched timestamps
- raw payload preservation
- settlement-source-specific confirmation
- final Polymarket outcome reconciliation

Until those exist, the repo is a good research sketch, not a reliable data logger.
