# Polymarket Weather Market Logger — Project Plan

## What This Is

Polymarket runs daily staircase temperature markets for 16 cities.
Each day: "Will the highest temperature in [City] be exactly X°C?" markets
settle at the day's official high from a specific Weather Underground airport station.

This project collects:
1. **METAR observations** — live airport instrument readings, the exact same data WU uses to settle markets
2. **Forecast data** — 6 independent sources predicting the day's high at each airport, 48h ahead
3. **Order book snapshots** — YES/NO prices on every temperature bucket, every 2 minutes

Goal: Find gaps between what forecasts say, what the thermometer reads, and what the market prices.

---

## Settlement Stations (Weather Underground ICAO codes)

| City        | Station | Airport                        | Notes                         |
|-------------|---------|--------------------------------|-------------------------------|
| Seoul       | RKSI    | Incheon International          |                               |
| Hong Kong   | ZBAA    | Beijing Capital International  | ⚠️ Anomaly — resolves on Beijing station |
| London      | EGLC    | London City Airport            |                               |
| Tokyo       | RJTT    | Haneda Airport                 |                               |
| NYC         | KLGA    | LaGuardia Airport              |                               |
| Paris       | LFPB    | Le Bourget Airport             |                               |
| Beijing     | ZBAA    | Beijing Capital International  |                               |
| Miami       | KMIA    | Miami International Airport    |                               |
| Singapore   | WSSS    | Changi Airport                 |                               |
| Madrid      | LEMD    | Barajas Airport                |                               |
| Moscow      | EFHK    | Helsinki Vantaa Airport        | ⚠️ Anomaly — resolves on Helsinki station |
| Munich      | EDDM    | Munich Airport                 |                               |
| Amsterdam   | EHAM    | Schiphol Airport               |                               |
| Ankara      | LTAC    | Ankara Esenboga Airport        |                               |
| Wellington  | NZWN    | Wellington Airport             |                               |
| Shenzhen    | ZGSZ    | Shenzhen Bao'an Airport        |                               |
| Guangzhou   | ZGGG    | Guangzhou Baiyun Airport       |                               |

---

## Data Sources

### Ground Truth — METAR
**Source**: `aviationweather.gov/api/data/metar`
**What**: Live airport instrument readings — the same underlying data Weather Underground
displays and Polymarket uses to settle markets.
**Frequency**: Every 30 minutes (stations update ~hourly; polling at 30 min catches every reading)
**Coverage**: All 16 settlement stations in a single batch API call
**Key field**: `temp` (current 2m temperature in °C). We compute running daily high
ourselves by tracking `MAX(temp_c)` since local midnight.

---

### Forecast Sources

Six independent sources all predicting the daily high at each airport's exact lat/lon, 48h ahead.
Fetched at each source's actual update cadence — no point fetching more often than the model runs.

#### 1. TAF (Terminal Aerodrome Forecast)
**Source**: `aviationweather.gov/api/data/taf`
**What**: Official aviation forecast issued by the national meteorological agency for that
exact airport. Same institution that produces the METAR. Where TX/TN temperature fields
are present, this is the highest-quality forecast available.
**Frequency**: Every 6h (issued at 00, 06, 12, 18 UTC). Fetch 30 min after issuance.
**Coverage**: All 16 stations — but only 5 include TX/TN temperature forecasts (RKSI, ZBAA,
ZGGG, ZGSZ, LEMD). Others get wind/cloud but no temp.
**Horizon**: 30 hours (not 48h — limitation of TAF format)

#### 2. ECMWF IFS — Direct from ECMWF Open Data
**Source**: `data.ecmwf.int` via `ecmwf-opendata` Python library
**What**: European Centre for Medium-Range Weather Forecasts IFS model. Gold standard
globally. 0.25° resolution (~28km grid — airports within 14km of a grid point).
**Why direct, not via open-meteo**: open-meteo mirrors ECMWF but adds 1–3h ingestion
delay on top of ECMWF's natural ~4–5h post-run publication time, yielding 6–8h total.
Fetching directly from ECMWF Open Data gives data ~4–5h after run time.
**Frequency**: 2× per day (00z and 12z runs). Fetch ~5h after run time (05:00 and 17:00 UTC).
**Coverage**: Global — all 16 airports confirmed working.
**Format**: GRIB2, parsed with `cfgrib`. Extract 2m temp (param `2t`) at forecast steps
+24h and +48h, compute daily max across 3-hourly steps within the local day.
**Connection limits**: ECMWF rate-limits direct access. At 2 fetches/day for 16 airports
this is well within limits, but implement exponential backoff on 429 responses.
**Library**: `pip install ecmwf-opendata cfgrib`

#### 3. GFS (Global Forecast System)
**Source**: `api.open-meteo.com` — model `gfs_seamless`
**What**: NOAA's primary global model. Run by the US National Centers for Environmental
Prediction. Standard reference for North American weather; solid global coverage.
**Frequency**: 4× per day (00, 06, 12, 18 UTC runs). Fetch ~4h after run time.
**Coverage**: Global — all 16 airports confirmed working.

#### 4. ICON (Icosahedral Nonhydrostatic)
**Source**: `api.open-meteo.com` — model `icon_seamless`
**What**: DWD (German Weather Service) global model. Particularly strong over Europe
but good globally. Uses a different numerical approach to GFS/ECMWF.
**Frequency**: 4× per day. Fetch ~3h after run time (faster than GFS to publish).
**Coverage**: Global — all 16 airports confirmed working.

#### 5. Météo-France ARPEGE
**Source**: `api.open-meteo.com` — model `meteofrance_seamless`
**What**: French national model. Good for European and tropical regions.
**Frequency**: 4× per day. Fetch ~3.5h after run time.
**Coverage**: Global — all 16 airports confirmed working.

#### 6. GEM (Global Environmental Multiscale)
**Source**: `api.open-meteo.com` — model `gem_seamless`
**What**: Environment and Climate Change Canada's model. Best for North America,
solid elsewhere.
**Frequency**: 4× per day. Fetch ~3.5h after run time.
**Coverage**: Global — all 16 airports confirmed working.

---

## Three Analytical Questions — Data Requirements

### Q1: What are the 5 models predicting 48h out at market open?

**What "market open" means**: Temperature markets appear on Polymarket ~48h before
settlement. `discover_markets.py` records `first_seen_utc` the moment it first finds
a market. That timestamp is the open.

**The query**:
For each model, find the most recent forecast that was *available* at open time:
```sql
SELECT mf.model, mf.high_c, mf.low_c, mf.fetched_utc, mf.model_run_utc
FROM model_forecasts mf
JOIN weather_markets wm ON mf.station = wm.station
                        AND mf.forecast_date = wm.settlement_date
WHERE wm.condition_id = ?
  AND mf.fetched_utc <= wm.open_time_utc
GROUP BY mf.model
HAVING mf.fetched_utc = MAX(mf.fetched_utc)
```

**Requires**:
- `weather_markets.open_time_utc` — set by `discover_markets.py` on first discovery
- `weather_markets.settlement_date` — already planned
- `model_forecasts` populated before market opens (TAF and models run ahead of time)

---

### Q2: What was the actual temperature resolution?

**What "resolution" means**: The official METAR daily high at the settlement station
on the settlement date, as recorded at or just after the market's close time.

**The query**:
```sql
SELECT wm.condition_id, wm.city, wm.settlement_date,
       wm.settlement_temp_c, wm.settled_at_utc,
       wm.bucket_type, wm.temp_c,
       CASE WHEN wm.settlement_temp_c = wm.temp_c THEN 'YES'
            ELSE 'NO' END as resolved
FROM weather_markets wm
WHERE wm.settlement_date = ?
ORDER BY wm.city, wm.temp_c
```

**Requires**:
- `weather_markets.settlement_temp_c` — written by settlement job after close
- `weather_markets.settled_at_utc` — when the settlement was recorded
- A settlement job that runs shortly after `close_time_utc`, reads the final METAR
  `daily_high_c` for that station, and writes it back to all markets for that city+date

---

### Q3: Order book prices at open and at standard intervals to close?

**Standard intervals**: T-48h (open), T-24h, T-12h, T-6h, T-3h, T-1h, T-30min, T-close

**The query**:
```sql
SELECT ob.snapshot_label, ob.ts_utc,
       wm.temp_c, wm.bucket_type,
       ob.yes_bid, ob.yes_ask, ob.yes_mid
FROM ob_snapshots ob
JOIN weather_markets wm ON ob.condition_id = wm.condition_id
WHERE wm.city = ? AND wm.settlement_date = ?
ORDER BY ob.ts_utc, wm.temp_c
```

**Requires**:
- `ob_snapshots.snapshot_label` — tag applied at capture time:
  `open` / `T-24h` / `T-12h` / `T-6h` / `T-3h` / `T-1h` / `T-30m` / `close`
- `weather_markets.open_time_utc` and `close_time_utc` — to compute label offsets
- `log_orderbooks.py` to apply labels: compute `hours_to_close` at each snapshot,
  assign the nearest standard label if within 5 minutes of a threshold

---

## Fetch Schedule

| Source       | Runs/day | Fetch times (UTC)          | Delay after run | Horizon |
|--------------|----------|----------------------------|-----------------|---------|
| METAR        | 48       | Every :00 and :30          | Real-time       | n/a     |
| TAF          | 4        | 00:30, 06:30, 12:30, 18:30 | 30 min          | 30h     |
| ECMWF direct | 2        | ~05:00, ~17:00             | ~5h             | 48h+    |
| GFS          | 4        | ~04:00, 10:00, 16:00, 22:00| ~4h             | 48h     |
| ICON         | 4        | ~03:00, 09:00, 15:00, 21:00| ~3h             | 48h     |
| Météo-France | 4        | ~03:30, 09:30, 15:30, 21:30| ~3.5h           | 48h     |
| GEM          | 4        | ~03:30, 09:30, 15:30, 21:30| ~3.5h           | 48h     |

---

## Retry Policy

Every fetch is wrapped in `with_retry()`:
- **3 attempts** maximum per fetch cycle
- **180 second wait** between attempts
- Every attempt logged to `fetch_log` with exact UTC timestamp, duration in ms,
  status (`success` / `retry` / `failed`), records saved, and error message

---

## Database Schema

```sql
-- Live airport instrument readings (ground truth)
CREATE TABLE wx_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    temp_c          REAL,
    daily_high_c    REAL,              -- running max since local midnight
    source          TEXT DEFAULT 'metar'
);

-- TAF aviation forecasts (TX/TN where available)
CREATE TABLE taf_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    issued_utc      TEXT NOT NULL,
    valid_from_utc  TEXT NOT NULL,
    valid_to_utc    TEXT NOT NULL,
    tx_c            REAL,             -- forecast daily max
    tx_time_utc     TEXT,
    tn_c            REAL,             -- forecast daily min
    tn_time_utc     TEXT,
    raw_taf         TEXT,
    fetched_utc     TEXT NOT NULL,
    UNIQUE(station, issued_utc)
);

-- NWP model forecasts (GFS, ECMWF, ICON, Meteofrance, GEM)
CREATE TABLE model_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    model           TEXT NOT NULL,    -- gfs_seamless | ecmwf_ifs025 | icon_seamless | etc.
    model_run_utc   TEXT,             -- which run this data came from
    fetched_utc     TEXT NOT NULL,
    forecast_date   TEXT NOT NULL,    -- YYYY-MM-DD local date being forecast
    horizon_hours   INTEGER,          -- 24 or 48
    high_c          REAL,
    low_c           REAL,
    lat             REAL,
    lon             REAL
);

-- Orderbook snapshot every 2 minutes per market
CREATE TABLE ob_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    yes_bid         REAL,
    yes_ask         REAL,
    no_bid          REAL,
    no_ask          REAL,
    yes_mid         REAL,
    hours_to_close  REAL,              -- Q3: computed at capture time
    snapshot_label  TEXT               -- Q3: 'open'|'T-24h'|'T-12h'|'T-6h'|
                                       --     'T-3h'|'T-1h'|'T-30m'|'close'|NULL
);

-- Polymarket weather markets (refreshed daily)
CREATE TABLE weather_markets (
    condition_id        TEXT PRIMARY KEY,
    city                TEXT NOT NULL,
    station             TEXT NOT NULL,
    settlement_date     TEXT NOT NULL,    -- YYYY-MM-DD
    bucket_type         TEXT NOT NULL,    -- 'exact' | 'above_eq' | 'below_eq'
    temp_c              REAL,             -- the temperature this bucket represents
    yes_token_id        TEXT,
    no_token_id         TEXT,
    question            TEXT,
    open_time_utc       TEXT,             -- Q1,Q3: first seen by discover_markets.py
    close_time_utc      TEXT,             -- Q3: settlement UTC time (end_date_utc)
    settlement_temp_c   REAL,             -- Q2: final METAR daily high at settlement
    settled_at_utc      TEXT,             -- Q2: when settlement was recorded
    active              INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT (datetime('now'))
);

-- Complete fetch audit trail
CREATE TABLE fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,        -- exact UTC timestamp of this attempt
    source      TEXT NOT NULL,        -- metar | taf | ecmwf_direct | gfs_seamless | etc.
    station     TEXT,                 -- ICAO or 'batch' or 'all_airports'
    attempt     INTEGER NOT NULL,     -- 1, 2, or 3
    status      TEXT NOT NULL,        -- success | retry | failed
    n_records   INTEGER DEFAULT 0,
    error       TEXT,
    duration_ms INTEGER
);

-- Flagged opportunities
CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    city        TEXT NOT NULL,
    alert_type  TEXT NOT NULL,        -- neg_risk_gap | obs_mismatch | forecast_divergence | convergence
    detail_json TEXT
);
```

---

## File Structure

```
polymarket-weather/
├── scripts/
│   ├── discover_markets.py     # Scrape Polymarket daily, populate weather_markets
│   ├── log_orderbooks.py       # Poll CLOB every 2 min, store ob_snapshots
│   ├── fetch_weather.py        # All weather collection: METAR + TAF + 5 models (scheduled loop)
│   └── neg_risk_scanner.py     # "above X°C" vs bucket sum + obs mismatch alerts
├── data/
│   └── city_stations.json      # ICAO → city, lat, lon, anomaly flags
├── logs/
│   └── research/
├── weather.db
└── PLAN.md
```

---

## Phase Plan

### Phase 1 — Data collection ✅ In progress
- [x] 16 settlement stations identified and mapped
- [x] METAR fetch working — all 16 stations, exact resolution source
- [x] TAF fetch working — TX/TN parsed for 5 stations
- [x] GFS, ICON, Météo-France, GEM via open-meteo — all 16 airports confirmed
- [x] Retry logic + fetch_log with exact timestamps
- [ ] ECMWF direct via `ecmwf-opendata` library (replace open-meteo ECMWF)
- [ ] `discover_markets.py` refined: set `open_time_utc` on first discovery,
      `close_time_utc` from Polymarket end_date, dedupe condition IDs properly
- [ ] `log_orderbooks.py`: compute `hours_to_close` per snapshot, apply
      `snapshot_label` at standard intervals (open/T-24h/.../close)
- [ ] `settle_markets.py` (new): runs ~30 min after each city's `close_time_utc`,
      reads final METAR `daily_high_c`, writes `settlement_temp_c` + `settled_at_utc`
      back to all weather_markets rows for that city+date
- [ ] Full loop running continuously (`fetch_weather.py --loop`)

### Phase 2 — Gap detection
- [ ] `neg_risk_scanner.py`: "above X°C" vs sum of constituent bucket YES prices
- [ ] Obs mismatch: current METAR high > market's implied high, flag it
- [ ] Forecast divergence: when models spread >3°C across same airport, flag it
- [ ] End-of-day convergence: last 60 min, scan for obvious NO entries

### Phase 3 — Analysis (after 2+ weeks of data)
- [ ] How often do neg-risk gaps appear vs BTC (5/day)?
- [ ] Do gap durations allow manual execution or require API?
- [ ] Which forecast model best predicts the METAR settlement reading?
- [ ] Do the HK (ZBAA) and Moscow (EFHK) anomalies create systematic mispricings?
- [ ] Does model disagreement (spread >3°C) predict market mispricing?

---

## Key Open Questions

1. How accurate is each model vs actual METAR settlement? (Need 2+ weeks to measure)
2. Which cities have the most market liquidity / tradeable spreads?
3. Does the ~30 min WU data lag (vs real-time METAR) create a window like the BTC lag?
4. Are HK and Moscow anomaly markets priced on the correct station or the wrong one?
5. Does ECMWF direct beat open-meteo ECMWF by enough to matter for daily high forecasts?
