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
| Hong Kong   | VHHH               | Hong Kong Observatory (HKO)    | Asia/Hong_Kong   | Settlement: HKO Daily Extract. Fast proxy: VHHH (HK Int'l Airport, nearest ICAO, ~13km from HKO) |
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

**Alert latency requirement**: The effective last-actionable obs_mismatch for Type B is
not the reading observed just before 12:00 UTC — it is the last reading fetched AND
processed before 12:00 UTC. With a 30-minute METAR poll cycle, any observation taken
between 11:31 and 12:00 UTC is fetched at 12:00 and processed after trading closes.
The true last-actionable window ends at ~11:30 UTC (the :30 poller), not 12:00 UTC.

The alert pipeline must insert and fire obs_mismatch alerts within seconds of a new
METAR row being committed — not at the next poll cycle. If alert generation is batched
with the METAR fetch loop, the effective signal window shrinks to ~11:00 UTC (the
last :00 poller that completes before 12:00). Any design that batches alerts with fetches
loses the final 30–60 minutes of the most volatile window for Type B markets.

**Required**: obs_mismatch alerts must be generated immediately on METAR insert (trigger-
or callback-based), not deferred to the next scheduled run.

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

**Unit conversion required for obs_mismatch**: METAR always reports temperature in Celsius
(`daily_high_c`), but NYC and Miami bucket thresholds are stored in Fahrenheit (`bucket_unit='F'`).
Any obs_mismatch comparison must convert `daily_high_c` to Fahrenheit before comparing against
`lower_temp`/`upper_temp`. Comparing Celsius directly against Fahrenheit thresholds produces
completely wrong alerts (e.g. 35°C compared against 93°F threshold — numerically 35 < 93, so
the alert silently misses that 35°C = 95°F has already exceeded the 94°F bucket). This
conversion requirement applies to all three cities where `bucket_unit != 'C'`.

```python
# In neg_risk_scanner.py obs_mismatch check:
daily_high = obs["daily_high_c"]
if market["bucket_unit"] == "F":
    daily_high = daily_high * 9/5 + 32   # convert to F before comparing bucket thresholds
# Then compare daily_high against lower_temp / upper_temp
```

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

### All weather staircase markets are neg-risk markets

Confirmed from CLOB API: `neg_risk: true` for every weather bucket market.
`neg_risk_market_id` groups all buckets for the same city + settlement date.

This has two direct implications:

**1. Neg-risk arbitrage applies**
The same "above X°C YES ≈ sum of constituent bucket YES prices" constraint that
applies to BTC staircase markets applies here. The neg-risk scanner for weather
should use `neg_risk_market_id` to identify the group, then check the consistency
constraint across all buckets in the group.

**2. Capital efficiency for multi-leg trades**
Polymarket's neg-risk collateral netting applies. Buying NO on multiple buckets
in the same neg-risk group does not require posting full face value for each leg.
Maximum loss = 1 × stake (you can only be in the wrong bucket once).

Store `neg_risk_market_id` and `neg_risk_request_id` in `weather_markets` for
group lookups in the scanner.

**Multi-group guard**: the assumption "one `neg_risk_market_id` per city+date" may be
violated if Polymarket adds new buckets mid-day via a separate UMA request, producing a
second `neg_risk_market_id` for the same city+date. If the scanner only groups by
`neg_risk_market_id`, it checks each group independently and misses cross-group price
inconsistencies. Required behavior:

- `discover_markets.py` must warn if more than one `neg_risk_market_id` is found for
  the same `(city, settlement_date)`.
- `neg_risk_scanner.py` must group by `(city, settlement_date)` first, then check for
  multiple `neg_risk_market_id` values. If multiple exist, run the staircase consistency
  check across ALL buckets for that city+date regardless of group ID. Flag inter-group
  price gaps as a separate alert type (`alert_type = 'neg_risk_gap_cross_group'`) so
  they are distinguishable from within-group gaps.

---

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

**Guard — market discovered after close**: if `first_seen_utc >= close_time_utc`, the market
was not discovered until after trading locked. This happens when the scheduler is down for
an extended period. `discover_markets.py` must detect this condition and immediately set
`active=0` with a log warning. These markets must be excluded from Q1 analysis — their
`first_seen_utc` is post-close, so `mf.fetched_utc <= wm.first_seen_utc` returns
post-close forecasts that appear to be "available at open" but are not. The Q1 query
already includes `AND wm.first_seen_utc < wm.close_time_utc` as a required filter (see
safe query patterns).

**Zero-row result handling**: Q1 may legitimately return zero rows for a model if the
market was discovered before that model's forecast horizon extends to the settlement date.
Example: market discovered at T-50h, but GFS only publishes 48h ahead — no GFS forecast
existed for the settlement date at discovery time. This is expected and must be reported
as "no T-48h forecast available" rather than treated as a collection failure. Distinguish
the two cases using `horizon_hours` stored in `model_forecasts`: if
`(close_time_utc - first_seen_utc) > model_horizon_hours`, the absence is a collection
failure; if less, it is expected.

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
       wm.proxy_outcome,             -- YES/NO under the source-adapted proxy value
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
- `weather_markets.proxy_outcome` — bucket-level YES/NO under `settlement_value_proxy`
- `weather_markets.settlement_unit` — unit of the normalized settlement value
- `weather_markets.bucket_unit` — unit used by bucket thresholds
- `weather_markets.settlement_source` — which source was used
- `weather_markets.settled_at_utc`

---

### Q3: Order book prices at open and at standard intervals to close?

**Standard intervals**: `open` (first-seen), `T-24h`, `T-12h`, `T-6h`, `T-3h`, `T-1h`, `T-30min`, `post_close`

**`snapshot_label` semantics — two kinds of labels:**

Every 2-minute snapshot is stored and receives a bin label based on `hours_to_close`. This
makes time-slice queries simple. In addition, the first snapshot taken within 5 minutes of
`first_seen_utc` receives the special `open` label, which is set only once per market and
never overwritten.

Bin label assignment (applied to every snapshot by `hours_to_close`):

| Label | Fires when `hours_to_close` is… |
|-------|----------------------------------|
| `T-30min` | ≤ 0.5h |
| `T-1h` | ≤ 1h |
| `T-3h` | ≤ 3h |
| `T-6h` | ≤ 6h |
| `T-12h` | ≤ 12h |
| `T-24h` | ≤ 24h |
| `open_window` | > 24h and no `open` label yet earned |
| `post_close` | < 0h (after trading close) |

`open` label: fires once — on the first snapshot for that `condition_id` whose `ts_utc` is
within 5 minutes of `weather_markets.first_seen_utc`. Requires `first_seen_utc` to be loaded
into the market dict by `_load_active_markets`. If `first_seen_utc` is NULL or no snapshot
falls in the window, no `open` label is assigned.

Note: T-48h is not a bin label because few markets are discovered exactly 48h before close.
The `open` label is the honest substitute — it records what we first observed, whenever that
was. Always display `first_seen_utc` alongside `snapshot_label='open'` in analysis output
so the actual discovery lag is visible.

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

**Requires**:
- `ob_snapshots.snapshot_label` — bin label always set; `open` set once per market
- `ob_snapshots.hours_to_close` — computed at capture time from `close_time_utc`
- `weather_markets.first_seen_utc` and `close_time_utc` — loaded into market dict at capture time

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

-- TAF aviation forecasts (TX/TN where available; only a subset of stations include temp)
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
--
-- KEY CLOB FIELDS (confirmed from live API):
--   game_start_time      = local midnight UTC — start of temperature measurement window
--   end_date_iso         = T00:00:00Z placeholder — just a date, NOT trading close time
--   accepting_orders     = False when trading closed
--   accepting_order_timestamp = market creation time (when orders first accepted)
--   neg_risk             = True for ALL weather staircase markets
--   neg_risk_market_id   = groups all buckets for same city+date (use for neg-risk scanner)
--   neg_risk_request_id  = UMA neg-risk request identifier
--
-- TRADING CLOSE (confirmed): 12:00 UTC on settlement_date — from Gamma/HTML endDate field
-- TEMP WINDOW: game_start_time (local midnight) → next local midnight (full calendar day)
-- RESOLUTION:  after WU/HKO/NOAA finalizes (~2h after local midnight) + Polymarket UMA
--
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
    raw_market_json         TEXT,            -- raw CLOB market payload for audit/replay
    game_start_time_utc     TEXT,            -- CLOB field: local midnight UTC = temp window start
    close_time_utc          TEXT,            -- T12:00:00Z on settlement_date = trading close
    accepting_order_ts_utc  TEXT,            -- CLOB accepting_order_timestamp = market creation
    neg_risk_market_id      TEXT,            -- CLOB neg_risk_market_id — groups all city/date buckets
    neg_risk_request_id     TEXT,            -- CLOB neg_risk_request_id — UMA identifier
    first_seen_utc          TEXT,            -- when discover_markets.py first found this market
    settlement_value_proxy  REAL,            -- Q2: normalized proxy source value in settlement_unit
    settlement_value_final  REAL,            -- Q2: final Polymarket/UMA resolved value/outcome
    proxy_outcome           TEXT,            -- YES | NO under settlement_value_proxy
    settlement_source       TEXT,            -- which source was used for proxy settlement
    settlement_rounding_rule TEXT,           -- 'round' | 'floor' | 'ceiling' | unknown
                                             -- source reports 21.7C; bucket is integer 22C
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
│   ├── settle_markets.py       # Runs after source-specific finalization; applies
│   │                           # settlement_value_proxy to bucket proxy_outcome
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

Canonical derivation — use `game_start_time_utc`, not `close_time_utc` (store as a comment in `discover_markets.py`):
```python
# PRIMARY: derive settlement_date from game_start_time_utc (local midnight = start of day)
# game_start_time is always stored as the UTC moment of local midnight for the measured day.
# Converting it to local time and extracting the date is unambiguous for all cities and DST states.
from zoneinfo import ZoneInfo
game_local = datetime.fromisoformat(game_start_time_utc).astimezone(ZoneInfo(station_tz))
settlement_date = game_local.date().isoformat()  # always the local date being measured
```

Do NOT derive `settlement_date` from `close_time_utc`. The midnight-check workaround
(`if close_local.time() == midnight: subtract 1 day`) fails during Wellington's daylight
saving (NZDT, UTC+13): `12:00 UTC → 01:00 NZDT`, which is not midnight, so no subtraction
occurs and the stored date is one day too late. `game_start_time_utc` is the correct and
DST-safe primary source for settlement_date derivation — it is always the UTC instant of
local midnight for the day being measured, regardless of timezone offset or DST state.

### Q2: actual temperature resolution

**The settlement lookup**: `MAX(temp_c) WHERE station=? AND local_date=?`

`settle_markets.py` runs ~2h after local midnight (when the local calendar day is
complete and the named settlement source has had time to finalize). The daily high covers the full local calendar
day — midnight to midnight local — matching the rules language: "highest temperature
recorded for all times on this day."

Do NOT add `AND observed_utc <= close_time_utc`. Trading closure at 12:00 UTC does
not end the temperature measurement window. Polymarket resolves after local midnight
using the finalized full-day reading from the market's named source.

The `local_date` to query — derive from `game_start_time_utc` (same as `settlement_date`):
```python
from zoneinfo import ZoneInfo
game_local = datetime.fromisoformat(game_start_time_utc).astimezone(ZoneInfo(station_tz))
settlement_day = game_local.date().isoformat()  # DST-safe; correct for Wellington NZDT
```

**The rounding problem**: Settlement sources can report fractional degrees (e.g. WU 21.7°C,
HKO 33.2°C). Market buckets are whole integers. Polymarket applies a rounding rule at
settlement. If the source reports 21.7°C and the market resolves to whole-degree buckets,
the Q2 CASE expression `bucket_type = 'exact' AND settlement_value_proxy = 21.7` will
never match any bucket. Every bucket returns NO, which is wrong.

**Critical distinction — `settlement_rounding_rule` has two concepts, not one:**

- `source_precision`: how the settlement source natively publishes its value (e.g. HKO
  publishes one decimal place; WU may publish one decimal or whole degrees depending on
  station). This is a property of the source.
- `resolution_rounding_rule`: how Polymarket/UMA rounds the source value to compare against
  integer bucket thresholds. This is a property of the market rules. Valid values: `'round'`
  (standard half-up), `'floor'`, `'ceiling'`, `'unknown'`.

These are stored in a single field `settlement_rounding_rule` today. This must be treated
carefully: `'one_decimal'` (returned by `_rounding_rule()` for HKO) describes source
precision, not the resolution rounding operation. For HKO markets that resolve to
whole-degree buckets, the resolution operation is still `'round'` (or whatever the rules
say). Do not use `'one_decimal'` as the rounding function — it will leave 33.2 unrounded
and the Q2 comparison fails.

Fix: parse and store both values separately, or at minimum ensure `settlement_rounding_rule`
always stores the resolution operation (`'round'` / `'floor'` / `'ceiling'` / `'unknown'`),
not the source's native precision. Apply the resolution rounding when writing
`settlement_value_proxy` so the stored value is already in the form the market uses.
Until the resolution rule is confirmed, flag proxy settlements as `resolution_status =
'proxy_only'` and exclude them from strategy conclusions (see safe query patterns).

**The unit/range problem**: Not all markets are integer Celsius exact buckets. US markets
can use Fahrenheit range buckets (`68-69°F`), and Hong Kong rules can use one decimal
Celsius. Resolution logic must normalize source values into `settlement_unit`, then compare
against `lower_temp`/`upper_temp` in `bucket_unit`. If units differ, convert explicitly and
store both raw and normalized values.

### Q3: order book timeline

**`hours_to_close` arithmetic**: computed as `(close_time_utc - ts_utc) / 3600`.
Both are UTC strings. Subtraction is timezone-safe. ✓

**`snapshot_label` assignment**: bin labels use `hours_to_close` comparison only — no
calendar date involved. Timezone-safe. ✓ The `open` label additionally requires
`first_seen_utc` to be loaded per market at collection time.

**Known code bug — `_load_active_markets` does not fetch `first_seen_utc`**: the current
implementation of `log_orderbooks.py` does not include `first_seen_utc` in the market dict,
so the `open` label can never be assigned. Fix: add `first_seen_utc` to the SELECT in
`_load_active_markets`, then check it when assigning labels in `_snapshot_market`.

**Known code bug — label thresholds do not match this plan**: as of 2026-06-01, the code
uses `[0.5, 1, 2, 4, 8, 12, 24]` as thresholds (T-2h, T-4h, T-8h present; T-3h, T-6h
absent). These must be corrected to `[0.5, 1, 3, 6, 12, 24]` to match the standard intervals
defined here.

**Known code bug — active market filter drops NYC/Miami near UTC midnight**: `_load_active_markets`
uses `settlement_date >= date('now')` where `date('now')` is UTC. For Type C cities (NYC,
Miami), local midnight is 04:00–05:00 UTC next day. After UTC midnight their settlement_date
(local June 1) is less than `date('now')` (UTC June 2), dropping them from monitoring before
their temperature window closes. Fix: use `settlement_date >= date('now', '-1 day')` to
extend the window, and let `close_time_utc` + `hours_to_close` govern post-close labeling.

**Dependency risk**: `close_time_utc` must be populated before `log_orderbooks.py`
runs for a market. If `discover_markets.py` fails or hasn't run yet, `close_time_utc`
is NULL. `hours_to_close` is NULL. No labels are assigned. All raw snapshots are still
stored, but the standard interval view is empty for that market.

Mitigation: `log_orderbooks.py` should log a warning per market where `close_time_utc`
is NULL, and retry discovery before the next collection cycle.

**"Open" label is first-seen, not true market creation**: if `discover_markets.py`
first runs 36 hours before settlement (instead of 48h), the "open" snapshot is at T-36h,
not T-48h. The data is honest — it reflects what we first observed — but queries
that assume "open = T-48h" will be misleading. Always display `first_seen_utc` alongside
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

-- ✓ Correct: derive settlement local date (DST-safe — use game_start_time_utc, not close_time_utc)
-- In Python before querying:
-- game_local = datetime.fromisoformat(game_start_time_utc).astimezone(ZoneInfo(tz))
-- local_date = game_local.date().isoformat()

-- ✓ Correct: Q2 settlement query — exclude unconfirmed rounding
-- Always gate on resolution_status before drawing strategy conclusions:
SELECT * FROM weather_markets
WHERE settlement_date = ?
  AND settlement_value_proxy IS NOT NULL
  AND resolution_status != 'proxy_only'   -- proxy_only = rounding rule unknown; may match wrong bucket
ORDER BY city, lower_temp

-- ✗ Wrong: use proxy_only rows as confirmed settlements
-- settlement_value_proxy may be unrounded (e.g. 27.8 stored when market resolves at 28);
-- Q2 CASE expression will return NO for every bucket — silent bad data, not an error.

-- ✓ Correct: Q1 zero-row check — verify a result is expected before reporting a gap
-- If first_seen_utc is within 48h of close_time_utc, some models may have no forecast yet.
-- Check horizon_hours to distinguish "no forecast existed" from "collection failure":
SELECT mf.model,
       mf.high_c, mf.fetched_utc, mf.model_run_utc, mf.horizon_hours
FROM model_forecasts mf
JOIN weather_markets wm ON mf.station = wm.station
                        AND mf.forecast_date = wm.settlement_date
WHERE wm.condition_id = ?
  AND mf.fetched_utc <= wm.first_seen_utc
  AND wm.first_seen_utc < wm.close_time_utc   -- exclude markets discovered after close
GROUP BY mf.model
HAVING mf.fetched_utc = MAX(mf.fetched_utc)
-- If zero rows: check whether (close_time_utc - first_seen_utc) < model's horizon_hours.
-- If yes: gap is expected (market opened before this model's horizon). Not a collection failure.
-- If no: gap is a collection failure — log it.
```

---

## Testability Matrix

Every planned test must map to fields captured at collection time. If a required field is
missing, the test is not allowed to produce a strategy conclusion.

Tests marked ⛔ **Blocked** cannot produce conclusions until the listed blocker is resolved.
Tests marked ⚠️ **Partial** can run but results will be incomplete.

| Test | Required captured data | Status |
|------|------------------------|--------|
| Resolution source mismatch audit | `weather_markets.rules_text`, `rules_source`, `resolution_source_type`, `settlement_observations.value/unit/raw_payload_json`, `wx_observations.daily_high_c`, `market_resolutions.resolved_outcome/resolved_value` | ⛔ Blocked — WU adapter missing; ~82% of markets have no `settlement_value_proxy` |
| Liquidity / executability filter | `ob_snapshots.yes_bid/yes_ask/no_bid/no_ask`, all size fields, `spread`, `raw_book_json`, `hours_to_close` | ✅ Ready |
| Already priced in | source `observed_utc` or model valid/run time, `fetched_utc`, first `ob_snapshots.ts_utc` after fetch, pre-fetch and post-fetch price deltas | ✅ Ready |
| Data delay / source latency | source valid/publish time where available, system `fetched_utc`, first DB insert/change time, first orderbook snapshot after fetch | ✅ Ready |
| Forecast revision momentum | `model_forecasts.model`, `model_run_utc`, `model_run_is_estimated`, `fetched_utc`, `forecast_date`, `high_c/low_c`, orderbook snapshots after each revision | ⚠️ Partial — only GFS + TAF collecting; ICON/MF/GEM absent |
| Stale observation strategy | `wx_observations.observed_utc/fetched_utc/local_date/daily_high_c`, trading `close_time_utc`, bucket lower/upper/unit, executable orderbook snapshot | ✅ Ready |
| Market-open forecast accuracy | `weather_markets.first_seen_utc`, `model_forecasts.fetched_utc <= first_seen_utc`, opening bucket prices, final settlement value | ⚠️ Partial — `open` snapshot label not yet firing; only GFS for forecasts |
| Forecast consensus vs market | all model forecasts for same station/local_date, opening or selected-time market distribution, final settlement value | ⛔ Blocked — requires ≥3 models; currently only GFS + TAF (partial) |
| Best forecast timing by station | forecast `fetched_utc`, `model_run_utc`, `hours_before_close`, station timezone/local date, final settlement value | ⚠️ Partial — single model limits per-station comparison |
| Dynamic rebalancing | full sequence of forecasts, observations, settlement-source updates, orderbook snapshots, and simulated position events | ⛔ Blocked — WU adapter missing for most cities |
| Negative-risk gaps | `weather_markets.neg_risk_market_id`, bucket lower/upper/unit/type, executable prices/sizes for every bucket in group | ✅ Ready |
| Local-time weather path | `wx_observations.local_hour`, `is_in_peak_window`, station timezone, orderbook repricing after local milestones | ✅ Ready |
| Station reliability | forecast error, proxy/source mismatch rate, intraday volatility, late-day new highs, liquidity/spread/repricing metrics | ⛔ Blocked — needs WU adapter + multiple models + 2+ weeks of data |
| Bucket adjacency / hedge quality | bucket intervals, neg-risk group, executable bid/ask/size, settlement outcome, capital-at-risk model | ⛔ Blocked — WU adapter missing; no confirmed settlement values |

This matrix is the build contract. `scripts/init_db.py` must create every field needed here,
and analysis scripts should fail loudly when required data is absent.

---

## Phase Plan

### Phase 1 — Data collection (in progress)

**Audit status as of 2026-06-01** — what is actually working vs what the plan describes:

| Component | Status | Finding |
|-----------|--------|---------|
| METAR fetch | ✅ Collecting | 17 stations responding; latest rows include observed/fetched UTC and station-local date |
| TAF fetch | ✅ Collecting | 15 TAFs, Seoul TX=31°C confirmed |
| GFS model | ✅ Collecting | 51 latest rows (17 stations × 3 local forecast dates); historical DB now has 99 rows |
| ICON, MF, GEM | ❌ Not yet run | Only GFS fetched so far |
| ECMWF direct | ❌ Not built | Still using open-meteo for ECMWF |
| weather_markets table | ✅ Populated | 187 markets across 17 city/date events; rules/source/unit/token/close fields verified non-null |
| ob_snapshots table | ✅ Populated | Complete 187-market live snapshot saved with sizes, spread, raw books, hours-to-close, and labels |
| city_stations.json | ✅ Enriched | `timezone`, `lat`, `lon`, units, proxy station, and source metadata present for all 17 cities |
| wx_observations schema | ✅ Migrated | `observed_utc`, `fetched_utc`, `local_date`, `local_hour`, raw payload, and local-day highs stored |
| Polymarket close_time_utc | ✅ Confirmed | 12:00 UTC universal — scraped from settled markets |
| rules/source adapters | ⚠️ Partial | HKO CSV and NOAA WRH/Synoptic adapters added; WU and Polymarket/UMA final still pending |
| unit/range parser | ✅ Built | Exact C buckets, Fahrenheit range buckets, and floor/ceiling buckets parsed |
| orderbook depth | ✅ Built | Best prices, best sizes, spread, raw YES/NO books, and timing labels stored |

**Critical findings from audit:**

**1. Schema is now centralized and migrated**
`scripts/init_db.py` is the single schema owner. Legacy local databases may still contain
older compatibility columns such as `ts_utc`, but current writes populate the canonical
analysis fields: `observed_utc`, `fetched_utc`, `local_date`, `local_hour`,
`daily_high_c`, `source`, and `raw_payload_json`.

**2. city_stations.json is now the runtime metadata cache**
It contains `city`, `slug`, `station`, `country`, `lat`, `lon`, `timezone`,
bucket/settlement units, source type/name/URL, WU paths where relevant, and anomaly notes.
`fetch_weather.py` loads this file directly; live market rules still remain authoritative.

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

**Three timestamps from the CLOB API (confirmed from live markets):**

| Field | Value | Meaning |
|-------|-------|---------|
| `game_start_time` | local midnight UTC (e.g. `2026-05-30T04:00:00Z` for NYC EDT) | Temperature window opens — start of local calendar day |
| `end_date_iso` | `T00:00:00Z` date placeholder | Date only — NOT a meaningful time; ignore for timing |
| `accepting_order_timestamp` | ~2 days before settlement | Market creation time — when orders first accepted |

**One timestamp from Gamma/HTML metadata (confirmed):**

| Source | Value | Meaning |
|--------|-------|---------|
| HTML `endDate` / Gamma metadata | `T12:00:00Z` on settlement date | **Trading close** — when positions lock |

**Full confirmed timeline:**

| Event | UTC time | Notes |
|-------|----------|-------|
| Market created | `accepting_order_timestamp` (~T-48h) | Buckets appear in CLOB |
| Temperature window opens | `game_start_time` = local midnight | WU/HKO/NOAA begins tracking for this local date |
| **Trading closes** | **12:00 UTC on settlement_date** | Positions locked; `accepting_orders=False` |
| Temperature window closes | Next local midnight | Full 24h local calendar day complete |
| Source finalizes | ~2h after local midnight | WU/HKO/NOAA publishes confirmed daily high |
| Polymarket resolves | After source finalization | UMA resolver checks source; `closed=True`, prices snap to 0/1 |

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

**Note — `_close_time_from_event` fallback is fragile**: `discover_markets.py` has a
fallback `f"{day.isoformat()}T12:00:00Z"` when `event.get("endDate")` is missing or
malformed. This is correct today (all markets close at 12:00 UTC) but silently wins if
Polymarket ever changes the close time. Add a warning log whenever the fallback fires so
deviations from the confirmed universal close time are immediately visible.

**Fields removed from schema** (were based on wrong model):
- ~~`settlement_window_hours`~~ — removed. Not a meaningful concept given two-clock model.

**4. forecast_date timezone: looks correct by coincidence today**
At 03:03 UTC on June 1, all station UTC dates and local dates happen to match (it is
June 1 everywhere except Pacific/Auckland which was already June 1 local).
This will fail silently when fetches happen near UTC midnight for UTC+ stations.

**Checklist:**
- [x] 17-city pilot identified, with live-rule caveats for HKO/NOAA/Fahrenheit markets
- [x] METAR fetch working — 17 stations responding, temperatures and local-day highs correct
- [x] TAF fetch working — TX/TN parsed for 5 stations, issued/valid timestamps in UTC
- [x] GFS via open-meteo — 51 latest rows confirmed for 17 stations × 3 local forecast dates
- [x] Retry logic + fetch_log logging correctly
- [x] **Fix city_stations.json**: add `timezone`, `lat`, `lon` for all 17 entries;
      update Moscow from EFHK → UUWW; update Hong Kong from ZBAA → VHHH
- [x] **Rules-first discovery**: treat Gamma market rules as authority; refresh source,
      units, precision, bucket type/range, and station/source metadata from live rules
- [x] **Sync fetch_weather.py**: update STATIONS dict to use UUWW (Moscow) and
      VHHH (Hong Kong); load all station metadata from city_stations.json
- [x] **Fix wx_observations schema**: add `observed_utc`, `local_date`, `fetched_utc`;
      store METAR `reportTime` as `observed_utc`; compute `local_date` from that
      using station timezone; keep `fetched_utc` as when system retrieved it
- [x] **Fix daily high query**: use `(station, local_date)` not `DATE(ts_utc)`
- [x] **Polymarket settlement time confirmed**: 12:00 UTC universal for all weather markets
      (scraped from settled Seoul/NYC markets: `end_date_iso = '2026-MM-DDT12:00:00Z'`)
- [x] **Fix open-meteo timezone**: pass `timezone=<station tz>` per model request
      so `forecast_date` is in station's local calendar, not UTC
- [x] Create `scripts/init_db.py` as single schema owner; apply new schema
- [x] `discover_markets.py` refined: populate `weather_markets` table including
      confirmed CLOB fields: `game_start_time_utc`, `neg_risk_market_id`,
      `neg_risk_request_id`, `accepting_order_ts_utc`; set `close_time_utc` from
      Gamma `endDate` (T12:00:00Z); store `rules_source`, `resolution_source_url`,
      `settlement_rounding_rule`, units, bucket ranges, and raw market JSON
- [x] `log_orderbooks.py`: compute `hours_to_close`, assign nullable `snapshot_label`,
      store sizes/depth/spread/raw book JSON
- [x] `settle_markets.py`: apply proxy settlement values to bucket-level `proxy_outcome`

**Open — blocking settlement analysis (must fix before Phase 2):**

- [ ] **⛔ Build `wunderground_daily` adapter** in `fetch_settlement_sources.py` — ~82% of
      markets (London, Paris, Tokyo, Seoul, Beijing, Singapore, Madrid, Munich, Amsterdam,
      Ankara, Wellington, Shenzhen, Guangzhou, NYC, Miami) use WU as settlement source.
      Until this adapter exists, `settlement_value_proxy` is NULL and `proxy_outcome` is
      never populated for these cities. HKO and NOAA adapters are built; WU is the gap.
- [ ] **Fix `settlement_date` derivation in `discover_markets.py`**: use `game_start_time_utc`
      converted to station local timezone — NOT `day.isoformat()` (UTC loop date) and NOT
      the `close_time_utc` midnight-check workaround (fails for Wellington NZDT, UTC+13,
      where 12:00 UTC = 01:00 local, not midnight). See "Analysis Query Correctness" for
      the canonical `game_start_time_utc` derivation.
- [ ] **Fix `settlement_rounding_rule` conflation**: `_rounding_rule()` currently returns
      `'one_decimal'` (source precision) but the schema expects the resolution rounding
      operation (`'round'` / `'floor'` / `'ceiling'`). Fix `_rounding_rule()` to return
      the resolution operation, not the source's native precision. Add a separate note
      or field for source precision if needed. Without this fix, HKO markets with
      `settlement_rounding_rule='one_decimal'` will store unrounded values in
      `settlement_value_proxy` and every Q2 exact-bucket comparison will return NO.
- [ ] **Fix `_snapshot_label` thresholds in `log_orderbooks.py`**: current thresholds are
      `[0.5, 1, 2, 4, 8, 12, 24]` — missing T-3h and T-6h, extra T-2h/T-4h/T-8h. Fix to
      `[0.5, 1, 3, 6, 12, 24]`.
- [ ] **Fix `open` label never firing**: add `first_seen_utc` to `_load_active_markets`
      SELECT; in `_snapshot_market` check if current snapshot is within 5 min of
      `first_seen_utc` and no prior `open` label exists for this `condition_id`.
- [ ] **Fix active market filter for Type C cities**: change `settlement_date >= date('now')`
      to `settlement_date >= date('now', '-1 day')` so NYC/Miami are not dropped from
      monitoring after UTC midnight when their local day (and temperature window) continues.
- [ ] **Add post-close guard in `discover_markets.py`**: if `first_seen_utc >= close_time_utc`
      at discovery time, immediately set `active=0` and log a warning. Prevents post-close
      discoveries from polluting Q1 with misleading "pre-open" forecast rows.
- [ ] **Add unit conversion to obs_mismatch in `neg_risk_scanner.py`**: convert `daily_high_c`
      to `bucket_unit` before comparing against `lower_temp`/`upper_temp`. For NYC and Miami
      (`bucket_unit='F'`), apply `daily_high_c * 9/5 + 32` before the threshold comparison.
- [ ] **Add multi-group neg_risk guard**: `discover_markets.py` warns if multiple
      `neg_risk_market_id` values found for same `(city, settlement_date)`;
      `neg_risk_scanner.py` groups by city+date first, runs cross-group consistency check,
      and uses `alert_type='neg_risk_gap_cross_group'` for inter-group gaps.
- [ ] **Add real-time alert triggering for Type B obs_mismatch**: alerts must fire within
      seconds of METAR insert, not deferred to next poll cycle. The effective last-actionable
      window for Type B cities ends at ~11:30 UTC (the :30 poller before close); deferring
      alert generation to the next scheduled run loses the final 30–60 minutes.

**Open — collection completeness:**

- [ ] **Run ICON, MF, GEM**: only GFS collected so far; 3 of 6 forecast models missing;
      blocks "Forecast consensus vs market" and "Best forecast timing by station" tests
- [ ] ECMWF direct via `ecmwf-opendata` (3h faster than open-meteo mirror)
- [ ] Final Polymarket/UMA reconciliation: write `settlement_value_final`, apply final
      bucket-aware outcome, and compare final vs proxy
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
