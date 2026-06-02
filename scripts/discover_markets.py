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
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("discover")

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH     = os.path.join(REPO_ROOT, "weather.db")
STATIONS_F  = os.path.join(REPO_ROOT, "data", "city_stations.json")
GAMMA_EVENT = "https://gamma-api.polymarket.com/events/slug"
CLOB_BASE   = "https://clob.polymarket.com"

SLUG_ZERO_ALERT_THRESHOLD = 3   # ≥ this many cities returning 0 markets → slug failure alert


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
    """Return the RESOLUTION rounding operation (not source precision).

    This is how Polymarket/UMA rounds the source value to compare against
    integer bucket thresholds — NOT a description of how the source reports values.
    Valid values: 'round' | 'floor' | 'ceiling' | 'unknown'
    """
    text = (description or "").lower()
    # HKO publishes one decimal (e.g. 33.2°C) but market buckets are integers → round
    if "one decimal" in text or "to one decimal place" in text:
        return "round"
    # Sources that publish whole degrees — comparison is direct
    if "whole degrees" in text:
        return "round"
    if "floor" in text or "truncat" in text:
        return "floor"
    if "ceiling" in text or "ceil" in text:
        return "ceiling"
    return "unknown"


def _parse_bucket(question: str, default_unit: str) -> tuple[str, float | None, float | None, str]:
    unit = _unit_from_text(question, default_unit)
    q = question.replace(",", "")

    m = re.search(r"between\s+(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*°?[CF]", q, re.I)
    if m:
        return "range", float(m.group(1)), float(m.group(2)), unit

    m = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?[CF]?\s+or below", q, re.I)
    if m:
        return "below_eq", None, float(m.group(1)), unit

    m = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?[CF]?\s+or higher", q, re.I)
    if m:
        return "above_eq", float(m.group(1)), None, unit

    m = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?[CF]?\s+on", q, re.I)
    if m:
        x = float(m.group(1))
        return "exact", x, x, unit

    return "unknown", None, None, unit


def _token_ids(market: dict, clob: dict | None) -> tuple[str | None, str | None]:
    tokens = (clob or {}).get("tokens") or []
    if tokens:
        yes = next((t.get("token_id") for t in tokens if t.get("outcome") == "Yes"), None)
        no  = next((t.get("token_id") for t in tokens if t.get("outcome") == "No"),  None)
        return yes, no
    raw_ids      = market.get("clobTokenIds")
    raw_outcomes = market.get("outcomes")
    try:
        ids      = json.loads(raw_ids)      if isinstance(raw_ids, str)      else raw_ids
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        pairs    = dict(zip(outcomes or [], ids or []))
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
    """Extract close_time_utc from Gamma endDate.

    Falls back to the confirmed universal 12:00 UTC close when endDate is absent.
    Logs a warning when the fallback fires so deviations from the universal close
    are immediately visible.
    """
    end = event.get("endDate")
    if end and "T" in end:
        return end.replace("Z", "+00:00").replace("+00:00", "Z")
    log.warning("endDate missing or malformed for event on %s — using 12:00 UTC fallback", day)
    return f"{day.isoformat()}T12:00:00Z"


def _settlement_date_from_clob(
    temp_window_start_utc: str | None,
    close_time_utc: str,
    event_slug: str,
    station_tz: str,
) -> str:
    """Derive settlement_date (YYYY-MM-DD in station local time) from temp_window_start_utc.

    PRIMARY path: convert temp_window_start_utc to station local time and extract date.
    temp_window_start_utc = CLOB game_start_time = UTC moment of local midnight for the
    day being measured. This is DST-safe and correct for all cities including Wellington
    NZDT (UTC+13) where the close_time_utc midnight-check approach fails.

    FALLBACK (if temp_window_start_utc is NULL): parse the date from the event slug,
    which is always present and unambiguous. E.g. "highest-temperature-in-seoul-on-june-1-2026"
    → "2026-06-01".
    """
    if temp_window_start_utc:
        try:
            window_local = dt.datetime.fromisoformat(
                temp_window_start_utc.replace("Z", "+00:00")
            ).astimezone(ZoneInfo(station_tz))
            return window_local.date().isoformat()
        except Exception as e:
            log.warning("temp_window_start_utc parse failed (%s): %s", temp_window_start_utc, e)

    # Fallback: parse from event slug (always present)
    m = re.search(r"-on-(\w+)-(\d+)-(\d{4})$", event_slug)
    if m:
        try:
            month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
            return dt.datetime.strptime(
                f"{month_str} {day_str} {year_str}", "%B %d %Y"
            ).date().isoformat()
        except Exception as e:
            log.warning("Slug date parse failed (%s): %s", event_slug, e)

    # Last resort: derive from close_time_utc (may be wrong for Wellington NZDT)
    try:
        close_local = dt.datetime.fromisoformat(
            close_time_utc.replace("Z", "+00:00")
        ).astimezone(ZoneInfo(station_tz))
        if close_local.time() == dt.time(0, 0):
            return (close_local.date() - dt.timedelta(days=1)).isoformat()
        return close_local.date().isoformat()
    except Exception as e:
        log.error("All settlement_date derivation paths failed for %s: %s", event_slug, e)
        raise


def _upsert_market(
    conn: sqlite3.Connection,
    city: dict,
    event_slug: str,
    day: dt.date,
    event: dict,
    market: dict,
    clob: dict | None,
    discovery_source: str = "live_discovery",
) -> None:
    # Stamp first_seen_utc at the moment THIS market is processed, not batch start.
    first_seen_utc = _now()
    is_backfill = discovery_source == "backfill"

    description    = market.get("description") or market.get("rules") or ""
    question       = market.get("question") or (clob or {}).get("question") or ""
    resolution_url = market.get("resolutionSource") or city.get("resolution_source_url")
    source_type    = _source_type(description, resolution_url)
    rules_source   = _rules_source(description, source_type)
    settlement_unit = _unit_from_text(description, city.get("settlement_unit", "C"))
    bucket_type, lower, upper, bucket_unit = _parse_bucket(
        question, city.get("bucket_unit", settlement_unit)
    )
    yes_token, no_token = _token_ids(market, clob)
    condition_id = (
        market.get("conditionId")
        or market.get("condition_id")
        or (clob or {}).get("condition_id")
    )
    if not condition_id:
        return

    # Flag unparseable buckets immediately
    if bucket_type == "unknown":
        log.warning("unparseable_bucket: %s | %s", condition_id[:10], question[:80])
        conn.execute("""
            INSERT INTO alerts(opened_utc,last_seen_utc,city,settlement_date,alert_type,status,detail_json)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
        """, (first_seen_utc, first_seen_utc, city["city"], day.isoformat(),
              "unparseable_bucket", "open",
              json.dumps({"condition_id": condition_id, "question": question})))

    temp_window_start_utc = (clob or {}).get("game_start_time")
    close_time_utc        = _close_time_from_event(event, day)

    # Derive settlement_date from temp_window_start_utc + station timezone (DST-safe)
    station_tz = city.get("timezone", "UTC")
    settlement_date = _settlement_date_from_clob(
        temp_window_start_utc, close_time_utc, event_slug, station_tz
    )

    # Post-close guard: if we're discovering a market after its trading close, mark inactive
    if first_seen_utc >= close_time_utc and not is_backfill:
        log.warning("Post-close discovery: %s (%s) close=%s first_seen=%s — setting active=0",
                    condition_id[:10], city["city"], close_time_utc, first_seen_utc)

    market_start_utc = (clob or {}).get("accepting_order_timestamp")

    conn.execute("""
        INSERT INTO weather_markets (
            condition_id, event_slug, market_slug, city, station, settlement_date,
            bucket_type, lower_temp, upper_temp, bucket_unit, settlement_unit,
            yes_token_id, no_token_id, question, rules_text, rules_source,
            resolution_source_type, resolution_source_url, raw_market_json,
            temp_window_start_utc, close_time_utc, accepting_order_ts_utc,
            market_start_utc, neg_risk_market_id, neg_risk_request_id,
            first_seen_utc, first_seen_source, backfilled_at_utc,
            settlement_rounding_rule, active
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            temp_window_start_utc=excluded.temp_window_start_utc,
            close_time_utc=excluded.close_time_utc,
            accepting_order_ts_utc=excluded.accepting_order_ts_utc,
            market_start_utc=COALESCE(weather_markets.market_start_utc, excluded.market_start_utc),
            neg_risk_market_id=excluded.neg_risk_market_id,
            neg_risk_request_id=excluded.neg_risk_request_id,
            first_seen_utc=COALESCE(weather_markets.first_seen_utc, excluded.first_seen_utc),
            first_seen_source=COALESCE(weather_markets.first_seen_source, excluded.first_seen_source),
            backfilled_at_utc=COALESCE(weather_markets.backfilled_at_utc, excluded.backfilled_at_utc),
            settlement_rounding_rule=excluded.settlement_rounding_rule,
            active=CASE
                WHEN excluded.first_seen_utc >= excluded.close_time_utc THEN 0
                ELSE 1
            END
    """, (
        condition_id,
        event_slug,
        market.get("slug"),
        city["city"],
        city["station"],
        settlement_date,
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
        temp_window_start_utc,
        close_time_utc,
        market_start_utc,
        market_start_utc,
        (clob or {}).get("neg_risk_market_id"),
        (clob or {}).get("neg_risk_request_id"),
        first_seen_utc,
        discovery_source,
        first_seen_utc if is_backfill else None,
        _rounding_rule(description),
        # active: 0 if post-close, 1 otherwise
        0 if first_seen_utc >= close_time_utc else 1,
    ))


def _deactivate_stale_markets(conn: sqlite3.Connection) -> int:
    """Set active=0 for markets whose trading close was >48h ago."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).isoformat()
    cur = conn.execute(
        "UPDATE weather_markets SET active=0 WHERE active=1 AND close_time_utc < ?",
        (cutoff,)
    )
    conn.commit()
    return cur.rowcount


def _check_neg_risk_groups(conn: sqlite3.Connection, city: str, settlement_date: str) -> None:
    """Warn if multiple neg_risk_market_id values exist for the same city+date."""
    rows = conn.execute("""
        SELECT DISTINCT neg_risk_market_id
        FROM weather_markets
        WHERE city=? AND settlement_date=? AND neg_risk_market_id IS NOT NULL
    """, (city, settlement_date)).fetchall()
    if len(rows) > 1:
        log.warning("Multiple neg_risk_market_ids for %s %s: %s",
                    city, settlement_date, [r[0] for r in rows])


async def discover(conn: sqlite3.Connection, days_ahead: int = 2) -> int:
    cities   = _load_cities()
    today    = dt.datetime.now(dt.timezone.utc).date()
    upserted = 0
    zero_market_cities: list[str] = []

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for city in cities:
            for offset in range(days_ahead + 1):
                day        = today + dt.timedelta(days=offset)
                event_slug = _event_slug(city["slug"], day)
                try:
                    event = await _fetch_json(client, f"{GAMMA_EVENT}/{event_slug}")
                except Exception as e:
                    log.warning("%s: %s", event_slug, e)
                    continue
                if not event:
                    if offset == 0:
                        zero_market_cities.append(city["city"])
                    continue

                markets = event.get("markets") or []
                if not markets and offset == 0:
                    zero_market_cities.append(city["city"])

                log.info("%s: %d markets", event_slug, len(markets))
                for market in markets:
                    condition_id = market.get("conditionId")
                    clob = await _fetch_clob(client, condition_id) if condition_id else None

                    # Warn on NULL neg_risk_market_id
                    if clob and not clob.get("neg_risk_market_id"):
                        log.warning("NULL neg_risk_market_id for %s — will retry next run",
                                    (condition_id or "?")[:10])

                    _upsert_market(conn, city, event_slug, day, event, market, clob)
                    upserted += 1
                    await asyncio.sleep(0.05)

                # Check for multi-group neg_risk after upserting
                _check_neg_risk_groups(conn, city["city"], day.isoformat())
                conn.commit()
                await asyncio.sleep(0.1)

    # Slug failure detection: ≥3 cities returned 0 markets for today
    if len(zero_market_cities) >= SLUG_ZERO_ALERT_THRESHOLD:
        now = _now()
        log.error("Slug format failure suspected: %d cities returned 0 markets: %s",
                  len(zero_market_cities), zero_market_cities)
        conn.execute("""
            INSERT INTO alerts(opened_utc,last_seen_utc,city,alert_type,status,detail_json)
            VALUES(?,?,?,?,?,?)
        """, (now, now, "SYSTEM", "discovery_slug_failure", "open",
              json.dumps({"zero_market_cities": zero_market_cities})))
        conn.commit()

    # Deactivate stale markets
    stale = _deactivate_stale_markets(conn)
    if stale:
        log.info("Deactivated %d stale markets (close >48h ago)", stale)

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
