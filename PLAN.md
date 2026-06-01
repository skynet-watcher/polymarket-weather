# Polymarket Weather Market Logger — Project Plan

## What This Is

Polymarket runs daily staircase temperature markets for 17 cities:
each day, a set of "Will the highest temperature in [City] be exactly X°C?" markets
settle at the day's official high from a specific Weather Underground airport station.

This project logs:
1. **Order book snapshots** — YES/NO prices on every active temperature bucket, every few minutes
2. **Weather station readings** — actual observed temperature from the settlement station, every 15 minutes
3. **Forecast data** — NWS/open-meteo forecasts for each city at the start of each day

Goal: Find gaps between what the market prices and what the weather actually shows.

---

## Settlement Stations (Weather Underground ICAO codes)

| City        | WU Station | Notes                                      |
|-------------|------------|---------------------------------------------|
| Seoul       | RKSI       | Incheon International Airport               |
| Hong Kong   | ZBAA       | ⚠️ Shows Beijing airport — verify           |
| London      | EGLC       | London City Airport                         |
| Tokyo       | RJTT       | Haneda Airport                              |
| NYC         | KLGA       | LaGuardia Airport                           |
| Paris       | LFPB       | Le Bourget Airport                          |
| Beijing     | ZBAA       | Beijing Capital International               |
| Miami       | KMIA       | Miami International Airport                 |
| Singapore   | WSSS       | Changi Airport                              |
| Madrid      | LEMD       | Barajas Airport                             |
| Moscow      | EFHK       | ⚠️ Shows Helsinki airport — verify          |
| Munich      | EDDM       | Munich Airport                              |
| Amsterdam   | EHAM       | Schiphol Airport                            |
| Ankara      | LTAC       | Ankara Esenboga Airport                     |
| Wellington  | NZWN       | Wellington Airport                          |
| Shenzhen    | ZGSZ       | Shenzhen Bao'an Airport                     |
| Guangzhou   | ZGGG       | Guangzhou Baiyun Airport                    |

---

## The Opportunity (Hypothesis)

Same structure as the BTC staircase — but instead of BTC price, it's temperature.

**Potential edges:**
1. **Data lag**: WU updates station readings with a delay. If the current observed
   high is already 26°C and the market still prices 26°C YES at 40¢, that's stale.
2. **Forecast accuracy**: NWS/open-meteo forecasts often beat crowd wisdom on
   temperature range. If forecast says 28°C and market prices 28°C at 20¢, that's
   an entry.
3. **Neg-risk gaps**: Same as BTC — "above 25°C" YES ≠ sum of constituent buckets.
   These should appear when LP repricing lags after a temperature reading update.
4. **End-of-day convergence**: As the day ends and the high is locked in, remaining
   buckets collapse to 0 or 1. Buying NO on clearly-wrong buckets is free money in
   the last 30-60 minutes.

---

## Architecture

```
polymarket-weather/
├── scripts/
│   ├── discover_markets.py     # Find all active weather markets, build city map
│   ├── log_orderbooks.py       # Poll CLOB every 2 min, store snapshots
│   ├── fetch_weather.py        # Poll WU stations every 15 min
│   └── neg_risk_scanner.py     # Same as BTC version but for temperature
├── app/
│   └── jobs/
│       └── weather_monitor.py  # Unified loop: poll + analyze + alert
├── data/
│   └── city_stations.json      # City → WU station mapping
├── logs/
│   └── research/
├── weather.db                  # SQLite: markets + snapshots + observations
└── PLAN.md
```

---

## Database Schema

```sql
-- One row per weather market (refreshed daily)
CREATE TABLE weather_markets (
    id              TEXT PRIMARY KEY,     -- Polymarket condition_id
    city            TEXT NOT NULL,
    station         TEXT NOT NULL,        -- WU ICAO code
    settlement_date TEXT NOT NULL,        -- YYYY-MM-DD local
    bucket_type     TEXT NOT NULL,        -- 'exact', 'above', 'below'
    temp_c          REAL,                 -- temperature for this bucket
    yes_token_id    TEXT,
    no_token_id     TEXT,
    active          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Orderbook snapshot every ~2 minutes per market
CREATE TABLE ob_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    yes_bid         REAL,
    yes_ask         REAL,
    no_bid          REAL,
    no_ask          REAL,
    yes_mid         REAL
);

-- Weather station readings every ~15 minutes
CREATE TABLE wx_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    temp_c          REAL,
    daily_high_c    REAL,                -- running high for the day
    source          TEXT DEFAULT 'wunderground'
);

-- Open-meteo forecast at start of each day
CREATE TABLE wx_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    city            TEXT NOT NULL,
    forecast_date   TEXT NOT NULL,
    fetched_at_utc  TEXT NOT NULL,
    high_c          REAL,                -- forecast daily high
    low_c           REAL,
    precip_mm       REAL,
    source          TEXT DEFAULT 'open-meteo'
);

-- Flagged gaps (neg-risk or price vs observation)
CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT NOT NULL,
    city            TEXT NOT NULL,
    alert_type      TEXT NOT NULL,       -- 'neg_risk_gap', 'obs_mismatch', 'convergence'
    detail_json     TEXT
);
```

---

## Phase Plan

### Phase 1 — Data collection (Week 1)
- [x] Discover all active markets via Polymarket website scraping
- [ ] Map cities to WU stations (done above — verify anomalies)
- [ ] `discover_markets.py`: daily job to refresh market list + condition IDs
- [ ] `log_orderbooks.py`: poll CLOB every 2 min, store in `ob_snapshots`
- [ ] `fetch_weather.py`: poll WU (or open-meteo) every 15 min, store in `wx_observations`
- [ ] `fetch_forecasts.py`: pull open-meteo forecast each morning

### Phase 2 — Gap detection (Week 2)
- [ ] `neg_risk_scanner.py`: check "above X°C" vs sum of exact buckets
- [ ] Observation vs market mismatch: if current high > 27°C and YES@27 > 5¢, flag it
- [ ] End-of-day convergence: last 60 min, scan for obvious NO buys

### Phase 3 — Analysis (Week 3+)
- [ ] How often do neg-risk gaps appear in temperature markets?
- [ ] How long do gaps persist vs BTC markets?
- [ ] Does forecast accuracy create systematic mispricings?
- [ ] Are settlement anomalies (HK→Beijing, Moscow→Helsinki) tradeable?

---

## Key Questions to Answer

1. How often do neg-risk gaps appear (vs 5/day on BTC)?
2. How long do they last — are they exploitable without API execution?
3. Does the WU data lag create a tradeable window like the BTC repricing lag?
4. Are the Hong Kong (ZBAA) and Moscow (EFHK) station anomalies real errors?
5. Do 19 cities × ~10 buckets × 2 days ahead = ~380 markets create enough flow?
