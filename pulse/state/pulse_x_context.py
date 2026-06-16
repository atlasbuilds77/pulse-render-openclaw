#!/usr/bin/env python3
"""
Live X/news/options-flow context fetcher for Pulse.

This script uses the xAI/OpenClaw `x_search` tool indirectly via the `openclaw` CLI
when available. It returns a normalized JSON structure Pulse can merge into trader_brain.

Usage:
  python3 pulse_x_context.py TSLA
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Dict, List

TRUSTED_X_HANDLES = [
    "unusual_whales",
    "DeItaone",
    "zerohedge",
    "WalterBloomberg",
    "StockMKTNewz",
    "MarketRebels",
    "newsfilterio",
    "Benzinga",
]


def _extract_json_blob(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    for candidate in (text, text[text.find('{'):] if '{' in text else '', text[text.find('['):] if '[' in text else ''):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _run_x_search(query: str) -> Any:
    payload = {
        "query": query,
        "allowed_x_handles": [],
        "enable_image_understanding": False,
        "enable_video_understanding": False,
    }
    cmd = ["openclaw", "x-search", json.dumps(payload)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "x_search failed")
    parsed = _extract_json_blob(proc.stdout)
    if parsed is None:
        raise RuntimeError("Could not parse x_search output")
    return parsed


def _collect_texts(payload: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        iterable = payload
    elif isinstance(payload, dict):
        iterable = payload.get("results") or payload.get("items") or payload.get("posts") or []
    else:
        iterable = []

    for item in iterable:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content") or item.get("snippet") or item.get("body")
        handle = item.get("author_handle") or item.get("handle") or item.get("author") or item.get("username")
        url = item.get("url") or item.get("post_url")
        created = item.get("created_at") or item.get("date")
        if text:
            items.append({
                "handle": handle,
                "text": text,
                "url": url,
                "created_at": created,
            })
    return items


def _normalize(items: List[Dict[str, Any]], symbol: str) -> Dict[str, Any]:
    texts = [i["text"] for i in items if i.get("text")]
    lowered = " \n".join(texts).lower()

    options_flow_mentions = []
    for item in items:
        text = str(item.get("text", ""))
        lower = text.lower()
        if any(k in lower for k in ["sweep", "sweeps", "call sweep", "put sweep", "unusual options", "0dte", "block", "dark pool"]):
            options_flow_mentions.append(item)

    narratives = []
    if "upgrade" in lowered:
        narratives.append("analyst-upgrade chatter")
    if "downgrade" in lowered:
        narratives.append("analyst-downgrade chatter")
    if "earnings" in lowered:
        narratives.append("earnings narrative active")
    if "guidance" in lowered:
        narratives.append("guidance discussion")
    if "news" in lowered or "breaking" in lowered:
        narratives.append("breaking-news chatter")
    if "call" in lowered and "sweep" in lowered:
        narratives.append("bullish options-flow chatter")
    if "put" in lowered and "sweep" in lowered:
        narratives.append("bearish options-flow chatter")

    bullish_hits = sum(1 for t in texts if any(k in t.lower() for k in ["bullish", "calls", "beat", "breakout", "squeeze", "long"]))
    bearish_hits = sum(1 for t in texts if any(k in t.lower() for k in ["bearish", "puts", "miss", "breakdown", "short", "offering"]))
    if bullish_hits > bearish_hits:
        sentiment = "bullish"
    elif bearish_hits > bullish_hits:
        sentiment = "bearish"
    elif texts:
        sentiment = "mixed"
    else:
        sentiment = "unknown"

    rumor_risk = "high" if any(k in lowered for k in ["rumor", "unconfirmed", "hearing", "source?"]) else "low" if texts else "unknown"
    confidence = min(1.0, len(items) / 8.0)

    return {
        "symbol": symbol,
        "headline_signals": items[:5],
        "sentiment_skew": sentiment,
        "options_flow_mentions": options_flow_mentions[:5],
        "repeated_narratives": narratives[:6],
        "trusted_accounts": sorted({str(i.get('handle')) for i in items if i.get('handle')})[:10],
        "rumor_risk": rumor_risk,
        "x_confidence_score": confidence,
        "post_count": len(items),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol')
    args = parser.parse_args()
    symbol = args.symbol.strip().upper().replace('$', '')
    query = f'${symbol} OR {symbol} news OR {symbol} calls OR puts OR sweep OR unusual options'
    try:
        raw = _run_x_search(query)
        items = _collect_texts(raw)
        print(json.dumps(_normalize(items, symbol)))
        return 0
    except Exception as exc:
        print(json.dumps({
            "symbol": symbol,
            "headline_signals": [],
            "sentiment_skew": "unknown",
            "options_flow_mentions": [],
            "repeated_narratives": [],
            "trusted_accounts": [],
            "rumor_risk": "unknown",
            "x_confidence_score": 0.0,
            "error": str(exc),
        }))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
