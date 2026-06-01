# Dashboard Plan — Polymarket Weather

## Purpose

A live read-only dashboard to:
1. Watch model forecasts update at standard intervals (T-48h → T-close)
2. Watch market prices shift across temperature buckets in real time
3. Confirm settlement outcomes as they arrive
4. Sanity-check the data visually — catch collection failures, drift, mismatches

Stack: Python + FastAPI (backend reads from weather.db) + plain HTML/CSS/JS
(no build step, no framework). Refreshes every 60 seconds. Deployed locally.

---

## Screen 1 — City Overview Grid

The landing page. 17 city cards in a 4-column grid. Each card is a
quick-read status for that city's active market day.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  🌡️  Polymarket Weather                          June 2 2026  19:47 UTC    │
│  ● Live — 275 markets · 198 snapshots in last 2min · 17 METAR stations     │
│─────────────────────────────────────────────────────────────────────────────│
│                                                                             │
│  [ All ] [ Type A — settled ] [ Type B — open ] [ Type C — pre-close ]     │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  🟢 SEOUL    │  │  🟢 TOKYO    │  │  🟡 LONDON   │  │  🟡 PARIS    │   │
│  │  Type A      │  │  Type A      │  │  Type B      │  │  Type B      │   │
│  │  Jun 2       │  │  Jun 2       │  │  Jun 2       │  │  Jun 2       │   │
│  │──────────────│  │──────────────│  │──────────────│  │──────────────│   │
│  │  METAR  28°C │  │  METAR  29°C │  │  METAR  19°C │  │  METAR  22°C │   │
│  │  Fcst   30°C │  │  Fcst   31°C │  │  Fcst   21°C │  │  Fcst   23°C │   │
│  │  Market ████ │  │  Market ████ │  │  Market ████ │  │  Market ████ │   │
│  │  29°C ▌0.61  │  │  30°C ▌0.54  │  │  21°C ▌0.48  │  │  22°C ▌0.52  │   │
│  │  T-7h close  │  │  T-7h close  │  │  T-2h close  │  │  T-2h close  │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  ✅ BEIJING  │  │  ✅ SHENZHEN │  │  🔵 NYC      │  │  🔵 MIAMI    │   │
│  │  Type A      │  │  Type A      │  │  Type C      │  │  Type C      │   │
│  │  Jun 1 ✓     │  │  Jun 1 ✓     │  │  Jun 2       │  │  Jun 2       │   │
│  │──────────────│  │──────────────│  │──────────────│  │──────────────│   │
│  │  Settled     │  │  Settled     │  │  CLOSED      │  │  CLOSED      │   │
│  │  ✅ 34°C     │  │  ✅ 32°C     │  │  Fcst  85°F  │  │  Fcst  88°F  │   │
│  │  exact-34    │  │  exact-32    │  │  Awaiting    │  │  Awaiting    │   │
│  │  proxy ✓     │  │  proxy ✓     │  │  settlement  │  │  settlement  │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
│                                                                             │
│  [ ... 9 more cities ... ]                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Card status colours:**
- 🟢 Green — trading open, METAR active
- 🟡 Yellow — last 3h before close (Type B alert window)
- 🔵 Blue — trading closed, awaiting settlement (Type C post-12:00 UTC)
- ✅ Settled — outcome confirmed

**Card "Market" row** shows a mini-bar for the top bucket by price.

---

## Screen 2 — City Detail View

Clicking a city card opens the detail view. This is the primary research screen.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  ← Back    SEOUL · Jun 2 2026 · Type A · RKSI · Asia/Seoul                │
│  Close: 12:00 UTC (21:00 KST) · 7h 13m remaining                          │
│─────────────────────────────────────────────────────────────────────────────│
│                                                                             │
│  ┌─────────────────────────────┐  ┌─────────────────────────────────────┐  │
│  │  FORECAST PANEL             │  │  BUCKET LADDER                      │  │
│  │─────────────────────────────│  │─────────────────────────────────────│  │
│  │  Model   T-48h T-24h T-12h  │  │  °C  Bucket         YES price       │  │
│  │  ─────── ───── ───── ─────  │  │                                     │  │
│  │  GFS      28.1  29.4  30.1  │  │  35+ ≥35°C  ░░░░░░░░░░░░░  0.04   │  │
│  │  ICON     29.5  30.2  30.8  │  │  34  exact  ░░░░░░░░░░░░░  0.06   │  │
│  │  GEM      29.1  29.9  30.4  │  │  33  exact  ░░░░░░░░░░░░░  0.08   │  │
│  │  MF       28.8  29.6  30.0  │  │  32  exact  ░░░░░░░░░░░░░  0.12   │  │
│  │  TAF      ────  30.0  30.0  │  │  31  exact  ░░░░░░░░░░░░░  0.15   │  │
│  │  ─────── ───── ───── ─────  │  │ ▶30  exact  ████████████▌  0.61 ◀ │  │
│  │  Median   29.1  29.9  30.4  │  │  29  exact  ░░░░░░░░░░░░░  0.09   │  │
│  │  METAR    28.4  ─────────── │  │  28  exact  ░░░░░░░░░░░░░  0.04   │  │
│  │  (proxy)                    │  │ ≤27  ≤27°C  ░░░░░░░░░░░░░  0.02   │  │
│  │                             │  │                                     │  │
│  │  ⚠️  Spread: 2.0°C (ICON-GFS) │  │  ◀ = current top bucket           │  │
│  │  Market implied: 30.2°C     │  │  ▶ = forecast median target         │  │
│  │  Consensus vs mkt:  +0.2°C  │  │  Bars = YES price width             │  │
│  └─────────────────────────────┘  └─────────────────────────────────────┘  │
│                                                                             │
│  ── INTRADAY TEMPERATURE PATH ─────────────────────────────────────────────│
│  Daily high (°C) by local hour           RKSI · Jun 2 KST                 │
│                                                                             │
│  32 │                                  ◆ forecast median 30.4             │
│  31 │                         ◆        ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─       │
│  30 │                  ╭──────●        ● running daily high               │
│  29 │           ╭──────╯                                                  │
│  28 │     ╭─────╯                                                         │
│  27 │─────╯                                                               │
│     └──────────────────────────────────────────────────────────────        │
│     00  02  04  06  08  10  12  14  16  18  20  22  (KST)                │
│                             ↑ now (13:47 KST)                             │
│                                                                             │
│  ── PRICE TIMELINE ────────────────────────────────────────────────────────│
│  YES price of each bucket over the last 48h                                │
│                                                                             │
│       0.8 ┤                                      ╭──────╮                 │
│  30°C 0.6 ┤──────────────────────────────────────╯      ╰─────────────   │
│       0.4 ┤                                                                │
│  29°C 0.3 ┤──────────────────────────────╮                                │
│       0.2 ┤                              ╰──────────────────────────────  │
│  31°C 0.2 ┤────────────────────────────────────────╮                     │
│       0.1 ┤                                        ╰──────────────────── │
│           └────────────────────────────────────────────────────────        │
│           T-48h      T-24h      T-12h      T-6h   T-3h  T-1h  now        │
│            ↑                                                               │
│            open                                                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Bucket Ladder logic:**
- Each row is a temperature bucket, ordered hot → cold
- Bar width = YES price (0 to 1)
- The widest bar = market's favoured bucket
- The ▶ marker = forecast median
- Gap between ▶ and widest bar = potential edge signal

**Intraday Temperature Path:**
- Running daily high (°C) from METAR readings through the local day
- Dotted horizontal line = forecast median
- When the running high crosses a bucket boundary → bucket becomes impossible

**Price Timeline:**
- Shows how each bucket's YES price has moved over the 48h trading window
- Standard interval ticks marked (T-48h, T-24h, T-12h, T-6h, T-3h, T-1h)
- Colour per bucket (top 3-4 buckets, others greyed out)

---

## Screen 3 — Settlement Confirmation View

Shown when a market has settled (proxy_outcome or confirmed resolution).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  ← Back    SEOUL · Jun 1 2026 · SETTLED                                   │
│─────────────────────────────────────────────────────────────────────────────│
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                      ✅  RESOLVED: 31°C                               │  │
│  │                      exact-31 bucket · source: WU (Synoptic proxy)   │  │
│  │                      settled_at: Jun 1 17:23 UTC · proxy_only        │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ── FORECAST ACCURACY ─────────────────────────────────────────────────────│
│                                                                             │
│  Model      T-48h   T-24h   T-12h   T-6h   T-3h    Error   Direction     │
│  ─────────  ──────  ──────  ──────  ─────  ─────  ───────  ─────────     │
│  GFS        28.1    29.4    30.1    30.8   31.1    +0.1°C   ✅ correct    │
│  ICON       29.5    30.2    30.8    31.2   31.4    +0.4°C   ✅ correct    │
│  GEM        29.1    29.9    30.4    31.0   31.2    +0.2°C   ✅ correct    │
│  MF         28.8    29.6    30.0    30.5   31.0    -0.0°C   ✅ correct    │
│  TAF        ────    30.0    30.5    31.0   31.0    -0.0°C   ✅ correct    │
│  Consensus  29.1    29.9    30.4    30.9   31.1    +0.1°C   ✅ correct    │
│  METAR prx  28.3    30.1    31.0    31.0   31.0    +0.0°C   ✅ correct    │
│                                                                             │
│  ── MARKET PRICE vs OUTCOME ───────────────────────────────────────────────│
│                                                                             │
│  Bucket   Open(T-48h)  T-24h   T-12h   T-6h   Close   Outcome  P&L hint  │
│  ───────  ───────────  ──────  ──────  ─────  ──────  ───────  ────────  │
│  ≥35°C    0.02         0.02    0.02    0.02   0.01    NO        N/A       │
│  34°C     0.04         0.04    0.03    0.03   0.02    NO        N/A       │
│  33°C     0.07         0.07    0.06    0.05   0.03    NO        N/A       │
│  32°C     0.12         0.13    0.12    0.09   0.04    NO        N/A       │
│  ▶31°C    0.24         0.32    0.45    0.62   0.78   YES ✅     +buy@open │
│  30°C     0.31         0.25    0.18    0.12   0.08    NO       -buy@open  │
│  29°C     0.12         0.10    0.08    0.06   0.04    NO        N/A       │
│  ≤28°C    0.08         0.07    0.06    0.02   0.01    NO        N/A       │
│                                                                             │
│  ── DATA QUALITY CHECK ────────────────────────────────────────────────────│
│  ✅ Settlement source:  WU Synoptic proxy (23.0°C raw → round → 31°C)     │
│  ✅ Resolved outcome:   exact-31 YES (CLOB price → 1.00 confirmed)        │
│  ✅ Proxy vs outcome:   match (proxy=31°C = resolved bucket=31°C)         │
│  ✅ METAR peak:         31.0°C at 14:30 KST — consistent with settlement  │
│  ✅ Forecasts:          5/5 models within 1°C of outcome at T-3h          │
│  ⚠️  Note:              proxy_only — WU API not yet confirmed              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Screen 4 — Data Health Panel

Accessible from a top-nav "Health" link. For sanity-checking collection.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Data Health · Jun 2 2026 19:47 UTC                                        │
│─────────────────────────────────────────────────────────────────────────────│
│                                                                             │
│  ── COLLECTION STATUS ──────────────────────────────────────────────────── │
│                                                                             │
│  Source         Last fetch    Records/day   Status                         │
│  ─────────────  ──────────── ─────────────  ──────                        │
│  METAR (17 stn) 19:30 UTC     48 fetches    ✅ 17/17 stations             │
│  TAF            18:30 UTC     4 fetches     ✅ 17 TAFs (5 with TX/TN)     │
│  GFS            16:02 UTC     4 fetches     ✅ 51 rows (17×3 dates)       │
│  ICON           ─────────     not started   ❌ 0 rows                     │
│  MF (ARPEGE)    ─────────     not started   ❌ 0 rows                     │
│  GEM            ─────────     not started   ❌ 0 rows                     │
│  Order books    19:46 UTC     720/day       ✅ 198 snapshots (2min)       │
│  Discovery      00:05 UTC     1/day         ✅ 275 active markets         │
│  Settlement src 18:00 UTC     12/day        ⚠️  2/17 cities (HKO+NOAA)   │
│                                                                             │
│  ── STATION COVERAGE ───────────────────────────────────────────────────── │
│  Last METAR by station (most recent observed_utc)                          │
│                                                                             │
│  RKSI  Seoul      ✅ 19:30    VHHH  Hong Kong  ✅ 19:30                  │
│  EGLC  London     ✅ 19:00    LFPB  Paris      ✅ 19:00                  │
│  RJTT  Tokyo      ✅ 19:30    KLGA  NYC        ✅ 19:00                  │
│  ZBAA  Beijing    ✅ 19:30    KMIA  Miami      ✅ 19:00                  │
│  WSSS  Singapore  ✅ 19:30    LEMD  Madrid     ✅ 19:00                  │
│  UUWW  Moscow     ✅ 19:00    EDDM  Munich     ✅ 19:00                  │
│  EHAM  Amsterdam  ✅ 19:00    LTAC  Ankara     ✅ 19:00                  │
│  NZWN  Wellington ✅ 07:30    ZGSZ  Shenzhen   ✅ 19:30                  │
│  ZGGG  Guangzhou  ✅ 19:30                                                │
│                                                                             │
│  ── OPEN ALERTS ────────────────────────────────────────────────────────── │
│  Type                  City        Since       Detail                      │
│  ──────────────────────────────────────────────────────────────────────── │
│  neg_risk_gap_forward  Seoul       19:32 UTC   >30°C bid=0.48 sum=0.44   │
│                                                gap=+0.04 > 2¢ threshold   │
│  obs_mismatch          Tokyo       18:55 UTC   daily_high=29°C invalidates │
│                                                exact-27 YES ask=0.07      │
│  (no other open alerts)                                                    │
│                                                                             │
│  ── SETTLEMENT INTEGRITY ───────────────────────────────────────────────── │
│  Jun 1 settlements                                                         │
│  Seoul    ✅ 1 YES (31°C) · proxy=31°C match                              │
│  Tokyo    ✅ 1 YES (29°C) · proxy=29°C match                              │
│  Beijing  ✅ 1 YES (34°C) · proxy=34°C match                              │
│  Shenzhen ✅ 1 YES (32°C) · proxy=32°C match                              │
│  HK       ✅ 1 YES (31.2°C) · HKO=31.2°C match                           │
│  Moscow   ✅ 1 YES (20°C) · NOAA=20°C match                               │
│  NYC      ⏳ not yet settled (local midnight UTC+4 = 04:00 UTC Jun 2)     │
│  Miami    ⏳ not yet settled                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Navigation Structure

```
┌──────────────────────────────────────────────────────────┐
│  🌡️  Polymarket Weather  │ Overview │ Health │ Alerts 🔴2 │
└──────────────────────────────────────────────────────────┘
         │
         ├── Overview (Screen 1) — city grid, click to drill down
         │       └── City Detail (Screen 2) — forecast + bucket + METAR
         │               └── Settlement (Screen 3) — outcome + accuracy
         │
         ├── Health (Screen 4) — collection status, station coverage
         │
         └── Alerts — live feed of open alerts (neg-risk gaps, obs mismatch)
```

---

## Key Design Decisions

**Why no charts library?**
Plain HTML + CSS bars for the bucket ladder — fast, no dependencies,
works in any browser. The price timeline is the one place a lightweight
chart library (Chart.js CDN, no build step) makes sense.

**Refresh cadence:**
- City cards and health panel: auto-refresh every 60s
- City detail intraday temp: auto-refresh every 5min (METAR cadence)
- Settlement outcomes: poll every 2min (matches order book logger)

**Settlement confirmation UX:**
When a market settles, the city card flips from its colour state to ✅ and
shows the resolved bucket. The detail view adds a green banner and the
forecast accuracy table. No action required — just observe.

**Sanity-check design principle:**
Every number shown must trace to a specific DB field. The health panel
shows the last observed_utc per station so collection gaps are immediately
visible. The settlement integrity table shows proxy vs resolved bucket for
every settled market — a mismatch is a data quality flag, not a trade signal.

---

## Implementation Plan

### Phase 1 — Read-only local server (build this first)
- `scripts/dashboard.py` — FastAPI app reading from weather.db
- `templates/` — HTML templates (Jinja2, no build step)
- Screens 1, 2, 4 first (overview, city detail, health)
- Chart.js CDN for the price timeline and intraday temp charts

### Phase 2 — Settlement view
- Screen 3 populated when `proxy_outcome IS NOT NULL`
- Forecast accuracy table joined from `model_forecasts`

### Phase 3 — Alerts tab
- Live feed from `alerts` table
- Click-through to city detail at the flagged timestamp

### Start command
```bash
python scripts/dashboard.py
# → http://localhost:8000
```

All data read from `weather.db`. No writes. No auth needed for local use.
