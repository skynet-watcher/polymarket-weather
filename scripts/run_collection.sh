#!/bin/bash
# Start all collection loops in parallel background processes.
# Run from repo root. Logs go to logs/collection_*.log
#
# Usage:
#   bash scripts/run_collection.sh          # start all
#   bash scripts/run_collection.sh --stop   # kill all collection processes

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$REPO/logs"
mkdir -p "$LOG"
PYTHON=python3

if [[ "${1:-}" == "--stop" ]]; then
    echo "Stopping collection processes..."
    pkill -f "fetch_weather.py"     2>/dev/null && echo "  stopped fetch_weather"    || true
    pkill -f "log_orderbooks.py"    2>/dev/null && echo "  stopped log_orderbooks"   || true
    pkill -f "discover_markets.py"  2>/dev/null && echo "  stopped discover_markets" || true
    pkill -f "fetch_settlement"     2>/dev/null && echo "  stopped settlement_sources" || true
    pkill -f "settle_markets.py"    2>/dev/null && echo "  stopped settle_markets"   || true
    echo "Done."
    exit 0
fi

echo "=== Starting collection $(date -u) ==="

# 1. Run discovery once now, then cron handles daily repeats
echo "Running initial discovery..."
$PYTHON "$REPO/scripts/discover_markets.py" --days-ahead 2 \
    >> "$LOG/collection_discover.log" 2>&1
echo "  Discovery complete"

# 2. Weather fetch loop (METAR every 30min + forecast models)
echo "Starting fetch_weather.py --loop ..."
nohup $PYTHON "$REPO/scripts/fetch_weather.py" --loop \
    >> "$LOG/collection_weather.log" 2>&1 &
echo "  PID $! → logs/collection_weather.log"

# 3. Orderbook snapshot loop (every 2 minutes)
echo "Starting log_orderbooks.py ..."
nohup $PYTHON "$REPO/scripts/log_orderbooks.py" \
    >> "$LOG/collection_orderbooks.log" 2>&1 &
echo "  PID $! → logs/collection_orderbooks.log"

echo ""
echo "Collection running. Check status:"
echo "  tail -f $LOG/collection_weather.log"
echo "  tail -f $LOG/collection_orderbooks.log"
echo "  bash scripts/heartbeat.sh report"
echo ""
echo "Stop with: bash scripts/run_collection.sh --stop"
