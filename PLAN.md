# Polymarket Weather Market Logger — Project Plan

## What This Is

Polymarket currently tracks **17 city markets** across **16 unique settlement stations**
(Hong Kong and Beijing both map to ZBAA).

Each day: "Will the highest temperature in [City] be exactly X°C?" markets
settle at the day's official high temperature from a specific airport weather station
named in each market's resolution rules.

This project collects three categories of data:
1. **METAR observations** — live airport instrument readings used as a fast intraday proxy
2. **Forecast data** — 6 independent sources predicting the day's high at each airport, 48h ahead
3. **Order book snapshots** — YES/NO prices on every temperature bucket, every 2 minutes

Goal: Find gaps between what forecasts say, what the thermometer reads, and what the market prices.

---

## Settlement Stations

| City        | Station | Airport                        | Timezone         | Notes                         |
|-------------|---------|--------------------------------|------------------|-------------------------------|
| Seoul       | RKSI    | Incheon International          | Asia/Seoul       |                               |
| Hong Kong   | ZBAA    | Beijing Capital International  | Asia/Shanghai    | ⚠️ Anomaly — resolves on Beijing station |
| London      | EGLC    | London City Airport            | Europe/London    |                               |
| Tokyo       | RJTT    | Haneda Airport                 | Asia/Tokyo       |                               |
| NYC         | KLGA    | LaGuardia Airport              | America/New_York |                               |
| Paris       | LFPB    | Le Bourget Airport             | Europe/Paris     |                               |
| Beijing     | ZBAA    | Beijing Capital International  | Asia/Shanghai    |                               |
| Miami       | KMIA    | Miami International Airport    | America/New_York |                               |
| Singapore   | WSSS    | Changi Airport                 | Asia/Singapore   |                               |
| Madrid      | LEMD    | Barajas Airport                | Europe/Madrid    |                               |
| Moscow      | EFHK    | Helsinki Vantaa Airport        | Europe/Helsinki  | ⚠️ Anomaly — resolves on Helsinki station |
| Munich      | EDDM    | Munich Airport                 | Europe/Berlin    |                               |
| Amsterdam   | EHAM    | Schiphol Airport               | Europe/Amsterdam |                               |
| Ankara      | LTAC    | Ankara Esenboga Airport        | Europe/Istanbul  |                               |
| Wellington  | NZWN    | Wellington Airport             | Pacific/Auckland |                               |
| Shenzhen    | ZGSZ    | Shenzhen Bao'an Airport        | Asia/Shanghai    |                               |
| Guangzhou   | ZGGG    | Guangzhou Baiyun Airport       | Asia/Shanghai    |                               |

All station metadata (city, station, lat, lon, timezone, WU path, anomaly flags) lives in
`data/city_stations.json` as the single source of truth. All scripts load from there.

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

`settle_markets.py` separates proxy settlement (METAR daily high at close time) from final
settlement (Polymarket/UMA resolved outcome), storing both.

---

## Data Sources

### Ground Truth — METAR (Fast Proxy)
**Source**: `aviationweather.gov/api/data/metar`
**What**: Live airport instrument readings. Used as a fast intraday proxy — the same
instrument family Weather Underground draws from, but METAR is real-time while WU publishes
a final daily summary that may differ (rounding, QA, data cutoff time).
**Frequency**: Every 30 minutes (stations update ~hourly; polling at 30 min catches every reading)
**Coverage**: All 16 unique settlement stations in a single batch API call
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
**Coverage**: All 16 stations fetched; only 5 include TX/TN temperature (RKSI, ZBAA, ZGGG, ZGSZ, LEMD).
**Horizon**: 30 hours (TAF format limitation — not full 48h)
**Timestamps stored**: `issued_utc`, `valid_from_utc`, `valid_to_utc`, `fetched_utc`

#### 2. ECMWF IFS — Direct from ECMWF Open Data
**Source**: `data.ecmwf.int` via `ecmwf-opendata` Python library
**What**: European Centre for Medium-Range Weather Forecasts IFS model. Gold standard globally.
0.25° resolution (~28km grid — all airports within 14km of a grid point).
**Why direct, not via open-meteo**: open-meteo mirrors ECMWF but adds 1–3h ingestion delay
on top of ECMWF's natural ~4–5h post-run time, yielding 6–8h total. Direct gives ~4–5h.
**Frequency**: 2× per day (00z and 12z runs). Fetch ~5h after run time (05:00 and 17:00 UTC).
**Coverage**: Global — all 16 airports confirmed.
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
- **Proxy resolution**: running METAR daily high at the station as of close time (fast, available same day)
- **Final resolution**: the Polymarket/UMA outcome (authoritative, available after settlement confirms)

**The query**:
```sql
SELECT wm.condition_id, wm.city, wm.settlement_date,
       wm.settlement_temp_c_proxy,   -- METAR daily high at close time
       wm.settlement_temp_c_final,   -- Polymarket/UMA resolved outcome (if available)
       wm.settlement_source,
       wm.settled_at_utc,
       wm.bucket_type, wm.temp_c,
       CASE
         WHEN wm.bucket_type = 'exact'    AND wm.settlement_temp_c_proxy = wm.temp_c  THEN 'YES'
         WHEN wm.bucket_type = 'above_eq' AND wm.settlement_temp_c_proxy >= wm.temp_c THEN 'YES'
         WHEN wm.bucket_type = 'below_eq' AND wm.settlement_temp_c_proxy <= wm.temp_c THEN 'YES'
         ELSE 'NO'
       END as resolved_proxy
FROM weather_markets wm
WHERE wm.settlement_date = ?
ORDER BY wm.city, wm.temp_c
```

**Requires**:
- `weather_markets.settlement_temp_c_proxy` — METAR daily high written by `settle_markets.py`
- `weather_markets.settlement_temp_c_final` — Polymarket/UMA outcome, written when available
- `weather_markets.settlement_source` — which source was used
- `weather_markets.settled_at_utc`

---

### Q3: Order book prices at open and at standard intervals to close?

**Standard intervals**: T-48h (open), T-24h, T-12h, T-6h, T-3h, T-1h, T-30min, T-close

**The query**:
```sql
SELECT ob.snapshot_label, ob.ts_utc, ob.hours_to_close,
       wm.temp_c, wm.bucket_type,
       ob.yes_bid, ob.yes_ask, ob.yes_mid
FROM ob_snapshots ob
JOIN weather_markets wm ON ob.condition_id = wm.condition_id
WHERE wm.city = ? AND wm.settlement_date = ?
ORDER BY ob.ts_utc, wm.temp_c
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
    temp_c          REAL,
    daily_high_c    REAL,                  -- MAX(temp_c) by (station, local_date)
    source          TEXT DEFAULT 'metar'
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
    UNIQUE(station, model, model_run_utc, forecast_date)
);

-- Polymarket weather markets (refreshed daily by discover_markets.py)
CREATE TABLE weather_markets (
    condition_id            TEXT PRIMARY KEY,
    city                    TEXT NOT NULL,
    station                 TEXT NOT NULL,
    settlement_date         TEXT NOT NULL,   -- YYYY-MM-DD in station's local timezone
    bucket_type             TEXT NOT NULL,   -- 'exact' | 'above_eq' | 'below_eq'
    temp_c                  REAL,
    yes_token_id            TEXT,
    no_token_id             TEXT,
    question                TEXT,
    rules_text              TEXT,            -- raw resolution rules from Polymarket
    rules_source            TEXT,            -- named source in rules (e.g. 'Weather Underground')
    resolution_source_url   TEXT,            -- exact URL named in rules
    first_seen_utc          TEXT,            -- when discover_markets.py first found this market
    close_time_utc          TEXT,            -- settlement UTC time from Polymarket end_date
    settlement_temp_c_proxy REAL,            -- Q2: METAR daily high at close time (fast proxy)
    settlement_temp_c_final REAL,            -- Q2: Polymarket/UMA resolved outcome (final truth)
    settlement_source       TEXT,            -- which source was used for proxy settlement
    settlement_rounding_rule TEXT,           -- Q2: how Polymarket rounds fractional temps
                                             --     'round' | 'floor' | 'ceiling' | unknown
                                             --     METAR reports 21.7C; bucket is integer 22C
                                             --     must apply same rule or proxy match fails
    -- settlement_window_hours REMOVED — was based on wrong model.
    -- Trading close (12:00 UTC) ≠ temperature measurement end.
    -- Resolution = WU full local calendar day per market rules.
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
    no_bid          REAL,
    no_ask          REAL,
    yes_mid         REAL,
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
```

---

## File Structure

```
polymarket-weather/
├── scripts/
│   ├── init_db.py              # Single schema owner — all scripts call this first
│   ├── discover_markets.py     # Scrape Polymarket daily; set first_seen_utc, close_time_utc,
│   │                           # rules_source, resolution_source_url per market
│   ├── log_orderbooks.py       # Poll CLOB every 2 min; compute hours_to_close;
│   │                           # assign snapshot_label near standard thresholds
│   ├── fetch_weather.py        # METAR + TAF + 5 NWP models; scheduled loop
│   ├── settle_markets.py       # Runs 30min after close_time_utc per city; writes
│   │                           # settlement_temp_c_proxy + settlement_temp_c_final
│   └── neg_risk_scanner.py     # "above X°C" vs bucket sum + obs mismatch alerts
├── data/
│   └── city_stations.json      # Single source of truth: city, station, lat, lon,
│                               # timezone, country, wu_path, anomaly flags
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

**Rule**: `settlement_date` = `end_date_utc` converted to station timezone, date only.
`close_time_utc` = settlement date + `T12:00:00Z` (confirmed universal for all weather markets).
Store the derivation method in a comment in `discover_markets.py` so it cannot drift.

Note: for cities where 12:00 UTC is before local midnight (Wellington), the settlement
date local is the NEXT day's date. For `settlement_date` use the date of the local day
being measured, which is `close_time_utc - 12h` converted to local date for all cities.
Concretely: `settlement_date = (close_dt_utc - timedelta(hours=12)).astimezone(tz).date()`
guarantees you get the measured day's date, not the settlement day's date.

### Q2: actual temperature resolution

**The settlement lookup**: `MAX(temp_c) WHERE station=? AND local_date=?`

`settle_markets.py` runs ~2h after local midnight (when the local calendar day is
complete and WU has had time to finalize). The daily high covers the full local calendar
day — midnight to midnight local — matching the rules language: "highest temperature
recorded for all times on this day."

Do NOT add `AND observed_utc <= close_time_utc`. Trading closure at 12:00 UTC does
not end the temperature measurement window. Polymarket resolves after local midnight
using the finalized full-day WU reading.

The `local_date` to query:
```python
close_local = datetime.fromisoformat(close_time_utc).astimezone(ZoneInfo(tz))
# Handle Wellington edge case: if close_time is exactly local midnight,
# the measured day is the previous local date
if close_local.time() == datetime.time(0, 0):
    settlement_day = (close_local - timedelta(days=1)).date()
else:
    settlement_day = close_local.date()
```

Correct pattern:
```python
from zoneinfo import ZoneInfo
tz = ZoneInfo(station_info["timezone"])
close_dt = datetime.fromisoformat(close_time_utc).astimezone(tz)
local_date = close_dt.date().isoformat()
```

**The rounding problem**: METAR observations can report fractional degrees (e.g. 21.7°C).
Market buckets are whole integers. Polymarket applies a rounding rule at settlement —
but that rule is not stored anywhere in this system. If METAR daily high = 21.7°C, the
Q2 CASE expression with `bucket_type = 'exact' AND settlement_temp_c_proxy = 21.7` will
never match any bucket. The resolved bucket will appear as NO for everything, which is wrong.

Fix: add `settlement_rounding_rule` to `weather_markets` (e.g. `'round'` / `'floor'` /
`'ceiling'`). Parse it from the market's rules text. Apply it when writing
`settlement_temp_c_proxy` so the stored value is already rounded to the integer the
market will use. Until this is known, flag proxy settlements as `resolution_status =
'proxy_only'` and do not treat them as confirmed.

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

**Critical findings from audit:**

**1. Schema is stale — wx_observations has wrong columns**
Current columns: `id, station, city, ts_utc, temp_c, daily_high_c, source`
Required columns: `id, station, city, observed_utc, fetched_utc, local_date, temp_c, daily_high_c, source`
`ts_utc` is storing fetch time, not observation time. `observed_utc` and `local_date` not present.

**2. city_stations.json missing timezone, lat, lon**
The plan designates it as single source of truth but it currently only has:
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
| **Temperature clock** | What temperature is used | WU full local calendar day (midnight to midnight local) |

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
| Beijing / Shenzhen / Guangzhou / Singapore | 16:00 UTC same day | ~18:00 UTC same day |
| Helsinki (Moscow*) | 21:00 UTC same day | ~23:00 UTC same day |
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
- [x] 17 city markets / 16 unique settlement stations identified and mapped
- [x] METAR fetch working — 16 stations responding, temperatures correct
- [x] TAF fetch working — TX/TN parsed for 5 stations, issued/valid timestamps in UTC
- [x] GFS via open-meteo — 48 rows confirmed
- [x] Retry logic + fetch_log logging correctly
- [ ] **Fix city_stations.json**: add `timezone`, `lat`, `lon` for all 17 entries
- [ ] **Fix wx_observations schema**: add `observed_utc`, `local_date`, `fetched_utc`;
      store METAR `reportTime` as `observed_utc`; compute `local_date` from that
      using station timezone; keep `fetched_utc` as when system retrieved it
- [ ] **Fix daily high query**: use `(station, local_date)` not `DATE(ts_utc)`
- [ ] **Determine Polymarket settlement time**: scrape one settled market's resolution
      details to find exact UTC close time per city; hardcode if consistent
- [ ] **Fix open-meteo timezone**: pass `timezone=<station tz>` per model request
      so `forecast_date` is in station's local calendar, not UTC
- [ ] **Run ICON, MF, GEM**: only GFS collected so far
- [ ] Create `scripts/init_db.py` as single schema owner; apply new schema
- [ ] ECMWF direct via `ecmwf-opendata` (3h faster than open-meteo mirror)
- [ ] `discover_markets.py` refined: populate `weather_markets` table with
      `first_seen_utc`, `close_time_utc`, `rules_source`, `resolution_source_url`,
      `settlement_rounding_rule`
- [ ] `log_orderbooks.py`: compute `hours_to_close`, assign nullable `snapshot_label`
- [ ] `settle_markets.py`: write proxy and final settlement, apply rounding rule
- [ ] Full scheduled loop running continuously

### Phase 2 — Gap detection
- [ ] Neg-risk scanner: "above X°C" YES ≠ sum of constituent bucket YES prices
- [ ] Obs mismatch: METAR daily high already exceeds a bucket still priced >5¢
- [ ] Forecast divergence: models spread >3°C on same airport — flag and watch market
- [ ] End-of-day convergence: last 60 min, obvious NO entries

### Phase 3 — Analysis (after 2+ weeks of data)
- [ ] Which forecast model best predicts actual METAR settlement reading?
- [ ] Do HK (ZBAA) and Moscow (EFHK) anomalies create systematic mispricings?
- [ ] Does model disagreement predict market mispricing?
- [ ] How often do neg-risk gaps appear, and how long do they last?
- [ ] Does WU data lag vs real-time METAR create a tradeable window?

---

## Key Open Questions

1. Which settlement source does each market actually name in its rules? (Must scrape per-market)
2. How much does WU final daily summary differ from raw METAR daily high? (Need to measure)
3. Which cities have the most market liquidity and tradeable spreads?
4. Does ECMWF direct beat open-meteo ECMWF by enough to matter for daily high forecasts?
5. Are HK and Moscow anomaly markets priced on the correct station or the wrong one?
