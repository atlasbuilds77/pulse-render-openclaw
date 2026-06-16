#!/usr/bin/env python3
"""
Fetch a live-ish quote from Polygon and print normalized JSON.

Usage:
  python3 polygon_quote.py SPY
"""

import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

POLYGON_BASE = "https://api.polygon.io"
DEFAULT_CREDENTIALS_PATH = "/data/credentials.json"
INDEX_MAP = {
    "SPX": "I:SPX",
    "NDX": "I:NDX",
    "VIX": "I:VIX",
    "DJI": "I:DJI",
    "RUT": "I:RUT",
}


def _read_credentials_key(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        polygon = payload.get("polygon", {})
        key = polygon.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    except Exception:
        return None
    return None


def _load_api_key() -> str:
    env_key = os.getenv("POLYGON_API_KEY", "").strip()
    if env_key:
        return env_key
    cred_path = os.getenv("PULSE_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_PATH)
    file_key = _read_credentials_key(cred_path)
    if file_key:
        return file_key
    raise RuntimeError("Polygon API key missing (POLYGON_API_KEY or credentials file).")


def _normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if symbol.startswith("$"):
        symbol = symbol[1:]
    if symbol.startswith("^"):
        symbol = symbol[1:]
    return INDEX_MAP.get(symbol, symbol)


def _fetch(path: str, api_key: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query = dict(params or {})
    query["apiKey"] = api_key
    url = f"{POLYGON_BASE}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


def _epoch_ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def _trade_ts_to_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        ts = int(value)
    except Exception:
        return None
    # Polygon last-trade timestamps are nanoseconds.
    if ts > 10**15:
        return ts // 1_000_000
    if ts > 10**12:
        return ts // 1000
    return ts


def _build_response(
    requested: str,
    symbol: str,
    price: Optional[float],
    source: str,
    status: Optional[str],
    as_of_ms: Optional[int],
    prev_close: Optional[float],
) -> Dict[str, Any]:
    return {
        "requested": requested,
        "symbol": symbol,
        "price": price,
        "source": source,
        "status": status,
        "as_of_ms": as_of_ms,
        "as_of_iso": _epoch_ms_to_iso(as_of_ms),
        "prev_close": prev_close,
        "delayed": status == "DELAYED",
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: polygon_quote.py <TICKER>"}))
        return 2

    requested = sys.argv[1].strip().upper()
    symbol = _normalize_symbol(requested)

    try:
        api_key = _load_api_key()
    except Exception as exc:
        print(json.dumps({"error": str(exc), "requested": requested, "symbol": symbol}))
        return 1

    last_trade = {}
    prev_day = {}

    try:
        last_trade = _fetch(f"/v2/last/trade/{symbol}", api_key)
    except urllib.error.HTTPError as exc:
        print(
            json.dumps(
                {
                    "error": f"Polygon HTTP error: {exc.code}",
                    "requested": requested,
                    "symbol": symbol,
                }
            )
        )
        return 1
    except Exception as exc:
        print(json.dumps({"error": f"Polygon request failed: {exc}", "requested": requested, "symbol": symbol}))
        return 1

    try:
        prev_day = _fetch(f"/v2/aggs/ticker/{symbol}/prev", api_key, {"adjusted": "true"})
    except Exception:
        prev_day = {}

    trade_result = (last_trade or {}).get("results") or {}
    prev_results = (prev_day or {}).get("results") or []
    prev_first = prev_results[0] if prev_results else {}

    price = trade_result.get("p")
    if price in (None, 0):
        price = prev_first.get("c")

    as_of_ms = _trade_ts_to_ms(trade_result.get("t")) or prev_first.get("t")
    prev_close = prev_first.get("c")
    status = (last_trade or {}).get("status") or (prev_day or {}).get("status")
    source = "polygon_last_trade" if trade_result.get("p") not in (None, 0) else "polygon_prev_close"

    payload = _build_response(
        requested=requested,
        symbol=symbol,
        price=price,
        source=source,
        status=status,
        as_of_ms=as_of_ms,
        prev_close=prev_close,
    )

    if payload["price"] is None:
        payload["error"] = "No price returned by Polygon."
        print(json.dumps(payload))
        return 1

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
