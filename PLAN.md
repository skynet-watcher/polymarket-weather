# Polymarket Weather Market Logger — Project Plan

## What This Is

This repo starts as a **selected weather-market pilot**, not full Polymarket weather coverage.
The initial pilot covers 17 configured city markets, but live Polymarket weather currently
contains many more city/event slugs. Discovery must be rules-first so the pilot can expand
without changing the core schema.

Each day: "Will the highest temperature in [City] be X?" markets settle at the day's
official high temperature from the source named in each market's resolution rules.
Markets are not all Celsius exact buckets: some are Fahrenheit, some are ranges, some
are `or below` / `or above`, and some use non-Wunderground sources.

This project collects three categories of data:
1. **METAR observations** — live airport instrument readings used as a fast intraday proxy
2. **Forecast data** — 6 independent sources predicting the day's high at each airport, 48h ahead
3. **Order book snapshots** — YES/NO prices on every temperature bucket, every 2 minutes

Goal: Find gaps between what forecasts say, what the thermometer reads, and what the market prices.

---

## Pilot Settlement Sources

These are starting assumptions only. They must be refreshed from live Polymarket/Gamma
rules during discovery. `data/city_stations.json` is a cached/enriched config, not the
authority. The authority is the per-market rules text and resolution source.

| City        | Fast Proxy Station | Expected Source / Station      | Timezone         | Notes                         |
|-------------|--------------------|--------------------------------|------------------|-------------------------------|
| Seoul       | RKSI               | Incheon International          | Asia/Seoul       |                               |
| Hong Kong   | HKO/HKO            | Hong Kong Observatory          | Asia/Hong_Kong   | Rules source is HKO Daily Extract, not WU/ZBAA |
| London      | EGLC               | London City Airport            | Europe/London    |                               |
| Tokyo       | RJTT               | Haneda Airport                 | Asia/Tokyo       |                               |
| NYC         | KLGA               | LaGuardia Airport              | America/New_York | Fahrenheit range buckets       |
| Paris       | LFPB               | Le Bourget Airport             | Europe/Paris     |                               |
| Beijing     | ZBAA               | Beijing Capital International  | Asia/Shanghai    |                               |
| Miami       | KMIA               | Miami International Airport    | America/New_York | Fahrenheit range buckets       |
| Singapore   | WSSS               | Changi Airport                 | Asia/Singapore   |                               |
| Madrid      | LEMD               | Barajas Airport                | Europe/Madrid    |                               |
| Moscow      | UUWW               | NOAA Vnukovo International     | Europe/Moscow    | Rules source is NOAA, not EFHK |
| Munich      | EDDM               | Munich Airport                 | Europe/Berlin    |                               |
| Amsterdam   | EHAM               | Schiphol Airport               | Europe/Amsterdam |                               |
| Ankara      | LTAC               | Ankara Esenboga Airport        | Europe/Istanbul  |                               |
| Wellington  | NZWN               | Wellington Airport             | Pacific/Auckland |                               |
| Shenzhen    | ZGSZ               | Shenzhen Bao'an Airport        | Asia/Shanghai    |                               |
| Guangzhou   | ZGGG               | Guangzhou Baiyun Airport       | Asia/Shanghai    |                               |

All enriched metadata lives in `data/city_stations.json` after discovery:

- city and event slug
- fast proxy station
- resolution source name and URL
- source-specific station code
- lat/lon/timezone
- bucket unit and settlement unit
- precision and rounding rule
- anomaly/source notes

During discovery, live Gamma/Polymarket rules overwrite stale config assumptions.

---

## Market Typology — Peak vs Trading Close

The relationship between the afternoon temperature peak and trading close is different
for every city and fundamentally changes how each market works and what signals matter.

### Type A — Post-peak settlement (Asian cities)
**Cities**: Seoul, Tokyo, Beijing, Shenzhen, Guangzhou, Singapore
**Typical peak**: 14:00–15:00 local = 05:00–07:00 UTC
**Trading closes**: 20:00–21:00 local = 12:00 UTC — 5–7 hours after peak

By the time trading closes, the afternoon high is already known and locked.
The market should converge to the correct bucket during the afternoon as
METAR readings rise toward and past the peak. Any pricing lag after a new
METAR high is a live signal.

**Active signal window**: 04:00–10:00 UTC (afternoon local across Asian stations)
**Key alert**: obs_mismatch — METAR daily_high_c just exceeded bucket X; market
still pricing bucket X above 5¢

### Type B — At-peak settlement (European cities)
**Cities**: London, Paris, Munich, Amsterdam, Madrid, Ankara, Moscow (UUWW/Vnukovo, NOAA source)
**Typical peak**: 13:00–16:00 local = 12:00–14:00 UTC
**Trading closes**: 13:00–14:00 local = 12:00 UTC — right at or just before peak

Trading closes exactly as temperatures are peaking. Market is most price-sensitive
and volatile in the last 1–2 hours of trading. A late-morning METAR reading above
the expected high is a last-minute signal before trading locks.

**Active signal window**: 08:00–12:00 UTC (late morning, approaching peak)
**Key alert**: obs_mismatch in final 2 hours of trading; prices still adjusting

### Type C — Pre-peak settlement (NYC, Miami, Wellington)
**Cities**: NYC, Miami, Wellington
**Typical peak**: 14:00–15:00 local
**Trading closes**: 
- NYC/Miami: 08:00 EDT = 12:00 UTC — **7 hours before peak**
- Wellington: midnight NZST = 12:00 UTC — **14 hours before peak**

Trading locks before the hottest part of the day has happened.
Settlement uses the full local calendar day high including the afternoon that occurs
after trading has closed. METAR readings after 12:00 UTC are irrelevant to trading
for these cities — nobody can act on them.

These markets are **purely forecast-based**: traders must predict the final daily
high without any ability to react to observed temperatures.

**Active signal window**: 06:00–12:00 UTC (morning observations before close)
**Key alert**: forecast divergence before market close (cannot react to afternoon METAR)
**No obs_mismatch alerts after 12:00 UTC** — trading is already closed

### Implications for data collection and alerts

```python
# Only fire obs_mismatch alerts when trading is open (before close_time_utc)
# AND we are in or past the peak window for this city type

TYPE_A_STATIONS = {'RKSI', 'RJTT', 'ZBAA', 'ZGSZ', 'ZGGG', 'WSSS', 'VHHH'}  # peak 05-07 UTC
#                                                                         ^^^^
#   HK proxy: VHHH (HK Int'l Airport ICAO) — fast proxy for HKO settlement source
TYPE_B_STATIONS = {'EGLC', 'LFPB', 'EDDM', 'EHAM', 'LEMD', 'LTAC', 'UUWW'}  # peak 12-14 UTC
TYPE_C_STATIONS = {'KLGA', 'KMIA', 'NZWN'}  # peak after trading closes

# For Type C: obs_mismatch is irrelevant after close_time_utc
# Forecast divergence (Q1) is the only signal before close
```

---

## Settlement Model — Three Layers

Do not treat any single data source as the definitive settlement truth. Three distinct layers exist:

| Layer | Source | Role |
|-------|--------|------|
| **Fast proxy** | METAR (aviationweather.gov) | Real-time intraday signal; drives the trade clock |
| **Settlement proxy** | Source named in market rules (e.g. WU daily summary for that station) | Closest to how Polymarket resolves; varies per market |
| **Final state** | Polymarket / UMA resolution outcome | Ground truth for research; only available after settlement |

The exact resolution source is stored per market in `rules_source` and `resolution_source_url`.
Do not hard-code "Weather Underground" as universal — individual markets may specify NOAA, WU,
a local met service, or another station.

`settle_markets.py` separates proxy settlement (source-adapted daily high for the full local
calendar day, written after local midnight) from final settlement (Polymarket/UMA resolved
outcome), storing both. `settlement_value_proxy` comes from the named source adapter
(WU/HKO/NOAA), not raw METAR — METAR is only the fast intraday signal.

### Source adapters required

The system must support at least these settlement-source adapters:

- `wunderground_daily`: Wunderground daily history table for station daily high
- `hong_kong_observatory_daily`: HKO Daily Extract, Absolute Daily Max
- `noaa_wrh_timeseries`: NOAA WRH time series, highest value under `Temp`
- `polymarket_final`: final Polymarket/UMA resolved outcome

Every adapter stores normalized values plus raw payloads. If a market names an unknown
source, discovery should still store the market but mark `resolution_source_type='unknown'`
and prevent strategy conclusions until a source adapter exists.

### Rules-first discovery

Market discovery is rules-first:

1. Discover active weather event slugs.
2. Fetch Gamma event payloads by slug.
3. Store every market's `question`, `description`/rules text, `resolutionSource`,
   `endDate`, `conditionId`, token IDs, and raw market JSON.
4. Parse source, units, bucket type/range, precision, and rounding from the rules/question.
5. Use `data/city_stations.json` only as enrichment/cache.

No analysis should depend on manually maintained station assumptions when live rules disagree.

---

## Data Sources

### Ground Truth — METAR (Fast Proxy)
**Source**: `aviationweather.gov/api/data/metar`
**What**: Live airport instrument readings. Used as a fast intraday proxy — the same
instrument family Weather Underground draws from, but METAR is real-time while WU publishes
a final daily summary that may differ (rounding, QA, data cutoff time).
**Frequency**: Every 30 minutes (stations update ~hourly; polling at 30 min catches every reading)
**Coverage**: All 17 configured settlement stations. HK proxy: VHHH (Hong Kong Int'l Airport
ICAO, ~13km from HKO Observatory). HKO does not publish METAR; VHHH is the nearest
aviation-grade instrument. Treat HK METAR readings as approximate — HKO settlement values
will differ. All 17 in a single batch API call.
**Key fields**:
- `temp` → `temp_c`: current 2m temperature
- `reportTime` → `observed_utc`: when the reading was taken at the station (stored separately from `fetched_utc`)
- Running `daily_high_c` computed by `MAX(temp_c)` grouped by `(station, local_date)` — using the
  station's local timezone, not UTC, to avoid midnight boundary errors for non-UTC cities

---

### Forecast Sources

Six independent sources predicting the daily high at each airport's exact lat/lon, 48h ahead.
Fetched at each source's actual update cadence.

#### 1. TAF (Terminal Aerodrome Forecast)
**Source**: `aviationweather.gov/api/data/taf`
**What**: Official aviation forecast issued by the national met agency for that exact airport.
Where TX/TN temperature fields are present, this is the highest-quality forecast available
because it comes from the same institution as the METAR.
**Frequency**: Every 6h (issued at 00, 06, 12, 18 UTC). Fetch 30 min after issuance.
**Coverage**: All 17 configured stations fetched; only 5 include TX/TN temperature (RKSI, ZBAA, ZGGG, ZGSZ, LEMD).
**Horizon**: 30 hours (TAF format limitation — not full 48h)
**Timestamps stored**: `issued_utc`, `valid_from_utc`, `valid_to_utc`, `fetched_utc`

#### 2. ECMWF IFS — Direct from ECMWF Open Data
**Source**: `data.ecmwf.int` via `ecmwf-opendata` Python library
**What**: European Centre for Medium-Range Weather Forecasts IFS model. Gold standard globally.
0.25° resolution (~28km grid — all airports within 14km of a grid point).
**Why direct, not via open-meteo**: open-meteo mirrors ECMWF but adds 1–3h ingestion delay
on top of ECMWF's natural ~4–5h post-run time, yielding 6–8h total. Direct gives ~4–5h.
**Frequency**: 2× per day (00z and 12z runs). Fetch ~5h after run time (05:00 and 17:00 UTC).
**Coverage**: Global — all 17 configured airports confirmed.
**Format**: GRIB2, parsed with `cfgrib`. Extract 2m temp (`2t`) at steps +24h and +48h,
then compute daily max across 3-hourly steps within each airport's local calendar day.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 3. GFS (Global Forecast System)
**Source**: `api.open-meteo.com` — model `gfs_seamless`
**What**: NOAA's primary global model.
**Frequency**: 4× per day. Fetch ~4h after each 00/06/12/18 UTC run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 4. ICON (Icosahedral Nonhydrostatic)
**Source**: `api.open-meteo.com` — model `icon_seamless`
**What**: DWD (German Weather Service) global model. Strong over Europe, solid globally.
**Frequency**: 4× per day. Fetch ~3h after run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 5. Météo-France ARPEGE
**Source**: `api.open-meteo.com` — model `meteofrance_seamless`
**What**: French national model. Good for European and tropical regions.
**Frequency**: 4× per day. Fetch ~3.5h after run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 6. GEM (Global Environmental Multiscale)
**Source**: `api.open-meteo.com` — model `gem_seamless`
**What**: Environment and Climate Change Canada's model.
**Frequency**: 4× per day. Fetch ~3.5h after run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

---

## Three Analytical Questions — Data Requirements

### Q1: What were the 5 models predicting 48h out at market open?

**What "market open" means**: `discover_markets.py` records `first_seen_utc` the moment it
first finds a market. This is used as a proxy for market open. It is *not* necessarily
the actual Polymarket creation timestamp — only use `first_seen_utc` for this purpose,
and only upgrade to a different field name if a true creation timestamp is available from
Polymarket/Gamma metadata.

**The query**:
```sql
-- Most recent forecast from each model that was available when market was first seen
SELECT mf.model, mf.high_c, mf.low_c, mf.fetched_utc, mf.model_run_utc
FROM model_forecasts mf
JOIN weather_markets wm ON mf.station = wm.station
                        AND mf.forecast_date = wm.settlement_date
WHERE wm.condition_id = ?
  AND mf.fetched_utc <= wm.first_seen_utc
GROUP BY mf.model
HAVING mf.fetched_utc = MAX(mf.fetched_utc)
```

**Requires**:
- `weather_markets.first_seen_utc` — set by `discover_markets.py` on first discovery
- `weather_markets.settlement_date`
- `model_forecasts.fetched_utc` — the moment this system acquired the forecast

---

### Q2: What was the actual temperature resolution?

**What "resolution" means**: Two separate answers exist:
- **Proxy resolution**: source-adapted daily high from the market's named resolution source,
  or fast proxy data when the final source is not yet available
- **Final resolution**: the Polymarket/UMA outcome (authoritative, available after settlement confirms)

**The query**:
```sql
SELECT wm.condition_id, wm.city, wm.settlement_date,
       wm.settlement_value_proxy,    -- source-adapted daily high
       wm.settlement_value_final,    -- Polymarket/UMA resolved outcome (if available)
       wm.settlement_source,
       wm.settled_at_utc,
       wm.bucket_type, wm.lower_temp, wm.upper_temp, wm.bucket_unit,
       CASE
         WHEN wm.bucket_type = 'exact'
              AND wm.settlement_value_proxy = wm.lower_temp THEN 'YES'
         WHEN wm.bucket_type = 'range'
              AND wm.settlement_value_proxy >= wm.lower_temp
              AND wm.settlement_value_proxy <= wm.upper_temp THEN 'YES'
              -- 'range' covers NYC/Miami Fahrenheit buckets (e.g. 68–69°F)
              -- settlement_value_proxy must already be in bucket_unit (convert if needed)
         WHEN wm.bucket_type = 'above_eq'
              AND wm.settlement_value_proxy >= wm.lower_temp THEN 'YES'
         WHEN wm.bucket_type = 'below_eq'
              AND wm.settlement_value_proxy <= wm.upper_temp THEN 'YES'
         ELSE 'NO'
       END as resolved_proxy
FROM weather_markets wm
WHERE wm.settlement_date = ?
ORDER BY wm.city, wm.lower_temp
```

**Requires**:
- `weather_markets.settlement_value_proxy` — source-adapted daily high written by `settle_markets.py`
- `weather_markets.settlement_value_final` — Polymarket/UMA outcome, written when available
- `weather_markets.settlement_unit` — unit of the normalized settlement value
- `weather_markets.bucket_unit` — unit used by bucket thresholds
- `weather_markets.settlement_source` — which source was used
- `weather_markets.settled_at_utc`

---

### Q3: Order book prices at open and at standard intervals to close?

**Standard intervals**: T-48h (open), T-24h, T-12h, T-6h, T-3h, T-1h, T-30min, T-close

**The query**:
```sql
SELECT ob.snapshot_label, ob.ts_utc, ob.hours_to_close,
       wm.lower_temp, wm.upper_temp, wm.bucket_type, wm.bucket_unit,
       ob.yes_bid, ob.yes_ask, ob.yes_mid, ob.yes_bid_size, ob.yes_ask_size, ob.spread
FROM ob_snapshots ob
JOIN weather_markets wm ON ob.condition_id = wm.condition_id
WHERE wm.city = ? AND wm.settlement_date = ?
ORDER BY ob.ts_utc, wm.lower_temp
```

**Important**: `snapshot_label` is nullable. Every 2-minute raw snapshot is stored regardless.
Labels are assigned when a snapshot falls within 5 minutes of a standard threshold.
Analysis can select nearest snapshots even if a label window was missed.

**Requires**:
- `ob_snapshots.snapshot_label` — nullable; assigned at capture time near standard thresholds
- `ob_snapshots.hours_to_close` — computed at capture time from `close_time_utc`
- `weather_markets.first_seen_utc` and `close_time_utc` — to compute label offsets

---

## Fetch Schedule

| Source       | Runs/day | Fetch times (UTC)           | Delay after run | Horizon |
|--------------|----------|-----------------------------|-----------------|---------|
| METAR        | 48       | Every :00 and :30           | Real-time       | n/a     |
| TAF          | 4        | 00:30, 06:30, 12:30, 18:30  | 30 min          | 30h     |
| ECMWF direct | 2        | ~05:00, ~17:00              | ~5h             | 48h+    |
| GFS          | 4        | ~04:00, 10:00, 16:00, 22:00 | ~4h             | 48h     |
| ICON         | 4        | ~03:00, 09:00, 15:00, 21:00 | ~3h             | 48h     |
| Météo-France | 4        | ~03:30, 09:30, 15:30, 21:30 | ~3.5h           | 48h     |
| GEM          | 4        | ~03:30, 09:30, 15:30, 21:30 | ~3.5h           | 48h     |

Scheduler computes next intended fetch time from model run times and offsets after each job
completes — not a naive interval — to prevent drift from actual model cadence.

---

## Retry Policy

Every fetch is wrapped in `with_retry()`:
- **3 attempts** maximum per fetch cycle
- **180 second wait** between attempts
- Every attempt logged to `fetch_log` with exact UTC timestamp, duration in ms,
  status (`success` / `retry` / `failed`), records saved, and error message

---

## Database Schema

Single schema owner: `scripts/init_db.py`. Every script calls this before touching the DB.
No script defines its own table layout independently.

```sql
-- Live airport instrument readings (fast intraday proxy — NOT final settlement truth)
CREATE TABLE wx_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    observed_utc    TEXT,                  -- when the reading was taken at the station
    fetched_utc     TEXT NOT NULL,         -- when this system retrieved it
    local_date      TEXT NOT NULL,         -- YYYY-MM-DD in station's local timezone
    local_hour      INTEGER,               -- 0-23 hour in station's local timezone
                                           -- used to filter to peak window (Type A: 14-16,
                                           -- Type B: 13-16, Type C: pre-close only)
    temp_c          REAL,
    daily_high_c    REAL,                  -- MAX(temp_c) for full local calendar day
                                           -- (midnight to midnight local — NOT capped at
                                           -- trading close; settlement = full day high)
    is_in_peak_window INTEGER DEFAULT 0,  -- 1 if this reading is during the city's
                                           -- typical afternoon peak hours (Type A/B/C aware)
    source          TEXT DEFAULT 'metar',
    raw_payload_json TEXT                  -- raw source observation for audit/replay
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_wx_obs_unique
    ON wx_observations(station, source, observed_utc);
CREATE INDEX IF NOT EXISTS ix_wx_station_date ON wx_observations(station, local_date);

-- TAF aviation forecasts (TX/TN where available; only 5 of 16 stations include temp)
CREATE TABLE taf_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    issued_utc      TEXT NOT NULL,         -- when the TAF was issued
    valid_from_utc  TEXT NOT NULL,
    valid_to_utc    TEXT NOT NULL,
    fetched_utc     TEXT NOT NULL,         -- when this system retrieved it
    tx_c            REAL,                  -- forecast daily max
    tx_time_utc     TEXT,                  -- when max is expected
    tn_c            REAL,                  -- forecast daily min
    tn_time_utc     TEXT,
    raw_taf         TEXT,
    raw_payload_json TEXT,
    UNIQUE(station, issued_utc)
);

-- NWP model forecasts (GFS, ECMWF direct, ICON, Météo-France, GEM)
CREATE TABLE model_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    model           TEXT NOT NULL,         -- ecmwf_direct | gfs_seamless | icon_seamless | etc.
    model_run_utc   TEXT NOT NULL,         -- which model run this data came from
    fetched_utc     TEXT NOT NULL,         -- when this system acquired it
    forecast_date   TEXT NOT NULL,         -- YYYY-MM-DD in station's local timezone
    horizon_hours   INTEGER,               -- 24 or 48
    high_c          REAL,
    low_c           REAL,
    lat             REAL,
    lon             REAL,
    model_run_is_estimated INTEGER DEFAULT 1,
    raw_payload_json TEXT,
    UNIQUE(station, model, model_run_utc, forecast_date)
);

-- Polymarket weather markets (refreshed daily by rules-first discovery)
CREATE TABLE weather_markets (
    condition_id            TEXT PRIMARY KEY,
    event_slug              TEXT,
    market_slug             TEXT,
    city                    TEXT NOT NULL,
    station                 TEXT NOT NULL,
    settlement_date         TEXT NOT NULL,   -- YYYY-MM-DD in station's local timezone
    bucket_type             TEXT NOT NULL,   -- 'exact' | 'range' | 'above_eq' | 'below_eq'
    lower_temp              REAL,
    upper_temp              REAL,
    bucket_unit             TEXT NOT NULL,   -- 'C' | 'F'
    settlement_unit         TEXT NOT NULL,   -- 'C' | 'F'
    yes_token_id            TEXT,
    no_token_id             TEXT,
    question                TEXT,
    rules_text              TEXT,            -- raw resolution rules from Polymarket
    rules_source            TEXT,            -- named source in rules (e.g. 'Weather Underground')
    resolution_source_type  TEXT,            -- wunderground_daily | hong_kong_observatory_daily |
                                             -- noaa_wrh_timeseries | unknown
    resolution_source_url   TEXT,            -- exact URL named in rules
    raw_market_json         TEXT,            -- raw Gamma market payload for audit/replay
    first_seen_utc          TEXT,            -- when discover_markets.py first found this market
    close_time_utc          TEXT,            -- settlement UTC time from Polymarket end_date
    settlement_value_proxy  REAL,            -- Q2: normalized proxy source value in settlement_unit
    settlement_value_final  REAL,            -- Q2: final Polymarket/UMA resolved value/outcome
    settlement_source       TEXT,            -- which source was used for proxy settlement
    settlement_rounding_rule TEXT,           -- Q2: how Polymarket rounds fractional temps
                                             --     'round' | 'floor' | 'ceiling' | unknown
                                             --     source reports 21.7C; bucket is integer 22C
                                             --     must apply same rule or proxy match fails
    -- settlement_window_hours REMOVED — was based on wrong model.
    -- Trading close (12:00 UTC) ≠ temperature measurement end.
    -- Resolution = full source-specific local calendar day per market rules.
    resolution_status       TEXT,            -- 'proxy_only' | 'confirmed' | 'disputed'
    settled_at_utc          TEXT,            -- when settle_markets.py ran for this market
    active                  INTEGER DEFAULT 1,
    created_at              TEXT DEFAULT (datetime('now'))
);

-- Orderbook snapshots every 2 minutes (all snapshots kept; label is nullable)
CREATE TABLE ob_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    yes_bid         REAL,
    yes_ask         REAL,
    yes_bid_size    REAL,
    yes_ask_size    REAL,
    no_bid          REAL,
    no_ask          REAL,
    no_bid_size     REAL,
    no_ask_size     REAL,
    yes_mid         REAL,
    spread          REAL,
    raw_book_json   TEXT,                   -- raw CLOB book for depth/executability analysis
    hours_to_close  REAL,                   -- computed at capture time
    snapshot_label  TEXT                    -- nullable: 'open'|'T-24h'|'T-12h'|'T-6h'|
                                            --          'T-3h'|'T-1h'|'T-30m'|'close'
);
CREATE INDEX IF NOT EXISTS ix_ob_cid_ts ON ob_snapshots(condition_id, ts_utc);

-- Complete fetch audit trail
CREATE TABLE fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    source      TEXT NOT NULL,              -- metar | taf | ecmwf_direct | gfs_seamless | etc.
    station     TEXT,
    attempt     INTEGER NOT NULL,           -- 1, 2, or 3
    status      TEXT NOT NULL,              -- success | retry | failed
    n_records   INTEGER DEFAULT 0,
    error       TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fetchlog_ts     ON fetch_log(ts_utc);
CREATE INDEX IF NOT EXISTS ix_fetchlog_source ON fetch_log(source, ts_utc);

-- Flagged opportunities
CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    city        TEXT NOT NULL,
    alert_type  TEXT NOT NULL,              -- neg_risk_gap | obs_mismatch | forecast_divergence | convergence
    detail_json TEXT
);

-- Normalized settlement-source observations (WU/HKO/NOAA/etc.)
CREATE TABLE settlement_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT,
    city            TEXT NOT NULL,
    station         TEXT,
    source_name     TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    source_url      TEXT,
    local_date      TEXT NOT NULL,
    value           REAL,
    unit            TEXT,
    precision       TEXT,
    fetched_utc     TEXT NOT NULL,
    raw_payload_json TEXT
);

-- Final market resolution from Polymarket/UMA
-- One row per condition_id (individual YES/NO market, not per event)
-- resolved_outcome: 'YES' or 'NO' (the winning outcome for this specific market)
-- resolved_value:   the actual temperature that drove resolution, in resolved_unit
-- Use alongside weather_markets (bucket_type, lower_temp, upper_temp) to reconstruct which
-- temperature caused YES. Do not try to store "which bucket resolved" here — derive it by
-- joining to weather_markets WHERE resolved_outcome='YES'.
CREATE TABLE market_resolutions (
    condition_id     TEXT PRIMARY KEY,
    resolved_outcome TEXT NOT NULL,          -- 'YES' | 'NO'
    resolved_value   REAL,                   -- observed temp in resolved_unit (if known)
    resolved_unit    TEXT,                   -- 'C' | 'F'
    resolution_status TEXT,                  -- 'confirmed' | 'disputed' | 'pending'
    resolved_at_utc  TEXT,
    raw_payload_json TEXT
);
```

---

## File Structure

```
polymarket-weather/
├── scripts/
│   ├── init_db.py              # Single schema owner — all scripts call this first
│   ├── discover_markets.py     # Gamma/rules-first discovery; set first_seen_utc,
│   │                           # close_time_utc, source adapter, units, bucket ranges
│   ├── log_orderbooks.py       # Poll CLOB every 2 min; compute hours_to_close;
│   │                           # assign snapshot_label; store size/depth/raw book
│   ├── fetch_weather.py        # METAR + TAF + 5 NWP models; scheduled loop
│   ├── fetch_settlement_sources.py # WU/HKO/NOAA source adapters
│   ├── settle_markets.py       # Runs after source-specific finalization; writes
│   │                           # settlement_value_proxy + final resolution
│   └── neg_risk_scanner.py     # "above X°C" vs bucket sum + obs mismatch alerts
├── data/
│   └── city_stations.json      # Enriched cache: city, slug, station, lat, lon,
│                               # timezone, unit, country, source metadata
├── logs/
│   └── research/
├── weather.db
└── PLAN.md
```

---

## Timestamp Design — Rationale

Every timestamp in the system must answer exactly one question. Conflating two questions
into one field causes silent data corruption that is hard to find later.

### The five timestamp questions this system needs to answer

| Question | Field | Where stored |
|----------|-------|-------------|
| When did the station instrument take this reading? | `observed_utc` | `wx_observations` |
| When did our system retrieve it? | `fetched_utc` | `wx_observations`, `taf_forecasts`, `model_forecasts`, `fetch_log` |
| What local calendar day does this reading belong to? | `local_date` | `wx_observations`, `model_forecasts` |
| Which model run produced this forecast? | `model_run_utc` | `model_forecasts` |
| When was this market first visible to us? | `first_seen_utc` | `weather_markets` |

### Why each one matters and what goes wrong without it

**`observed_utc` vs `fetched_utc` — do not merge these**

A METAR observation at Tokyo RJTT is taken at, say, 08:30 JST (23:30 UTC the day before).
Our poller runs 25 minutes later and fetches it at 23:55 UTC. If we store the fetch time
as the observation time, every downstream query that asks "what was the temperature at X time"
is off by up to 30 minutes. Over a day of 48 polls, this compounds into a blurry, shifted
picture of how temperatures evolved. The actual observation time is in the METAR `reportTime`
field and must be stored as `observed_utc`. The fetch time goes in `fetched_utc`. Both matter
for different questions.

**`local_date` — never use `DATE(ts_utc)` or `dt.date.today()` for station day boundaries**

The daily high for a weather market is the highest temperature reached during the local
calendar day at that station. UTC date boundaries do not match local calendar days for
any non-UTC station.

Tokyo (UTC+9): after 15:00 UTC, `DATE(ts_utc)` returns "tomorrow" while Tokyo is still
the same day. The daily high computation resets 9 hours too early. Wellington (UTC+12/+13)
is worse — most of its afternoon falls into the wrong UTC date. For Auckland in summer
(UTC+13), 11 hours of the local day are on the "previous" UTC date.

The correct approach: convert `observed_utc` to the station's local timezone, extract the
date, store it as `local_date`, and group daily highs by `(station, local_date)`.

All 17 city timezones are stored in `data/city_stations.json` and loaded at runtime.
No script should use `dt.date.today()` or `DATE(column)` for station-local day logic.

**`model_run_utc` — estimate vs ground truth**

For open-meteo models, `model_run_utc` is estimated from the known run schedule plus
the configured fetch offset. This is a reasonable approximation but will be wrong when
a model run is delayed (common with GFS). It should be labelled as an estimate in queries
and never used as proof of which run was ingested. For ECMWF direct, the actual run time
is in the GRIB2 metadata and should be extracted directly.

**`forecast_date` — must use station's local timezone, not UTC**

open-meteo returns forecast dates in whatever timezone is requested. If no timezone is
passed (the current bug), open-meteo defaults to UTC. A forecast for "tomorrow" in Tokyo
returned as `2026-06-02` is correct if today is June 1 in Tokyo — but the server may
be in UTC where it is still June 1. The mismatch is worst at the UTC day boundary for
stations in UTC+12/+13. The fix: pass `timezone=<station tz>` per request, so dates in
the response are already in the station's local calendar.

**`first_seen_utc` — honest about what we know**

We do not have access to Polymarket's internal market creation timestamp. `first_seen_utc`
is when `discover_markets.py` first encountered the market in a scrape. It is a proxy for
market open, not the actual open. It is the correct field to use for Q1 ("what forecasts
were available at open?") because it bounds what this system could have known. If
Polymarket ever exposes a true creation timestamp via the Gamma API, it should go in a
separate field rather than overwriting `first_seen_utc`.

### Timestamp rules for all scripts

1. Always use `datetime.now(timezone.utc)` — never `datetime.now()` (timezone-naive)
2. Always store `observed_utc` from the source field (`reportTime` for METAR), never
   substitute fetch time
3. Always compute `local_date` from `observed_utc` + station timezone — never from
   `dt.date.today()` or `DATE(ts_utc)`
4. Always pass station timezone to open-meteo so response dates are local, not UTC
5. Label estimated timestamps (e.g. `model_run_utc` from schedule offsets) in comments
   so future queries know not to treat them as authoritative

---

## Analysis Query Correctness — End-to-End Trace

The timestamps are only safe if the queries that use them are also consistent.
Each analytical question was traced through its full join path to find failure modes.

### Q1: forecast available at market open

**The join**: `mf.forecast_date = wm.settlement_date`

Both fields must be in the **same reference frame** — the station's local calendar date.

`forecast_date` comes from open-meteo. open-meteo defaults to UTC if no timezone is
passed. If the fetch happens at 23:30 UTC and Tokyo's settlement is June 2 local,
open-meteo returns `forecast_date = '2026-06-01'` (UTC) while `settlement_date =
'2026-06-02'` (Tokyo local). The JOIN returns zero rows. No error is raised.

Fix: pass `timezone=<station tz>` per open-meteo request. Already in Phase 1 checklist.

`settlement_date` comes from Polymarket's `end_date_utc`. It must be derived by
converting `end_date_utc` to the station's local timezone and extracting the date —
not by taking `DATE(end_date_utc)` in UTC. These differ for any station where
UTC midnight and local midnight don't coincide, which is all non-UTC stations.

**Rule**: `settlement_date` = the local calendar day being measured (midnight to midnight local).
`close_time_utc` = confirmed universal `T12:00:00Z` on the named UTC date for all weather markets.

Canonical derivation (store as a comment in `discover_markets.py`):
```python
close_local = datetime.fromisoformat(close_time_utc).astimezone(ZoneInfo(station_tz))
# Wellington edge case: 12:00 UTC = midnight NZST, which is the START of the next local day.
# The day being measured is the one that just ended.
if close_local.time() == datetime.time(0, 0):
    settlement_date = (close_local.date() - timedelta(days=1)).isoformat()
else:
    settlement_date = close_local.date().isoformat()
```
This handles all cities including Wellington (UTC+12 → midnight) without special-casing each one.

### Q2: actual temperature resolution

**The settlement lookup**: `MAX(temp_c) WHERE station=? AND local_date=?`

`settle_markets.py` runs ~2h after local midnight (when the local calendar day is
complete and WU has had time to finalize). The daily high covers the full local calendar
day — midnight to midnight local — matching the rules language: "highest temperature
recorded for all times on this day."

Do NOT add `AND observed_utc <= close_time_utc`. Trading closure at 12:00 UTC does
not end the temperature measurement window. Polymarket resolves after local midnight
using the finalized full-day WU reading.

The `local_date` to query — use the canonical derivation above (same as `settlement_date`):
```python
from zoneinfo import ZoneInfo
close_local = datetime.fromisoformat(close_time_utc).astimezone(ZoneInfo(station_tz))
if close_local.time() == datetime.time(0, 0):           # Wellington edge case
    settlement_day = (close_local.date() - timedelta(days=1)).isoformat()
else:
    settlement_day = close_local.date().isoformat()
```

**The rounding problem**: METAR observations can report fractional degrees (e.g. 21.7°C).
Market buckets are whole integers. Polymarket applies a rounding rule at settlement —
but that rule is not stored anywhere in this system. If METAR daily high = 21.7°C, the
Q2 CASE expression with `bucket_type = 'exact' AND settlement_value_proxy = 21.7` will
never match any bucket. The resolved bucket will appear as NO for everything, which is wrong.

Fix: add `settlement_rounding_rule` to `weather_markets` (e.g. `'round'` / `'floor'` /
`'ceiling'`). Parse it from the market's rules text. Apply it when writing
`settlement_value_proxy` so the stored value is already rounded to the integer the
market will use. Until this is known, flag proxy settlements as `resolution_status =
'proxy_only'` and do not treat them as confirmed.

**The unit/range problem**: Not all markets are integer Celsius exact buckets. US markets
can use Fahrenheit range buckets (`68-69°F`), and Hong Kong rules can use one decimal
Celsius. Resolution logic must normalize source values into `settlement_unit`, then compare
against `lower_temp`/`upper_temp` in `bucket_unit`. If units differ, convert explicitly and
store both raw and normalized values.

### Q3: order book timeline

**`hours_to_close` arithmetic**: computed as `(close_time_utc - ts_utc) / 3600`.
Both are UTC strings. Subtraction is timezone-safe. ✓

**`snapshot_label` assignment**: relative time comparison against `hours_to_close`
thresholds. No calendar date involved. Timezone-safe. ✓

**Dependency risk**: `close_time_utc` must be populated before `log_orderbooks.py`
runs for a market. If `discover_markets.py` fails or hasn't run yet, `close_time_utc`
is NULL. `hours_to_close` is NULL. No labels are assigned. All raw snapshots are still
stored, but the standard interval view is empty for that market.

Mitigation: `log_orderbooks.py` should log a warning per market where `close_time_utc`
is NULL, and retry discovery before the next collection cycle.

**"Open" label is first-seen, not true market creation**: the `open` label fires on
the first snapshot within 5 minutes of `first_seen_utc`. If `discover_markets.py`
first runs 36 hours before settlement (instead of 48h), the "open" snapshot is at T-36h,
not T-48h. The data is honest — it reflects what we first observed — but queries
that assume "open = T-48h" will be misleading. Always present `first_seen_utc` alongside
`snapshot_label = 'open'` in analysis output so the actual discovery lag is visible.

**Executability requirement**: every orderbook snapshot must include size/depth and raw
book JSON. Midpoint-only analysis is insufficient for strategy testing because many apparent
edges disappear at executable bid/ask size.

### Safe query patterns

```sql
-- ✓ Correct: compare UTC to UTC
WHERE mf.fetched_utc <= wm.first_seen_utc

-- ✓ Correct: compare local date to local date
WHERE mf.forecast_date = wm.settlement_date   -- both derived in station timezone

-- ✓ Correct: aggregate daily high by local date
SELECT MAX(temp_c) FROM wx_observations
WHERE station = ? AND local_date = ?           -- local_date pre-computed at insert

-- ✗ Wrong: aggregate daily high by UTC date
SELECT MAX(temp_c) FROM wx_observations
WHERE station = ? AND DATE(ts_utc) = ?        -- will be wrong for non-UTC stations

-- ✗ Wrong: derive local date at query time
WHERE DATE(ts_utc) = DATE('now')               -- server UTC, not station local

-- ✓ Correct: derive settlement local date
-- In Python before querying:
-- local_date = datetime.fromisoformat(close_time_utc).astimezone(ZoneInfo(tz)).date().isoformat()
```

---

## Phase Plan

### Phase 1 — Data collection (in progress)

**Audit status as of 2026-06-01** — what is actually working vs what the plan describes:

| Component | Status | Finding |
|-----------|--------|---------|
| METAR fetch | ✅ Collecting | 16 stations, all responding |
| TAF fetch | ✅ Collecting | 15 TAFs, Seoul TX=31°C confirmed |
| GFS model | ✅ Collecting | 48 rows (16 stations × 3 days) |
| ICON, MF, GEM | ❌ Not yet run | Only GFS fetched so far |
| ECMWF direct | ❌ Not built | Still using open-meteo for ECMWF |
| weather_markets table | ❌ Missing | discover_markets.py not producing rows |
| ob_snapshots table | ❌ Missing | log_orderbooks.py not running |
| city_stations.json | ⚠️ Incomplete | Missing `timezone`, `lat`, `lon` fields |
| wx_observations schema | ❌ Old schema | Missing `observed_utc`, `local_date`, `fetched_utc` |
| Polymarket close_time_utc | ⚠️ Unclear | end_date is date-only; no time component found |
| rules/source adapters | ❌ Missing | WU/HKO/NOAA/final outcome adapters not built |
| unit/range parser | ❌ Missing | Fahrenheit ranges and decimal-C sources not supported |
| orderbook depth | ❌ Missing | sizes/raw book not stored |

**Critical findings from audit:**

**1. Schema is stale — wx_observations has wrong columns**
Current columns: `id, station, city, ts_utc, temp_c, daily_high_c, source`
Required columns: `id, station, city, observed_utc, fetched_utc, local_date, temp_c, daily_high_c, source`
`ts_utc` is storing fetch time, not observation time. `observed_utc` and `local_date` not present.

**2. city_stations.json missing timezone, lat, lon**
The plan designates it as the enriched metadata cache but it currently only has:
`city, slug, station, country, wu_path, anomaly, anomaly_note`
Missing: `timezone, lat, lon`
All scripts that need these fields (fetch_weather.py, settle_markets.py) hardcode them instead.

**3. Settlement time and temperature window — two separate clocks**

`end_date_iso` confirmed as `2026-06-01T12:00:00Z` for all weather markets globally.
This is the **trading close** — when positions lock. It is NOT the temperature measurement
window end.

Actual rules text scraped from live NYC market:

> *"This market will resolve to the temperature range that contains the highest temperature
> recorded at the LaGuardia Airport Station in degrees Fahrenheit on 1 Jun '26. The
> resolution source will be Wunderground, specifically the **highest temperature recorded
> for all times on this day** for the LaGuardia Airport Station... data not finalized for
> this market's timeframe will not be considered."*

**Two clocks are running independently:**

| Clock | What it controls | Value |
|-------|-----------------|-------|
| **Trading clock** | When positions lock | 12:00 UTC on named date |
| **Temperature clock** | What temperature is used | Full local calendar day from the market's named source |

NYC trading closes at 08:00 EDT — 7 hours before the typical afternoon peak. But the
resolution temperature is the full local calendar day. Polymarket's UMA resolver checks
WU after the local day is complete and the data is finalized. Trading may be locked but
the actual temperature keeps being measured until local midnight.

**settle_markets.py must run after the LOCAL DAY ends**, not 30 minutes after trading close.
"30 min after `close_time_utc`" was wrong — that fires during the local day for NYC/Miami/London.

Correct settle_markets.py fire times per city (~2h after local midnight):

| City | Local midnight (UTC) | Settle_markets runs |
|------|---------------------|---------------------|
| Seoul / Tokyo | 15:00 UTC same day | ~17:00 UTC same day |
| Wellington | 12:00 UTC same day | ~14:00 UTC same day |
| Hong Kong (HKO source) | 16:00 UTC same day | ~18:00 UTC same day |
| Beijing / Shenzhen / Guangzhou / Singapore | 16:00 UTC same day | ~18:00 UTC same day |
| Moscow (UUWW/NOAA) | 21:00 UTC same day | ~23:00 UTC same day |
| Madrid | 22:00 UTC same day | ~00:00 UTC next day |
| London | 23:00 UTC same day | ~01:00 UTC next day |
| NYC / Miami | 04:00 UTC next day | ~06:00 UTC next day |

**daily_high_c definition (corrected)**:
Track `MAX(temp_c)` from local midnight to local midnight — the full local calendar day.
Do NOT cap at `close_time_utc`. The trading window closing does not end the temperature
measurement period.

**settlement_date derivation**:
Parse the local date from the market question or URL (most reliable).
If deriving from `close_time_utc`: convert to station local timezone; if result is
exactly midnight (00:00), the date being measured is the previous local date (Wellington
edge case). Cleaner: `settlement_date = close_utc.astimezone(tz).date()` except when
`close_utc.astimezone(tz).time() == midnight`, subtract one day.

**Fields removed from schema** (were based on wrong model):
- ~~`settlement_window_hours`~~ — removed. Not a meaningful concept given two-clock model.

**4. forecast_date timezone: looks correct by coincidence today**
At 03:03 UTC on June 1, all station UTC dates and local dates happen to match (it is
June 1 everywhere except Pacific/Auckland which was already June 1 local).
This will fail silently when fetches happen near UTC midnight for UTC+ stations.

**Checklist:**
- [x] 17-market pilot identified, with live-rule caveats for HKO/NOAA/Fahrenheit markets
- [x] METAR fetch working — 16 stations responding, temperatures correct
- [x] TAF fetch working — TX/TN parsed for 5 stations, issued/valid timestamps in UTC
- [x] GFS via open-meteo — 48 rows confirmed
- [x] Retry logic + fetch_log logging correctly
- [ ] **Fix city_stations.json**: add `timezone`, `lat`, `lon` for all 17 entries
- [ ] **Rules-first discovery**: treat Gamma market rules as authority; refresh source,
      units, precision, bucket type/range, and station/source metadata from live rules
- [ ] **Fix wx_observations schema**: add `observed_utc`, `local_date`, `fetched_utc`;
      store METAR `reportTime` as `observed_utc`; compute `local_date` from that
      using station timezone; keep `fetched_utc` as when system retrieved it
- [ ] **Fix daily high query**: use `(station, local_date)` not `DATE(ts_utc)`
- [x] **Polymarket settlement time confirmed**: 12:00 UTC universal for all weather markets
      (scraped from settled Seoul/NYC markets: `end_date_iso = '2026-MM-DDT12:00:00Z'`)
- [ ] **Fix open-meteo timezone**: pass `timezone=<station tz>` per model request
      so `forecast_date` is in station's local calendar, not UTC
- [ ] **Run ICON, MF, GEM**: only GFS collected so far
- [ ] Create `scripts/init_db.py` as single schema owner; apply new schema
- [ ] ECMWF direct via `ecmwf-opendata` (3h faster than open-meteo mirror)
- [ ] `discover_markets.py` refined: populate `weather_markets` table with
      `first_seen_utc`, `close_time_utc`, `rules_source`, `resolution_source_url`,
      `settlement_rounding_rule`, units, bucket ranges, and raw market JSON
- [ ] `log_orderbooks.py`: compute `hours_to_close`, assign nullable `snapshot_label`,
      store sizes/depth/spread/raw book JSON
- [ ] Add `fetch_settlement_sources.py`: WU, Hong Kong Observatory, NOAA WRH adapters
- [ ] `settle_markets.py`: write proxy and final settlement, apply unit conversion,
      precision, bucket-aware logic, and final Polymarket/UMA reconciliation
- [ ] Full scheduled loop running continuously

### Phase 2 — Gap detection
- [ ] Neg-risk scanner: "above X°C" YES ≠ sum of constituent bucket YES prices
- [ ] Obs mismatch: METAR daily high already exceeds a bucket still priced >5¢
- [ ] Forecast divergence: models spread >3°C on same airport — flag and watch market
- [ ] End-of-day convergence: last 60 min, obvious NO entries

### Phase 3 — Analysis (after 2+ weeks of data)
- [ ] Which forecast model best predicts final settlement-source readings?
- [ ] Do HKO Hong Kong and NOAA Moscow source differences create systematic mispricings?
- [ ] Does model disagreement predict market mispricing?
- [ ] How often do neg-risk gaps appear, and how long do they last?
- [ ] Does settlement-source lag vs fast proxy data create a tradeable window?

---

## Key Open Questions

1. Which settlement source does each market actually name in its rules? (Must fetch per-market)
2. How much do settlement-source final values differ from fast proxy highs? (Need to measure)
3. Which cities have the most market liquidity and tradeable spreads?
4. Does ECMWF direct beat open-meteo ECMWF by enough to matter for daily high forecasts?
5. Are HKO/NOAA source-specific markets priced as if traders assume the wrong station/source?
