# Polymarket Weather

This is the active Polymarket project.

The current focus is weather-market analysis: discovering active temperature markets, logging order books, collecting settlement-source weather observations, and studying pricing gaps.

Start here:

- `PLAN.md` — project scope, architecture, and research plan
- `scripts/discover_markets.py` — find active weather markets
- `scripts/log_orderbooks.py` — collect CLOB order book snapshots
- `scripts/fetch_weather.py` — collect weather observations from settlement sources
- `scripts/fetch_settlement_sources.py` — collect source-of-record daily highs where adapters exist
- `scripts/settle_markets.py` — apply proxy settlement values to bucket outcomes
- `scripts/neg_risk_scanner.py` — scan temperature-market pricing gaps

Older non-weather Polymarket work has been archived under:

`/Users/eric/Documents/Codex/_archive/polymarket/`
