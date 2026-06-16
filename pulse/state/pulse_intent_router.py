#!/usr/bin/env python3
"""
Pulse natural-language intent router.

Turns normal chat asks like:
- "SPY gamma levels"
- "calls or puts on QQQ"
- "AAPL news"
- "where is TSLA"

into the correct underlying Pulse data path.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__import__('os').getenv('PULSE_STATE_DIR', '/app/pulse/state'))
TRADER_BRAIN = ROOT / 'trader_brain.py'
POLYGON_QUOTE = ROOT / 'polygon_quote.py'
PULSE_ORCH = ROOT / 'pulse_orchestrator.py'

COMMON_WORDS = {
    'TELL','WHAT','WHATS','IS','ARE','THE','ME','ON','FOR','TO','OF','AND','OR','A','AN','YOU','YO',
    'PULSE','PLEASE','WITH','AT','NOW','LOOKING','LIKE','GIVE','SHOW','CHECK','DO','I','BUY','SHOULD',
    'CALLS','PUTS','GAMMA','LEVELS','LEVEL','BIAS','NEWS','SETUP','ORDERFLOW','ORDERBOOK','LIQUIDITY','WHERE',
    'BULLISH','BEARISH','SUPPORT','RESISTANCE','HEADLINE','CATALYST','PRICE','QUOTE','WALL','FLIP','MAGNETS'
}
INDEX_HINTS = {'SPY','QQQ','IWM','DIA','SPX','NDX','VIX','RUT','DJI'}
CRYPTO_HINTS = {'BTC','ETH','SOL'}


def _run_json(cmd: list[str]) -> Dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    stdout = (proc.stdout or '').strip()
    stderr = (proc.stderr or '').strip()
    if proc.returncode != 0 and not stdout:
        raise RuntimeError(stderr or 'command failed')
    try:
        return json.loads(stdout)
    except Exception as exc:
        raise RuntimeError(f'Invalid JSON from {cmd[-1] if cmd else cmd}: {exc}') from exc


def _normalize_symbol(raw: str) -> str:
    sym = raw.strip().upper().replace('$', '').replace('^', '')
    if sym in {'BTC','ETH','SOL'}:
        return f'{sym}-USD'
    return sym


def extract_symbol(text: str) -> Optional[str]:
    candidates = re.findall(r'\$?[A-Za-z]{1,5}(?:-USD)?|\^[A-Za-z]{1,5}', text)
    cleaned: list[str] = []
    for candidate in candidates:
        sym = _normalize_symbol(candidate)
        if not sym:
            continue
        if sym in INDEX_HINTS or sym in CRYPTO_HINTS or '-USD' in sym:
            return sym
        if sym.isalpha() and 1 <= len(sym) <= 5 and sym not in COMMON_WORDS:
            cleaned.append(sym)

    preferred = [sym for sym in cleaned if len(sym) >= 2]
    if preferred:
        return preferred[-1]
    return cleaned[-1] if cleaned else None


def classify_intent(text: str) -> str:
    lower = text.lower()
    if any(k in lower for k in ['gamma', 'call wall', 'put wall', 'gamma flip', 'magnets']):
        return 'gamma'
    if any(k in lower for k in ['orderbook', 'order book', 'orderflow', 'order flow', 'liquidity']):
        return 'orderbook'
    if 'news' in lower or 'headline' in lower or 'catalyst' in lower:
        return 'news'
    if 'bias' in lower or 'bullish' in lower or 'bearish' in lower or 'calls or puts' in lower or 'calls or puts on' in lower:
        return 'bias'
    if 'level' in lower or 'levels' in lower or 'support' in lower or 'resistance' in lower:
        return 'levels'
    if any(k in lower for k in ['price', 'quote', 'where is', "where's"]):
        return 'quote'
    return 'pulse'


def route_message(text: str) -> Dict[str, Any]:
    symbol = extract_symbol(text)
    if not symbol:
        return {
            'ok': False,
            'needs_symbol': True,
            'reply': 'Which ticker?',
        }

    intent = classify_intent(text)
    if intent == 'quote':
        payload = _run_json([sys.executable, str(POLYGON_QUOTE), symbol])
    elif intent == 'gamma':
        payload = _run_json([sys.executable, str(TRADER_BRAIN), symbol, '--mode', 'bias'])
    elif intent in {'levels', 'bias', 'news', 'orderbook'}:
        payload = _run_json([sys.executable, str(TRADER_BRAIN), symbol, '--mode', intent])
    else:
        payload = _run_json([sys.executable, str(PULSE_ORCH), symbol, '--mode', 'pulse'])

    reply = None
    if intent == 'quote':
        reply = f"{payload.get('symbol') or symbol} is {payload.get('price')} (Polygon {payload.get('as_of_iso')})."
    elif intent == 'gamma':
        reply = ((payload.get('discord_summary') or {}).get('text') if isinstance(payload, dict) else None)
    elif intent == 'bias':
        reply = ((payload.get('discord_summary') or {}).get('text') if isinstance(payload, dict) else None)
    elif intent == 'levels':
        levels = payload.get('key_levels') or payload.get('levels') or {}
        reply = f"{symbol} levels — PDH {levels.get('prior_day_high')}, PDL {levels.get('prior_day_low')}, VWAP {levels.get('vwap')}, OR high {levels.get('opening_range_high')}, OR low {levels.get('opening_range_low')}."
    elif intent == 'news':
        items = payload.get('news') or []
        first = items[0] if items else {}
        title = first.get('title') or first.get('headline') or first.get('summary') or 'No fresh headline found.'
        reply = f"{symbol} news: {title}"
    elif intent == 'orderbook':
        ob = payload.get('orderbook') or payload.get('liquidity') or {}
        if not ob and payload.get('discord_summary'):
            reply = (payload.get('discord_summary') or {}).get('text')
        else:
            reply = f"{symbol} liquidity read — top bid {ob.get('top_bid')}, top ask {ob.get('top_ask')}."
    else:
        reply = ((payload.get('discord_summary') or {}).get('text') if isinstance(payload, dict) else None)

    return {
        'ok': True,
        'symbol': symbol,
        'intent': intent,
        'payload': payload,
        'reply': reply,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('message', nargs='+')
    args = parser.parse_args()
    text = ' '.join(args.message)
    json.dump(route_message(text), sys.stdout, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
