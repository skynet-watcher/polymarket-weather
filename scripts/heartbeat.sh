#!/bin/bash
# Polymarket weather — heartbeat runner
# Called by cron; routes to the right test suite based on $1
#
# Usage (set by cron, not called directly):
#   heartbeat.sh offline    — schema + logic tests, no network
#   heartbeat.sh network    — includes live METAR + discovery calls
#   heartbeat.sh report     — prints test-readiness summary to stdout

set -euo pipefail

REPO="/Users/ericjacobsen/Vibe Code/polymarket-weather"
PYTHON="python3"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"

DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MODE="${1:-offline}"
LOG_FILE="$LOG_DIR/heartbeat_${MODE}_$(date -u +%Y%m%d).log"

echo "[$DATE] heartbeat $MODE starting" | tee -a "$LOG_FILE"

case "$MODE" in

  offline)
    # Group 1–7 offline tests — no network required
    # Run daily to catch schema drift and logic regressions
    $PYTHON "$REPO/scripts/run_tests.py" 2>&1 | tee -a "$LOG_FILE"
    EXIT=${PIPESTATUS[0]}
    if [ $EXIT -ne 0 ]; then
      echo "[$DATE] ❌ OFFLINE TESTS FAILED — check $LOG_FILE" | tee -a "$LOG_FILE"
    else
      echo "[$DATE] ✅ offline tests passed" | tee -a "$LOG_FILE"
    fi
    exit $EXIT
    ;;

  network)
    # Full test suite including live API calls (P-05 discovery, P-09 METAR)
    # Run 4× per day to catch API changes and station coverage issues
    $PYTHON "$REPO/scripts/run_tests.py" --network 2>&1 | tee -a "$LOG_FILE"
    EXIT=${PIPESTATUS[0]}
    if [ $EXIT -ne 0 ]; then
      echo "[$DATE] ❌ NETWORK TESTS FAILED — check $LOG_FILE" | tee -a "$LOG_FILE"
    else
      echo "[$DATE] ✅ network tests passed" | tee -a "$LOG_FILE"
    fi
    exit $EXIT
    ;;

  report)
    # Weekly readiness report — which analytical tests can now run
    # based on data available in the DB
    echo "=== Polymarket weather — test readiness $(date -u) ===" | tee -a "$LOG_FILE"
    $PYTHON - <<'PYEOF' 2>&1 | tee -a "$LOG_FILE"
import sqlite3, os, datetime as dt
from zoneinfo import ZoneInfo

REPO = "/Users/ericjacobsen/Vibe Code/polymarket-weather"
DB   = os.path.join(REPO, "weather.db")

if not os.path.exists(DB):
    print("  weather.db not found — no data collected yet")
    raise SystemExit(0)

conn = sqlite3.connect(DB)
now  = dt.datetime.now(dt.timezone.utc)

def q(sql, *a):
    return conn.execute(sql, a).fetchone()[0]

markets      = q("SELECT COUNT(*) FROM weather_markets WHERE active=1")
obs          = q("SELECT COUNT(*) FROM wx_observations")
snapshots    = q("SELECT COUNT(*) FROM ob_snapshots")
models       = q("SELECT COUNT(DISTINCT model) FROM model_forecasts")
settled      = q("SELECT COUNT(*) FROM weather_markets WHERE proxy_outcome IS NOT NULL")
days_obs     = q("SELECT COUNT(DISTINCT local_date) FROM wx_observations")
cities_obs   = q("SELECT COUNT(DISTINCT city) FROM wx_observations")
open_alerts  = q("SELECT COUNT(*) FROM alerts WHERE status='open'")

print(f"\n  Database: {DB}")
print(f"  Active markets:     {markets}")
print(f"  METAR observations: {obs} ({cities_obs} cities, {days_obs} days)")
print(f"  Order book snaps:   {snapshots}")
print(f"  Forecast models:    {models} (need ≥3 for consensus tests)")
print(f"  Settled markets:    {settled}")
print(f"  Open alerts:        {open_alerts}")

print("\n  Analytical test readiness:")

tests = [
    ("Test 8  Liquidity filter",         snapshots > 500,           "need ob_snapshots"),
    ("Test 12 Already priced in",        obs > 500,                 "need wx_observations"),
    ("Test 14 Source latency",           obs > 200,                 "need wx_observations"),
    ("Test 10 Local-time weather path",  obs > 500,                 "need wx_observations"),
    ("Test 1  Stale observation",        obs > 500 and settled > 0, "need obs + settlement"),
    ("Test 6  Neg-risk gaps",            snapshots > 500,           "need ob_snapshots"),
    ("Test 7  Resolution mismatch",      settled > 50,              "need ≥50 settled markets"),
    ("Test 2  Market-open accuracy",     models >= 1 and days_obs >= 3, "need forecasts + 3 days"),
    ("Test 9  Forecast revision",        models >= 1 and days_obs >= 7, "need 7 days forecasts"),
    ("Test 3  Forecast consensus",       models >= 3 and days_obs >= 7, "need ≥3 models + 7 days"),
    ("Test 4  Best forecast timing",     models >= 3 and days_obs >= 14,"need ≥3 models + 14 days"),
    ("Test 5  Dynamic rebalancing",      settled >= 100,            "need ≥100 settled"),
    ("Test 11 Station reliability",      days_obs >= 14 and settled > 50,"need 14 days + settlement"),
    ("Test 13 Bucket adjacency",         settled >= 50,             "need ≥50 settled"),
]

for name, ready, blocker in tests:
    icon = "✅" if ready else "⛔"
    note = "" if ready else f"  [{blocker}]"
    print(f"    {icon}  {name}{note}")

conn.close()
PYEOF
    ;;

  *)
    echo "Unknown mode: $MODE (use offline, network, or report)"
    exit 1
    ;;
esac
