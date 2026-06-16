#!/usr/bin/env python3
"""
Pulse trader brain v2.

Builds a structured multi-input trading snapshot:
- live market data
- multi-timeframe structure
- setup scoring
- optional X/news/options-flow context
- persistent analysis journal

Usage:
  python3 trader_brain.py SPY
  python3 trader_brain.py QQQ --mode levels
  python3 trader_brain.py TSLA --mode pulse
  python3 trader_brain.py BTC-USD --mode orderbook
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

POLYGON_BASE = "https://api.polygon.io"
COINBASE_BASE = "https://api.exchange.coinbase.com"
DEFAULT_CREDENTIALS_PATH = "/data/credentials.json"
DEFAULT_JOURNAL_PATH = os.getenv("PULSE_JOURNAL_PATH", "/data/pulse/analysis_journal.jsonl")
DEFAULT_VISION_DIR = os.getenv("PULSE_VISION_DIR", "/data/pulse/chart_vision")
NYSE_TZ = ZoneInfo("America/New_York") if ZoneInfo else None
INDEX_MAP = {
    "SPX": "I:SPX",
    "NDX": "I:NDX",
    "VIX": "I:VIX",
    "DJI": "I:DJI",
    "RUT": "I:RUT",
}
TRUSTED_X_HANDLES = [
    "unusual_whales",
    "DeItaone",
    "zerohedge",
    "WalterBloomberg",
    "StockMKTNewz",
    "MarketRebels",
    "newsfilterio",
]
OPTIONS_EXPIRY_WINDOW_DAYS = 21
OPTIONS_SNAPSHOT_LIMIT = 250


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _read_credentials_key(path: str) -> Optional[str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        key = (payload.get("polygon", {}) or {}).get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    except Exception:
        return None
    return None


def _load_polygon_api_key() -> str:
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


def _detect_asset(symbol: str) -> str:
    symbol = symbol.upper()
    if "-USD" in symbol or symbol.endswith("USD") and symbol not in {"SPY", "QQQ", "IWM", "DIA"}:
        return "crypto"
    return "equity"


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


def _polygon_fetch(path: str, api_key: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query = dict(params or {})
    query["apiKey"] = api_key
    url = f"{POLYGON_BASE}{path}?{urllib.parse.urlencode(query)}"
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected Polygon response type for {path}")
    return payload


def _coinbase_fetch(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{COINBASE_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http_get_json(url, headers={"User-Agent": "pulse-trader-brain/2.0"})


def _trade_ts_to_ms(value: Any) -> Optional[int]:
    ts = _safe_int(value)
    if ts is None:
        return None
    if ts > 10**15:
        return ts // 1_000_000
    if ts > 10**12:
        return ts // 1000
    return ts


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = statistics.fmean(values[:period])
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for idx in range(1, len(values)):
        delta = values[idx] - values[idx - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))
    avg_gain = statistics.fmean(gains[:period])
    avg_loss = statistics.fmean(losses[:period])
    for idx in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[idx]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[idx]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(bars) <= period:
        return None
    true_ranges: List[float] = []
    prev_close = None
    for bar in bars:
        high = _safe_float(bar.get("h"))
        low = _safe_float(bar.get("l"))
        close = _safe_float(bar.get("c"))
        if high is None or low is None:
            continue
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = close
    if len(true_ranges) < period:
        return None
    return statistics.fmean(true_ranges[-period:])


def _vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    pv = 0.0
    vol = 0.0
    for bar in bars:
        high = _safe_float(bar.get("h"))
        low = _safe_float(bar.get("l"))
        close = _safe_float(bar.get("c"))
        volume = _safe_float(bar.get("v"))
        if None in (high, low, close, volume):
            continue
        typical = (high + low + close) / 3.0
        pv += typical * volume
        vol += volume
    if vol == 0:
        return None
    return pv / vol


def _bar_iso(bar: Dict[str, Any]) -> Optional[str]:
    ts = _safe_int(bar.get("t"))
    return _ms_to_iso(ts)


def _latest_session_bars(intraday_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not intraday_bars:
        return []
    last_ts = _safe_int(intraday_bars[-1].get("t"))
    if last_ts is None:
        return intraday_bars
    last_day = dt.datetime.fromtimestamp(last_ts / 1000, tz=dt.timezone.utc).date()
    return [b for b in intraday_bars if _safe_int(b.get("t")) and dt.datetime.fromtimestamp(int(b.get("t")) / 1000, tz=dt.timezone.utc).date() == last_day]


def _find_fvg(bars: List[Dict[str, Any]], limit: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    bullish: List[Dict[str, Any]] = []
    bearish: List[Dict[str, Any]] = []
    for i in range(2, len(bars)):
        a, c = bars[i - 2], bars[i]
        a_high = _safe_float(a.get("h"))
        a_low = _safe_float(a.get("l"))
        c_high = _safe_float(c.get("h"))
        c_low = _safe_float(c.get("l"))
        if None in (a_high, a_low, c_high, c_low):
            continue
        if a_high < c_low:
            bullish.append({
                "from_bar_iso": _bar_iso(a),
                "to_bar_iso": _bar_iso(c),
                "zone_low": a_high,
                "zone_high": c_low,
            })
        if a_low > c_high:
            bearish.append({
                "from_bar_iso": _bar_iso(a),
                "to_bar_iso": _bar_iso(c),
                "zone_low": c_high,
                "zone_high": a_low,
            })
    return {"bullish": bullish[-limit:], "bearish": bearish[-limit:]}


def _classify_trend(price: Optional[float], ema20: Optional[float], ema50: Optional[float], ema200: Optional[float]) -> str:
    if None in (price, ema20, ema50):
        return "unknown"
    if price > ema20 > ema50 and (ema200 is None or ema50 > ema200):
        return "bullish"
    if price < ema20 < ema50 and (ema200 is None or ema50 < ema200):
        return "bearish"
    return "mixed"


def _classify_regime(price: Optional[float], atr: Optional[float], day_range: Optional[float], relative_volume: Optional[float], ema20: Optional[float], ema50: Optional[float]) -> str:
    if None in (price, atr, day_range):
        return "unknown"
    compression = atr > 0 and day_range / atr < 0.9
    expansion = atr > 0 and day_range / atr > 1.5 and (relative_volume or 0) >= 1.2
    trend = ema20 is not None and ema50 is not None and abs(ema20 - ema50) / max(price, 0.01) > 0.003
    if expansion and trend:
        return "expansion"
    if compression and trend:
        return "trend_compression"
    if compression:
        return "chop"
    if trend:
        return "trend"
    return "range"


def _timeframe_bias(bars: List[Dict[str, Any]], price: Optional[float]) -> Dict[str, Any]:
    closes = [_safe_float(b.get("c")) for b in bars]
    closes = [v for v in closes if v is not None]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    rsi14 = _rsi(closes, 14)
    return {
        "trend": _classify_trend(price or (closes[-1] if closes else None), ema20, ema50, None),
        "ema20": ema20,
        "ema50": ema50,
        "rsi14": rsi14,
    }


def _chunk_bars(bars: List[Dict[str, Any]], size: int) -> List[Dict[str, Any]]:
    grouped: List[Dict[str, Any]] = []
    for idx in range(0, len(bars), size):
        chunk = bars[idx: idx + size]
        if len(chunk) < size:
            continue
        highs = [_safe_float(b.get("h")) for b in chunk]
        lows = [_safe_float(b.get("l")) for b in chunk]
        opens = [_safe_float(b.get("o")) for b in chunk]
        closes = [_safe_float(b.get("c")) for b in chunk]
        vols = [_safe_float(b.get("v")) or 0.0 for b in chunk]
        if None in (highs[0], lows[0], opens[0], closes[-1]):
            continue
        grouped.append({
            "t": chunk[-1].get("t"),
            "o": opens[0],
            "h": max(v for v in highs if v is not None),
            "l": min(v for v in lows if v is not None),
            "c": closes[-1],
            "v": sum(vols),
        })
    return grouped


def _infer_direction(price: Optional[float], session_vwap: Optional[float], daily_bias: str, intraday_bias: str) -> str:
    bullish_votes = 0
    bearish_votes = 0
    if daily_bias == "bullish":
        bullish_votes += 1
    elif daily_bias == "bearish":
        bearish_votes += 1
    if intraday_bias == "bullish":
        bullish_votes += 1
    elif intraday_bias == "bearish":
        bearish_votes += 1
    if price is not None and session_vwap is not None:
        if price > session_vwap:
            bullish_votes += 1
        else:
            bearish_votes += 1
    if bullish_votes > bearish_votes:
        return "long"
    if bearish_votes > bullish_votes:
        return "short"
    return "neutral"


def _score_setup(
    direction: str,
    daily_trend: str,
    h1_trend: str,
    m15_trend: str,
    relative_volume: Optional[float],
    high_swept: bool,
    low_swept: bool,
    regime: str,
    rr: Optional[float],
    invalidation_distance_pct: Optional[float],
    x_context: Dict[str, Any],
) -> Dict[str, Any]:
    checks: List[Tuple[str, int, str]] = []

    htf_align = 2 if direction == "long" and daily_trend == "bullish" or direction == "short" and daily_trend == "bearish" else 1 if daily_trend == "mixed" else 0
    checks.append(("HTF trend alignment", htf_align, daily_trend))

    tf_stack = 2 if daily_trend == h1_trend == m15_trend and daily_trend in {"bullish", "bearish"} else 1 if h1_trend == m15_trend else 0
    checks.append(("Multi-timeframe alignment", tf_stack, f"D={daily_trend}, H1={h1_trend}, M15={m15_trend}"))

    liquidity = 2 if (direction == "long" and low_swept) or (direction == "short" and high_swept) else 1 if high_swept or low_swept else 0
    checks.append(("Liquidity context", liquidity, f"high_swept={high_swept}, low_swept={low_swept}"))

    volume_score = 2 if (relative_volume or 0) >= 1.5 else 1 if (relative_volume or 0) >= 1.0 else 0
    checks.append(("Volume / participation", volume_score, f"rvol={relative_volume}"))

    structure = 0
    if regime in {"trend", "expansion", "trend_compression"}:
        structure = 2 if regime != "trend_compression" else 1
    elif regime == "range":
        structure = 1
    checks.append(("Structure cleanliness", structure, regime))

    rr_score = 2 if (rr or 0) >= 2.0 else 1 if (rr or 0) >= 1.25 else 0
    checks.append(("Risk/reward quality", rr_score, f"rr={rr}"))

    invalid_score = 2 if invalidation_distance_pct is not None and 0.15 <= invalidation_distance_pct <= 1.2 else 1 if invalidation_distance_pct is not None and invalidation_distance_pct <= 2.0 else 0
    checks.append(("Invalidation clarity", invalid_score, f"invalidation_pct={invalidation_distance_pct}"))

    x_score = x_context.get("x_confidence_score", 0)
    x_grade = 2 if x_score >= 0.65 else 1 if x_score >= 0.35 else 0
    checks.append(("News / X context", x_grade, x_context.get("sentiment_skew", "unknown")))

    total = sum(item[1] for item in checks)
    penalties: List[str] = []
    if regime == "chop":
        total -= 1
        penalties.append("Chop penalty")
    if x_context.get("rumor_risk") == "high":
        total -= 1
        penalties.append("Rumor risk penalty")
    if direction == "neutral":
        total -= 1
        penalties.append("No directional edge")

    if total >= 13:
        grade = "A"
    elif total >= 10:
        grade = "B"
    elif total >= 7:
        grade = "C"
    else:
        grade = "PASS"

    confidence = max(0.0, min(1.0, total / 16.0))
    wait_vs_act = "act" if grade in {"A", "B"} and direction != "neutral" else "wait"
    return {
        "grade": grade,
        "score_total": total,
        "confidence": confidence,
        "wait_vs_act": wait_vs_act,
        "checks": [{"name": n, "score": s, "note": note} for n, s, note in checks],
        "penalties": penalties,
    }


def _maybe_search_x(symbol: str, mode: str) -> Dict[str, Any]:
    if mode not in {"full", "pulse", "news"}:
        return {}
    script = Path(__file__).with_name("pulse_x_context.py")
    query_hint = f'${symbol} OR {symbol} news OR {symbol} calls OR puts OR sweep OR unusual options'
    base = {
        "headline_signals": [],
        "sentiment_skew": "unknown",
        "options_flow_mentions": [],
        "repeated_narratives": [],
        "trusted_accounts": [],
        "rumor_risk": "unknown",
        "x_confidence_score": 0.0,
        "query_hint": query_hint,
        "trusted_handle_seed": TRUSTED_X_HANDLES,
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(script), symbol],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout or "").strip()
        if not output:
            return {**base, "error": (proc.stderr or "x context returned empty output").strip()}
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            merged = {**base, **parsed}
            return merged
        return {**base, "error": "Unexpected x context payload type"}
    except Exception as exc:
        return {**base, "error": str(exc)}


def _build_chart_vision_stub(symbol: str, asset_type: str) -> Dict[str, Any]:
    return {
        "status": "scaffolded",
        "asset_type": asset_type,
        "capture_targets": ["daily", "1h", "15m", "5m"],
        "vision_fields": [
            "visual_regime",
            "trend_geometry",
            "pattern_candidates",
            "level_confluence",
            "structure_quality_score",
            "momentum_visual_score",
            "chart_cleanliness_score",
            "visual_warning_flags",
        ],
        "storage_dir": DEFAULT_VISION_DIR,
        "note": "Use screenshot capture + vision model bridge to fill this section.",
    }


def _persist_analysis(payload: Dict[str, Any]) -> None:
    path = Path(os.getenv("PULSE_ANALYSIS_JOURNAL", DEFAULT_JOURNAL_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _coalesce_iso(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
        ms = _trade_ts_to_ms(value)
        if ms is not None:
            iso = _ms_to_iso(ms)
            if iso:
                return iso
    return None


def _days_to_expiry(expiry: Optional[str], now_utc: dt.datetime) -> Optional[int]:
    if not expiry:
        return None
    try:
        expiry_date = dt.date.fromisoformat(expiry)
    except Exception:
        return None
    return (expiry_date - now_utc.date()).days


def _pick_level_by_proximity(levels: List[Dict[str, Any]], price: Optional[float], prefer_above: bool) -> Optional[Dict[str, Any]]:
    if not levels:
        return None
    if price is None:
        return levels[0]
    filtered = []
    for level in levels:
        strike = _safe_float(level.get("strike"))
        if strike is None:
            continue
        if prefer_above and strike >= price:
            filtered.append(level)
        elif not prefer_above and strike <= price:
            filtered.append(level)
    candidates = filtered or levels
    return min(
        candidates,
        key=lambda item: abs((_safe_float(item.get("strike")) or price) - price),
    )


def _select_expiry_from_contracts(contracts: List[Dict[str, Any]], now_utc: dt.datetime) -> Optional[str]:
    expiries: List[Tuple[int, str]] = []
    seen = set()
    for contract in contracts:
        details = contract.get("details") or {}
        expiry = details.get("expiration_date")
        if not isinstance(expiry, str) or expiry in seen:
            continue
        dte = _days_to_expiry(expiry, now_utc)
        if dte is None or dte < 0 or dte > OPTIONS_EXPIRY_WINDOW_DAYS:
            continue
        expiries.append((dte, expiry))
        seen.add(expiry)
    if not expiries:
        return None
    expiries.sort(key=lambda item: (item[0], item[1]))
    return expiries[0][1]


def _fetch_option_snapshot_chain(symbol: str, api_key: str, expiry: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    contracts: List[Dict[str, Any]] = []
    next_url: Optional[str] = None
    selected_expiry = expiry
    while True:
        if next_url:
            url = next_url + ("&" if "?" in next_url else "?") + urllib.parse.urlencode({"apiKey": api_key})
            payload = _http_get_json(url)
        else:
            params: Dict[str, Any] = {"limit": str(OPTIONS_SNAPSHOT_LIMIT)}
            if selected_expiry:
                params["expiration_date"] = selected_expiry
            else:
                now_utc = _utc_now()
                params["expiration_date.gte"] = now_utc.date().isoformat()
                params["expiration_date.lte"] = (now_utc.date() + dt.timedelta(days=OPTIONS_EXPIRY_WINDOW_DAYS)).isoformat()
            payload = _polygon_fetch(f"/v3/snapshot/options/{symbol}", api_key, params)
        results = payload.get("results") or []
        if not isinstance(results, list):
            break
        if not selected_expiry:
            inferred = _select_expiry_from_contracts(results, _utc_now())
            if inferred:
                selected_expiry = inferred
                contracts = []
                next_url = None
                continue
        for item in results:
            details = item.get("details") or {}
            if selected_expiry and details.get("expiration_date") != selected_expiry:
                continue
            contracts.append(item)
        next_url = payload.get("next_url")
        if not next_url:
            break
    return contracts, selected_expiry


def _build_option_structure_summary(symbol: str, api_key: str, price: Optional[float], now_utc: dt.datetime) -> Dict[str, Any]:
    contracts, expiry_used = _fetch_option_snapshot_chain(symbol, api_key)
    if not contracts:
        return {
            "source": "polygon_options_snapshot",
            "status": "unavailable",
            "note": "No option snapshot contracts returned for the selected window.",
            "expiry_used": expiry_used,
            "days_to_expiry": _days_to_expiry(expiry_used, now_utc),
            "underlying_price": price,
            "as_of_iso": None,
            "greeks_available": False,
            "gamma_available": False,
            "gamma_mode": "unavailable",
            "call_wall": None,
            "put_wall": None,
            "gamma_flip": None,
            "magnets": [],
            "summary": {},
            "strikes": [],
        }

    strike_map: Dict[float, Dict[str, Any]] = {}
    as_of_candidates: List[str] = []
    greeks_available = False

    for item in contracts:
        details = item.get("details") or {}
        strike = _safe_float(details.get("strike_price"))
        contract_type = str(details.get("contract_type") or "").lower()
        if strike is None or contract_type not in {"call", "put"}:
            continue
        bucket = strike_map.setdefault(
            strike,
            {
                "strike": strike,
                "call_oi": 0,
                "put_oi": 0,
                "call_gex": 0.0,
                "put_gex": 0.0,
                "net_gex": 0.0,
                "call_contracts": 0,
                "put_contracts": 0,
            },
        )
        oi = _safe_int(item.get("open_interest")) or 0
        multiplier = _safe_float(details.get("shares_per_contract")) or 100.0
        greeks = item.get("greeks") or {}
        gamma = _safe_float(greeks.get("gamma"))
        if gamma is not None:
            greeks_available = True
        as_of_iso = _coalesce_iso(
            ((item.get("underlying_asset") or {}).get("last_updated")),
            ((item.get("last_quote") or {}).get("last_updated")),
            ((item.get("last_trade") or {}).get("sip_timestamp")),
            ((item.get("day") or {}).get("last_updated")),
        )
        if as_of_iso:
            as_of_candidates.append(as_of_iso)

        gex = (oi * gamma * multiplier) if gamma is not None else None
        if contract_type == "call":
            bucket["call_oi"] += oi
            bucket["call_contracts"] += 1
            if gex is not None:
                bucket["call_gex"] += gex
        else:
            bucket["put_oi"] += oi
            bucket["put_contracts"] += 1
            if gex is not None:
                bucket["put_gex"] += -gex

    strikes = sorted(strike_map.values(), key=lambda item: item["strike"])
    for bucket in strikes:
        bucket["net_gex"] = (bucket["call_gex"] or 0.0) + (bucket["put_gex"] or 0.0)

    call_oi_levels = [s for s in strikes if (s.get("call_oi") or 0) > 0]
    put_oi_levels = [s for s in strikes if (s.get("put_oi") or 0) > 0]
    call_wall = max(call_oi_levels, key=lambda item: (item.get("call_oi") or 0, -(abs((item.get("strike") or 0) - (price or 0))))) if call_oi_levels else None
    put_wall = max(put_oi_levels, key=lambda item: (item.get("put_oi") or 0, -(abs((item.get("strike") or 0) - (price or 0))))) if put_oi_levels else None

    gamma_flip = None
    magnets: List[Dict[str, Any]] = []
    if greeks_available:
        non_zero = [s for s in strikes if abs(s.get("net_gex") or 0.0) > 0]
        for left, right in zip(non_zero, non_zero[1:]):
            left_gex = left.get("net_gex") or 0.0
            right_gex = right.get("net_gex") or 0.0
            if left_gex == 0:
                gamma_flip = {"strike": left.get("strike"), "method": "net_gex_zero"}
                break
            if left_gex * right_gex < 0:
                gamma_flip = {
                    "strike": round((((left.get("strike") or 0.0) + (right.get("strike") or 0.0)) / 2.0), 2),
                    "method": "net_gex_zero_cross",
                    "between": [left.get("strike"), right.get("strike")],
                }
                break
        magnet_candidates = sorted(non_zero, key=lambda item: abs(item.get("net_gex") or 0.0), reverse=True)
        if price is not None:
            magnet_candidates = sorted(
                magnet_candidates,
                key=lambda item: (-abs(item.get("net_gex") or 0.0), abs((item.get("strike") or 0.0) - price)),
            )
        magnets = [
            {"strike": item.get("strike"), "net_gex": item.get("net_gex"), "call_gex": item.get("call_gex"), "put_gex": item.get("put_gex")}
            for item in magnet_candidates[:3]
        ]

    bull_trigger = None
    bear_trigger = None
    if greeks_available:
        bull_source = _pick_level_by_proximity([m for m in magnets if (_safe_float(m.get("strike")) is not None)], price, prefer_above=True)
        bear_source = _pick_level_by_proximity([m for m in magnets if (_safe_float(m.get("strike")) is not None)], price, prefer_above=False)
        bull_trigger = _safe_float((bull_source or {}).get("strike")) if bull_source else _safe_float((call_wall or {}).get("strike"))
        bear_trigger = _safe_float((bear_source or {}).get("strike")) if bear_source else _safe_float((put_wall or {}).get("strike"))
    else:
        bull_trigger = _safe_float((call_wall or {}).get("strike"))
        bear_trigger = _safe_float((put_wall or {}).get("strike"))

    trade_lean = "neutral"
    if price is not None and call_wall and put_wall:
        call_strike = _safe_float(call_wall.get("strike"))
        put_strike = _safe_float(put_wall.get("strike"))
        if call_strike is not None and put_strike is not None:
            if price >= call_strike:
                trade_lean = "bullish_breakout"
            elif price <= put_strike:
                trade_lean = "bearish_breakdown"
            elif gamma_flip and _safe_float(gamma_flip.get("strike")) is not None:
                trade_lean = "calls_above_flip" if price >= _safe_float(gamma_flip.get("strike")) else "puts_below_flip"
            else:
                trade_lean = "range_between_walls"

    summary = {
        "bias": "neutral",
        "trade_lean": trade_lean,
        "trigger_level": bull_trigger if trade_lean in {"bullish_breakout", "calls_above_flip"} else bear_trigger,
        "invalidation_level": _safe_float(gamma_flip.get("strike")) if gamma_flip else bear_trigger if trade_lean in {"bullish_breakout", "calls_above_flip"} else bull_trigger,
        "target_level": _safe_float((call_wall or {}).get("strike")) if trade_lean in {"bullish_breakout", "calls_above_flip", "range_between_walls"} else _safe_float((put_wall or {}).get("strike")),
        "support_level": _safe_float((put_wall or {}).get("strike")),
        "plain_english_reason": "",
        "do_not_chase": True,
    }
    if trade_lean in {"bullish_breakout", "calls_above_flip"}:
        summary["bias"] = "neutral_to_bullish"
        summary["plain_english_reason"] = (
            f"Price is above the key positioning pivot near {summary['invalidation_level']}. "
            f"Upside structure is cleaner toward {summary['target_level']}."
        ) if summary["target_level"] is not None else "Upside structure is cleaner if price holds above the pivot."
    elif trade_lean in {"bearish_breakdown", "puts_below_flip"}:
        summary["bias"] = "neutral_to_bearish"
        summary["plain_english_reason"] = (
            f"Price is below the key positioning pivot near {summary['invalidation_level']}. "
            f"Downside opens faster toward {summary['target_level']}."
        ) if summary["target_level"] is not None else "Below the pivot, downside structure is cleaner than hope."
    else:
        summary["bias"] = "neutral"
        upper = _safe_float((call_wall or {}).get("strike"))
        lower = _safe_float((put_wall or {}).get("strike"))
        if upper is not None and lower is not None:
            summary["plain_english_reason"] = f"Price is sitting between the {lower} put wall and {upper} call wall, so chop risk stays high."
        else:
            summary["plain_english_reason"] = "Positioning is mixed, so there is no clean side yet."

    compact_strikes = [
        {
            "strike": item.get("strike"),
            "call_oi": item.get("call_oi"),
            "put_oi": item.get("put_oi"),
            "call_gex": item.get("call_gex"),
            "put_gex": item.get("put_gex"),
            "net_gex": item.get("net_gex"),
        }
        for item in strikes
        if (item.get("call_oi") or 0) > 0 or (item.get("put_oi") or 0) > 0
    ]

    return {
        "source": "polygon_options_snapshot",
        "status": "ok",
        "note": None if greeks_available else "Greeks were empty in the snapshot; using open-interest structure only.",
        "expiry_used": expiry_used,
        "days_to_expiry": _days_to_expiry(expiry_used, now_utc),
        "underlying_price": price,
        "as_of_iso": min(as_of_candidates) if as_of_candidates else None,
        "contracts_considered": len(contracts),
        "greeks_available": greeks_available,
        "gamma_available": greeks_available,
        "gamma_mode": "live_snapshot" if greeks_available else "oi_fallback",
        "call_wall": {"strike": call_wall.get("strike"), "oi": call_wall.get("call_oi"), "gex": call_wall.get("call_gex")} if call_wall else None,
        "put_wall": {"strike": put_wall.get("strike"), "oi": put_wall.get("put_oi"), "gex": put_wall.get("put_gex")} if put_wall else None,
        "gamma_flip": gamma_flip,
        "magnets": magnets,
        "summary": summary,
        "strikes": compact_strikes,
    }


def _fmt_level(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "n/a"
    if abs(number) >= 100:
        return f"{number:.0f}" if abs(number - round(number)) < 0.05 else f"{number:.2f}"
    return f"{number:.2f}"


def _build_equity_discord_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    options = payload.get("options_positioning") or {}
    summary = options.get("summary") or {}
    price_block = payload.get("price") or {}
    price = _safe_float(price_block.get("last"))
    call_wall = ((options.get("call_wall") or {}).get("strike"))
    put_wall = ((options.get("put_wall") or {}).get("strike"))
    gamma_flip = ((options.get("gamma_flip") or {}).get("strike"))
    trigger = summary.get("trigger_level")
    invalidation = summary.get("invalidation_level")
    target = summary.get("target_level")
    trade_lean = str(summary.get("trade_lean") or "neutral")
    gamma_mode = options.get("gamma_mode") or "unknown"
    expiry_used = options.get("expiry_used")

    if trade_lean in {"bullish_breakout", "calls_above_flip"}:
        lean_text = f"calls above {_fmt_level(trigger)}"
        risk_text = f"lose {_fmt_level(invalidation)} and the long case weakens"
    elif trade_lean in {"bearish_breakdown", "puts_below_flip"}:
        lean_text = f"puts below {_fmt_level(trigger)}"
        risk_text = f"reclaim {_fmt_level(invalidation)} and downside probably stalls"
    else:
        lean_text = "nothing aggressive in the middle"
        risk_text = f"chop risk stays high between {_fmt_level(put_wall)} and {_fmt_level(call_wall)}"

    target_text = None
    if target is not None:
        if trade_lean in {"bearish_breakdown", "puts_below_flip"}:
            target_text = f"downside lane opens toward {_fmt_level(target)}"
        else:
            target_text = f"best target zone is {_fmt_level(target)}"

    why = str(summary.get("plain_english_reason") or "Positioning is mixed, so wait for cleaner levels.")
    mode_note = "live gamma" if gamma_mode == "live_snapshot" else "OI fallback"
    header = f"{payload.get('requested')} positioning ({mode_note}, expiry {expiry_used or 'n/a'})"
    lines = [
        header,
        f"Price: {_fmt_level(price)} | Lean: {lean_text}.",
    ]
    if target_text:
        lines.append(f"Target: {target_text}.")
    lines.append(f"Risk: {risk_text}.")
    if gamma_flip is not None:
        lines.append(f"Flip: {_fmt_level(gamma_flip)} | Call wall: {_fmt_level(call_wall)} | Put wall: {_fmt_level(put_wall)}.")
    else:
        lines.append(f"Call wall: {_fmt_level(call_wall)} | Put wall: {_fmt_level(put_wall)}.")
    lines.append(f"Why: {why}")
    return {
        "format": "discord_summary_v1",
        "header": header,
        "lines": lines,
        "text": "\n".join(lines),
    }


def build_equity_snapshot(symbol: str, mode: str, news_limit: int) -> Dict[str, Any]:
    api_key = _load_polygon_api_key()
    now_utc = _utc_now()

    degraded_notes: List[str] = []
    try:
        last_trade = _polygon_fetch(f"/v2/last/trade/{symbol}", api_key)
    except Exception as exc:
        last_trade = {"status": "UNAVAILABLE", "results": {}}
        degraded_notes.append(f"last trade unavailable ({type(exc).__name__})")
    prev_day = _polygon_fetch(f"/v2/aggs/ticker/{symbol}/prev", api_key, {"adjusted": "true"})

    start_intraday = (now_utc - dt.timedelta(days=3)).date().isoformat()
    end_intraday = now_utc.date().isoformat()
    intraday = _polygon_fetch(
        f"/v2/aggs/ticker/{symbol}/range/5/minute/{start_intraday}/{end_intraday}",
        api_key,
        {"adjusted": "true", "sort": "asc", "limit": "5000"},
    )

    start_daily = (now_utc - dt.timedelta(days=260)).date().isoformat()
    daily = _polygon_fetch(
        f"/v2/aggs/ticker/{symbol}/range/1/day/{start_daily}/{end_intraday}",
        api_key,
        {"adjusted": "true", "sort": "asc", "limit": "5000"},
    )

    try:
        recent_trades = _polygon_fetch(
            f"/v3/trades/{symbol}",
            api_key,
            {"order": "desc", "sort": "timestamp", "limit": "200"},
        )
    except Exception as exc:
        recent_trades = {"status": "UNAVAILABLE", "results": []}
        degraded_notes.append(f"recent tape unavailable ({type(exc).__name__})")

    try:
        news = _polygon_fetch(
            "/v2/reference/news",
            api_key,
            {"ticker": symbol, "limit": str(max(1, min(20, news_limit))), "order": "desc", "sort": "published_utc"},
        )
    except Exception as exc:
        news = {"status": "UNAVAILABLE", "results": []}
        degraded_notes.append(f"news unavailable ({type(exc).__name__})")

    trade_result = (last_trade.get("results") or {}) if isinstance(last_trade, dict) else {}
    prev_results = (prev_day.get("results") or []) if isinstance(prev_day, dict) else []
    prev = prev_results[0] if prev_results else {}
    intraday_bars = (intraday.get("results") or []) if isinstance(intraday, dict) else []
    daily_bars = (daily.get("results") or []) if isinstance(daily, dict) else []
    trades = (recent_trades.get("results") or []) if isinstance(recent_trades, dict) else []
    news_items = (news.get("results") or []) if isinstance(news, dict) else []

    price = _safe_float(trade_result.get("p")) or _safe_float(prev.get("c"))
    price_ts_ms = _trade_ts_to_ms(trade_result.get("t")) or _safe_int(prev.get("t"))
    prev_close = _safe_float(prev.get("c"))
    change_abs = price - prev_close if price is not None and prev_close not in (None, 0) else None
    change_pct = ((change_abs / prev_close) * 100.0) if change_abs is not None and prev_close else None

    session_bars = _latest_session_bars(intraday_bars)
    session_vwap = _vwap(session_bars)
    session_high = max((_safe_float(b.get("h")) for b in session_bars), default=None)
    session_low = min((_safe_float(b.get("l")) for b in session_bars), default=None)
    session_open = _safe_float(session_bars[0].get("o")) if session_bars else None
    session_close = _safe_float(session_bars[-1].get("c")) if session_bars else None
    session_volume = sum((_safe_float(b.get("v")) or 0.0) for b in session_bars) if session_bars else None
    day_range = (session_high - session_low) if None not in (session_high, session_low) else None

    orb_slice = session_bars[:6] if len(session_bars) >= 6 else session_bars
    orb_high = max((_safe_float(b.get("h")) for b in orb_slice), default=None)
    orb_low = min((_safe_float(b.get("l")) for b in orb_slice), default=None)

    closes = [_safe_float(b.get("c")) for b in daily_bars]
    closes = [x for x in closes if x is not None]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(daily_bars, 14)

    high_5d = max((_safe_float(b.get("h")) for b in daily_bars[-5:]), default=None) if daily_bars else None
    low_5d = min((_safe_float(b.get("l")) for b in daily_bars[-5:]), default=None) if daily_bars else None
    high_20d = max((_safe_float(b.get("h")) for b in daily_bars[-20:]), default=None) if daily_bars else None
    low_20d = min((_safe_float(b.get("l")) for b in daily_bars[-20:]), default=None) if daily_bars else None
    high_5w = max((_safe_float(b.get("h")) for b in daily_bars[-25:]), default=None) if daily_bars else None
    low_5w = min((_safe_float(b.get("l")) for b in daily_bars[-25:]), default=None) if daily_bars else None

    pdh = _safe_float(prev.get("h"))
    pdl = _safe_float(prev.get("l"))
    high_swept = bool(session_high is not None and pdh is not None and price is not None and session_high > pdh and price < pdh)
    low_swept = bool(session_low is not None and pdl is not None and price is not None and session_low < pdl and price > pdl)

    sizes = [_safe_float(t.get("size")) for t in trades]
    sizes = [s for s in sizes if s is not None]
    avg_trade_size = statistics.fmean(sizes) if sizes else None
    med_trade_size = statistics.median(sizes) if sizes else None
    max_trade_size = max(sizes) if sizes else None
    p95_trade_size = None
    large_print_count = 0
    if sizes and len(sizes) >= 10:
        ordered = sorted(sizes)
        idx = int(0.95 * (len(ordered) - 1))
        p95_trade_size = ordered[idx]
        large_print_count = sum(1 for s in sizes if s >= p95_trade_size)

    avg_volume_20d = None
    relative_volume = None
    if len(daily_bars) >= 2:
        prior_20 = daily_bars[-21:-1] if len(daily_bars) >= 21 else daily_bars[:-1]
        vols = [_safe_float(b.get("v")) for b in prior_20]
        vols = [v for v in vols if v is not None]
        if vols:
            avg_volume_20d = statistics.fmean(vols)
            if session_volume not in (None, 0):
                relative_volume = session_volume / avg_volume_20d if avg_volume_20d else None

    daily_tf = _timeframe_bias(daily_bars[-90:], price)
    h1_bars = _chunk_bars(session_bars, 12)
    m15_bars = _chunk_bars(session_bars, 3)
    h1_tf = _timeframe_bias(h1_bars, price)
    m15_tf = _timeframe_bias(m15_bars, price)
    m5_tf = _timeframe_bias(session_bars, price)

    fvg = _find_fvg(session_bars, limit=3)
    regime = _classify_regime(price, atr14, day_range, relative_volume, ema20, ema50)
    direction = _infer_direction(price, session_vwap, daily_tf["trend"], m15_tf["trend"])

    entry_zone = None
    invalidation = None
    targets: List[float] = []
    if direction == "long":
        entry_zone = {"low": session_vwap or pdl, "high": orb_high or price}
        invalidation = pdl or session_low
        for level in [pdh, high_5d, high_20d]:
            if level is not None and price is not None and level > price:
                targets.append(level)
    elif direction == "short":
        entry_zone = {"low": orb_low or price, "high": session_vwap or pdh}
        invalidation = pdh or session_high
        for level in [pdl, low_5d, low_20d]:
            if level is not None and price is not None and level < price:
                targets.append(level)
    targets = targets[:3]

    rr = None
    invalidation_distance_pct = None
    if price is not None and invalidation is not None and targets:
        risk = abs(price - invalidation)
        reward = abs(targets[0] - price)
        if risk > 0:
            rr = reward / risk
            invalidation_distance_pct = (risk / price) * 100.0

    news_summary = {
        "sentiment": "neutral",
        "sentiment_score": 0,
        "insight_hits": 0,
        "headlines": [],
    }
    sentiment_score = 0
    sentiment_hits = 0
    for item in news_items[:news_limit]:
        publisher = ((item.get("publisher") or {}).get("name")) or "Unknown"
        title = item.get("title")
        if title:
            news_summary["headlines"].append({
                "published_utc": item.get("published_utc"),
                "source": publisher,
                "title": title,
                "url": item.get("article_url"),
            })
        for insight in item.get("insights") or []:
            if str(insight.get("ticker", "")).upper() != symbol.upper():
                continue
            sentiment = str(insight.get("sentiment", "")).lower()
            if sentiment == "positive":
                sentiment_score += 1
            elif sentiment == "negative":
                sentiment_score -= 1
            sentiment_hits += 1
    if sentiment_hits:
        news_summary["sentiment_score"] = sentiment_score
        news_summary["insight_hits"] = sentiment_hits
        news_summary["sentiment"] = "positive" if sentiment_score > 0 else "negative" if sentiment_score < 0 else "neutral"

    x_context = _maybe_search_x(symbol, mode)
    chart_vision = _build_chart_vision_stub(symbol, "equity")
    options_positioning = _build_option_structure_summary(symbol, api_key, price, now_utc)
    setup_score = _score_setup(
        direction,
        daily_tf["trend"],
        h1_tf["trend"],
        m15_tf["trend"],
        relative_volume,
        high_swept,
        low_swept,
        regime,
        rr,
        invalidation_distance_pct,
        x_context,
    )

    payload: Dict[str, Any] = {
        "requested": symbol,
        "asset_type": "equity",
        "source": "polygon",
        "generated_at_utc": now_utc.isoformat(),
        "degraded_notes": degraded_notes,
        "price": {
            "last": price,
            "as_of_ms": price_ts_ms,
            "as_of_iso": _ms_to_iso(price_ts_ms),
            "prev_close": prev_close,
            "change_abs": change_abs,
            "change_pct": change_pct,
        },
        "market_regime": regime,
        "timeframe_stack": {
            "daily": daily_tf,
            "1h": h1_tf,
            "15m": m15_tf,
            "5m": m5_tf,
        },
        "key_levels": {
            "previous_day": {"high": pdh, "low": pdl, "close": prev_close},
            "session": {
                "open": session_open,
                "high": session_high,
                "low": session_low,
                "close": session_close,
                "vwap": session_vwap,
                "opening_range_30m_high": orb_high,
                "opening_range_30m_low": orb_low,
            },
            "swing": {
                "high_5d": high_5d,
                "low_5d": low_5d,
                "high_20d": high_20d,
                "low_20d": low_20d,
                "high_5w": high_5w,
                "low_5w": low_5w,
            },
            "ict": {
                "liquidity_sweep_high": high_swept,
                "liquidity_sweep_low": low_swept,
                "fair_value_gaps": fvg,
            },
            "options": {
                "expiry_used": options_positioning.get("expiry_used"),
                "days_to_expiry": options_positioning.get("days_to_expiry"),
                "call_wall": options_positioning.get("call_wall"),
                "put_wall": options_positioning.get("put_wall"),
                "gamma_flip": options_positioning.get("gamma_flip"),
                "magnets": options_positioning.get("magnets"),
                "gamma_mode": options_positioning.get("gamma_mode"),
            },
        },
        "data_says": {
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "rsi14": rsi14,
            "atr14": atr14,
            "relative_volume": relative_volume,
            "avg_volume_20d": avg_volume_20d,
            "session_volume": session_volume,
            "trend_strength": abs((ema20 or 0) - (ema50 or 0)) / price if price and ema20 and ema50 else None,
        },
        "liquidity": {
            "note": "US equity L2 order book is not available on this key; using trade-tape and volume proxies.",
            "recent_trade_count": len(trades),
            "avg_trade_size": avg_trade_size,
            "median_trade_size": med_trade_size,
            "p95_trade_size": p95_trade_size,
            "max_trade_size": max_trade_size,
            "large_print_count": large_print_count,
        },
        "news": news_summary,
        "options_positioning": options_positioning,
        "x_signals": x_context,
        "chart_says": chart_vision,
        "setup": {
            "direction": direction,
            "entry_zone": entry_zone,
            "invalidation": invalidation,
            "targets": targets,
            "risk_reward": rr,
            "trigger_condition": "confirmation through level with participation",
            "avoid_condition": "take no trade if price stays trapped in chop / rejects trigger immediately",
        },
        "score": setup_score,
        "verdict": {
            "wait_vs_act": setup_score["wait_vs_act"],
            "take": (
                "good setup if confirmation comes" if setup_score["grade"] in {"A", "B"} else "watchlist only until structure improves"
            ),
            "options_summary": options_positioning.get("summary"),
        },
    }
    payload["discord_summary"] = _build_equity_discord_summary(payload)

    _persist_analysis({
        "symbol": symbol,
        "timestamp": now_utc.isoformat(),
        "asset_type": "equity",
        "market_regime": regime,
        "direction": direction,
        "grade": setup_score["grade"],
        "score_total": setup_score["score_total"],
        "price": price,
        "entry_zone": entry_zone,
        "invalidation": invalidation,
        "targets": targets,
        "options_expiry": options_positioning.get("expiry_used"),
        "options_gamma_mode": options_positioning.get("gamma_mode"),
        "options_trade_lean": ((options_positioning.get("summary") or {}).get("trade_lean")),
    })

    if mode == "levels":
        return {"requested": symbol, "mode": mode, "price": payload["price"], "key_levels": payload["key_levels"], "degraded_notes": degraded_notes}
    if mode == "bias":
        return {
            "requested": symbol,
            "mode": mode,
            "price": payload["price"],
            "market_regime": regime,
            "timeframe_stack": payload["timeframe_stack"],
            "score": setup_score,
            "options_positioning": options_positioning,
            "discord_summary": payload["discord_summary"],
            "degraded_notes": degraded_notes,
        }
    if mode == "news":
        return {"requested": symbol, "mode": mode, "news": news_summary, "x_signals": x_context}
    if mode == "pulse":
        return {
            "requested": symbol,
            "mode": mode,
            "regime": regime,
            "data_says": payload["data_says"],
            "chart_says": chart_vision,
            "x_signals": x_context,
            "levels": payload["key_levels"],
            "options_positioning": options_positioning,
            "discord_summary": payload["discord_summary"],
            "setup": payload["setup"],
            "score": setup_score,
            "verdict": payload["verdict"],
            "degraded_notes": degraded_notes,
        }
    return payload


def build_crypto_snapshot(symbol: str, mode: str, news_limit: int) -> Dict[str, Any]:
    product = symbol.upper().replace("-USD", "-USD")
    now = _utc_now()
    ticker = _coinbase_fetch(f"/products/{product}/ticker")
    orderbook = _coinbase_fetch(f"/products/{product}/book", {"level": "2"})

    price = _safe_float(ticker.get("price")) if isinstance(ticker, dict) else None
    size = _safe_float(ticker.get("size")) if isinstance(ticker, dict) else None
    trade_id = ticker.get("trade_id") if isinstance(ticker, dict) else None
    time_iso = ticker.get("time") if isinstance(ticker, dict) else None

    payload: Dict[str, Any] = {
        "requested": symbol,
        "asset_type": "crypto",
        "source": "coinbase",
        "generated_at_utc": now.isoformat(),
        "price": {
            "last": price,
            "trade_size": size,
            "trade_id": trade_id,
            "as_of_iso": time_iso,
        },
        "orderbook": {
            "top_bid": _safe_float(orderbook.get("bids", [[None]])[0][0]) if isinstance(orderbook, dict) else None,
            "top_ask": _safe_float(orderbook.get("asks", [[None]])[0][0]) if isinstance(orderbook, dict) else None,
            "bid_levels": orderbook.get("bids", [])[:5] if isinstance(orderbook, dict) else [],
            "ask_levels": orderbook.get("asks", [])[:5] if isinstance(orderbook, dict) else [],
        },
    }

    if mode == "orderbook":
        return {"requested": symbol, "mode": mode, "price": payload["price"], "orderbook": payload["orderbook"]}

    end = now.isoformat().replace("+00:00", "Z")
    start = (now - dt.timedelta(days=10)).isoformat().replace("+00:00", "Z")
    candles = _coinbase_fetch(f"/products/{product}/candles", {"granularity": "3600", "start": start, "end": end})
    candles_sorted = sorted(candles, key=lambda r: r[0]) if isinstance(candles, list) else []
    closes = [_safe_float(row[4]) for row in candles_sorted]
    closes = [x for x in closes if x is not None]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi14 = _rsi(closes, 14)

    high_24h = max((_safe_float(row[2]) for row in candles_sorted[-24:]), default=None)
    low_24h = min((_safe_float(row[1]) for row in candles_sorted[-24:]), default=None)
    high_7d = max((_safe_float(row[2]) for row in candles_sorted[-24 * 7:]), default=None)
    low_7d = min((_safe_float(row[1]) for row in candles_sorted[-24 * 7:]), default=None)
    atr_proxy = statistics.fmean([abs((_safe_float(r[2]) or 0) - (_safe_float(r[1]) or 0)) for r in candles_sorted[-14:]]) if len(candles_sorted) >= 14 else None
    regime = _classify_regime(price, atr_proxy, (high_24h - low_24h) if None not in (high_24h, low_24h) else None, None, ema20, ema50)
    daily_trend = _classify_trend(price, ema20, ema50, ema200)

    x_context = _maybe_search_x(symbol, mode)
    chart_vision = _build_chart_vision_stub(symbol, "crypto")
    direction = "long" if daily_trend == "bullish" else "short" if daily_trend == "bearish" else "neutral"
    invalidation = low_24h if direction == "long" else high_24h if direction == "short" else None
    targets = [high_7d] if direction == "long" and high_7d else [low_7d] if direction == "short" and low_7d else []
    rr = None
    invalidation_distance_pct = None
    if price is not None and invalidation is not None and targets:
        risk = abs(price - invalidation)
        reward = abs(targets[0] - price)
        if risk > 0:
            rr = reward / risk
            invalidation_distance_pct = (risk / price) * 100.0

    setup_score = _score_setup(direction, daily_trend, daily_trend, daily_trend, None, False, False, regime, rr, invalidation_distance_pct, x_context)

    payload.update({
        "market_regime": regime,
        "timeframe_stack": {
            "daily": {"trend": daily_trend, "ema20": ema20, "ema50": ema50, "ema200": ema200, "rsi14": rsi14},
        },
        "key_levels": {"high_24h": high_24h, "low_24h": low_24h, "high_7d": high_7d, "low_7d": low_7d},
        "data_says": {"ema20": ema20, "ema50": ema50, "ema200": ema200, "rsi14": rsi14, "atr_proxy": atr_proxy},
        "x_signals": x_context,
        "chart_says": chart_vision,
        "setup": {
            "direction": direction,
            "entry_zone": {"low": low_24h, "high": price} if direction == "long" else {"low": price, "high": high_24h} if direction == "short" else None,
            "invalidation": invalidation,
            "targets": targets,
            "risk_reward": rr,
            "trigger_condition": "hold key level with follow-through",
            "avoid_condition": "skip if spread widens or momentum stalls into level",
        },
        "score": setup_score,
        "verdict": {"wait_vs_act": setup_score["wait_vs_act"], "take": "trend-follow only" if setup_score["grade"] in {"A", "B"} else "watch only"},
    })

    _persist_analysis({
        "symbol": symbol,
        "timestamp": now.isoformat(),
        "asset_type": "crypto",
        "market_regime": regime,
        "direction": direction,
        "grade": setup_score["grade"],
        "score_total": setup_score["score_total"],
        "price": price,
        "invalidation": invalidation,
        "targets": targets,
    })

    if mode == "levels":
        return {"requested": symbol, "mode": mode, "price": payload["price"], "key_levels": payload["key_levels"]}
    if mode == "bias":
        return {"requested": symbol, "mode": mode, "market_regime": regime, "timeframe_stack": payload["timeframe_stack"], "score": setup_score}
    if mode == "news":
        return {"requested": symbol, "mode": mode, "x_signals": x_context, "note": "Crypto headline feed should be added in runtime wrapper."}
    if mode == "pulse":
        return {
            "requested": symbol,
            "mode": mode,
            "regime": regime,
            "data_says": payload["data_says"],
            "chart_says": chart_vision,
            "x_signals": x_context,
            "levels": payload["key_levels"],
            "setup": payload["setup"],
            "score": setup_score,
            "verdict": payload["verdict"],
        }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build structured Pulse trading snapshot.")
    parser.add_argument("symbol", help="Ticker or product (e.g. SPY, QQQ, BTC-USD)")
    parser.add_argument(
        "--mode",
        choices=["full", "levels", "news", "bias", "orderbook", "pulse"],
        default="full",
        help="Output subset mode",
    )
    parser.add_argument("--news-limit", type=int, default=5, help="Headline count for equity mode")
    args = parser.parse_args()

    symbol = _normalize_symbol(args.symbol)
    asset = _detect_asset(symbol)

    try:
        if asset == "crypto":
            payload = build_crypto_snapshot(symbol, args.mode, args.news_limit)
        else:
            payload = build_equity_snapshot(symbol, args.mode, args.news_limit)
        print(json.dumps(payload))
        return 0
    except urllib.error.HTTPError as exc:
        body = None
        try:
            body = exc.read().decode("utf-8")[:300]
        except Exception:
            body = None
        print(json.dumps({"error": f"HTTP {exc.code}", "detail": body, "requested": symbol, "mode": args.mode, "asset_type": asset}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc), "requested": symbol, "mode": args.mode, "asset_type": asset}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
