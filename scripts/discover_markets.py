"""
Rules-first discovery for Polymarket weather markets.

Uses Gamma event payloads as the source of market rules and CLOB market payloads
as the source of token IDs, neg-risk grouping, and market timing fields.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("discover")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "weather.db")
STATIONS_F = os.path.join(REPO_ROOT, "data", "city_stations.json")
GAMMA_EVENT = "https://gamma-api.polymarket.com/events/slug"
CLOB_BASE = "https://clob.polymarket.com"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_cities() -> list[dict]:
    with open(STATIONS_F) as f:
        return json.load(f)["cities"]


def _date_slug(day: dt.date) -> str:
    return day.strftime("%B-%-d-%Y").lower()


def _event_slug(city_slug: str, day: dt.date) -> str:
    return f"highest-temperature-in-{city_slug}-on-{_date_slug(day)}"


def _source_type(description: str, source_url: str | None) -> str:
    blob = f"{description or ''} {source_url or ''}".lower()
    if "weather.gov.hk" in blob or "hong kong observatory" in blob:
        return "hong_kong_observatory_daily"
    if "weather.gov/wrh/timeseries" in blob or "noaa" in blob:
        return "noaa_wrh_timeseries"
    if "wunderground.com" in blob or "wunderground" in blob:
        return "wunderground_daily"
    return "unknown"


def _rules_source(description: str, source_type: str) -> str:
    if source_type == "hong_kong_observatory_daily":
        return "Hong Kong Observatory"
    if source_type == "noaa_wrh_timeseries":
        return "NOAA"
    if source_type == "wunderground_daily":
        return "Wunderground"
    m = re.search(r"resolution source .*? from ([^,.]+)", description or "", re.I)
    return m.group(1).strip() if m else "unknown"


def _unit_from_text(text: str, fallback: str) -> str:
    if "°F" in text or "degrees Fahrenheit" in text:
        return "F"
    if "°C" in text or "degrees Celsius" in text or "deg. C" in text:
        return "C"
    return fallback


def _rounding_rule(description: str) -> str:
    text = (description or "").lower()
    if "one decimal" in text or "to one decimal place" in text:
        return "one_decimal"
    if "whole degrees" in text:
        return "whole_degree"
    return "unknown"


def _parse_bucket(question: str, default_unit: str) -> tuple[str, float | None, float | None, str]:
    unit = _unit_from_text(question, default_unit)
    q = question.replace(",", "")

    m = re.search(r"between\s+(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*°?[CF]", q, re.I)
    if m:
        return "range", float(m.group(1)), float(m.group(2)), unit

    m = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?[CF]?\s+or below", q, re.I)
    if m:
        x = float(m.group(1))
        return "below_eq", None, x, unit

    m = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?[CF]?\s+or higher", q, re.I)
    if m:
        x = float(m.group(1))
        return "above_eq", x, None, unit

    m = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?[CF]?\s+on", q, re.I)
    if m:
        x = float(m.group(1))
        return "exact", x, x, unit

    return "unknown", None, None, unit


def _token_ids(market: dict, clob: dict | None) -> tuple[str | None, str | None]:
    tokens = (clob or {}).get("tokens") or []
    if tokens:
        yes = next((t.get("token_id") for t in tokens if t.get("outcome") == "Yes"), None)
        no = next((t.get("token_id") for t in tokens if t.get("outcome") == "No"), None)
        return yes, no

    raw_ids = market.get("clobTokenIds")
    raw_outcomes = market.get("outcomes")
    try:
        ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        pairs = dict(zip(outcomes or [], ids or []))
        return pairs.get("Yes"), pairs.get("No")
    except Exception:
        return None, None


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def _fetch_clob(client: httpx.AsyncClient, condition_id: str) -> dict | None:
    try:
        return await _fetch_json(client, f"{CLOB_BASE}/markets/{condition_id}")
    except Exception as e:
        log.warning("CLOB %s: %s", condition_id[:10], e)
        return None


def _close_time_from_event(event: dict, day: dt.date) -> str:
    end = event.get("endDate")
    if end and "T" in end:
        return end.replace("Z", "+00:00").replace("+00:00", "Z")
    return f"{day.isoformat()}T12:00:00Z"


def _upsert_market(
    conn: sqlite3.Connection,
    city: dict,
    event_slug: str,
    day: dt.date,
    event: dict,
    market: dict,
    clob: dict | None,
    first_seen_utc: str,
) -> None:
    description = market.get("description") or market.get("rules") or ""
    question = market.get("question") or (clob or {}).get("question") or ""
    resolution_url = market.get("resolutionSource") or city.get("resolution_source_url")
    source_type = _source_type(description, resolution_url)
    rules_source = _rules_source(description, source_type)
    settlement_unit = _unit_from_text(description, city.get("settlement_unit", "C"))
    bucket_type, lower, upper, bucket_unit = _parse_bucket(question, city.get("bucket_unit", settlement_unit))
    yes_token, no_token = _token_ids(market, clob)
    condition_id = market.get("conditionId") or market.get("condition_id") or (clob or {}).get("condition_id")
    if not condition_id:
        return

    conn.execute("""
        INSERT INTO weather_markets (
            condition_id, event_slug, market_slug, city, station, settlement_date,
            bucket_type, lower_temp, upper_temp, bucket_unit, settlement_unit,
            yes_token_id, no_token_id, question, rules_text, rules_source,
            resolution_source_type, resolution_source_url, raw_market_json,
            game_start_time_utc, close_time_utc, accepting_order_ts_utc,
            neg_risk_market_id, neg_risk_request_id, first_seen_utc,
            settlement_rounding_rule, active
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(condition_id) DO UPDATE SET
            event_slug=excluded.event_slug,
            market_slug=excluded.market_slug,
            city=excluded.city,
            station=excluded.station,
            settlement_date=excluded.settlement_date,
            bucket_type=excluded.bucket_type,
            lower_temp=excluded.lower_temp,
            upper_temp=excluded.upper_temp,
            bucket_unit=excluded.bucket_unit,
            settlement_unit=excluded.settlement_unit,
            yes_token_id=excluded.yes_token_id,
            no_token_id=excluded.no_token_id,
            question=excluded.question,
            rules_text=excluded.rules_text,
            rules_source=excluded.rules_source,
            resolution_source_type=excluded.resolution_source_type,
            resolution_source_url=excluded.resolution_source_url,
            raw_market_json=excluded.raw_market_json,
            game_start_time_utc=excluded.game_start_time_utc,
            close_time_utc=excluded.close_time_utc,
            accepting_order_ts_utc=excluded.accepting_order_ts_utc,
            neg_risk_market_id=excluded.neg_risk_market_id,
            neg_risk_request_id=excluded.neg_risk_request_id,
            first_seen_utc=COALESCE(weather_markets.first_seen_utc, excluded.first_seen_utc),
            settlement_rounding_rule=excluded.settlement_rounding_rule,
            active=1
    """, (
        condition_id,
        event_slug,
        market.get("slug"),
        city["city"],
        city["station"],
        day.isoformat(),
        bucket_type,
        lower,
        upper,
        bucket_unit,
        settlement_unit,
        yes_token,
        no_token,
        question,
        description,
        rules_source,
        source_type,
        resolution_url,
        json.dumps({"gamma": market, "clob": clob}, sort_keys=True),
        (clob or {}).get("game_start_time"),
        _close_time_from_event(event, day),
        (clob or {}).get("accepting_order_timestamp"),
        (clob or {}).get("neg_risk_market_id"),
        (clob or {}).get("neg_risk_request_id"),
        first_seen_utc,
        _rounding_rule(description),
    ))


async def discover(conn: sqlite3.Connection, days_ahead: int = 2) -> int:
    cities = _load_cities()
    first_seen_utc = _now()
    today = dt.datetime.now(dt.timezone.utc).date()
    upserted = 0

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for city in cities:
            for offset in range(days_ahead + 1):
                day = today + dt.timedelta(days=offset)
                event_slug = _event_slug(city["slug"], day)
                try:
                    event = await _fetch_json(client, f"{GAMMA_EVENT}/{event_slug}")
                except Exception as e:
                    log.warning("%s: %s", event_slug, e)
                    continue
                if not event:
                    continue

                markets = event.get("markets") or []
                log.info("%s: %d markets", event_slug, len(markets))
                for market in markets:
                    condition_id = market.get("conditionId")
                    clob = await _fetch_clob(client, condition_id) if condition_id else None
                    _upsert_market(conn, city, event_slug, day, event, market, clob, first_seen_utc)
                    upserted += 1
                    await asyncio.sleep(0.05)
                conn.commit()
                await asyncio.sleep(0.1)

    return upserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-ahead", type=int, default=2)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    n = asyncio.run(discover(conn, args.days_ahead))
    log.info("Upserted %d markets into %s", n, DB_PATH)
    conn.close()
