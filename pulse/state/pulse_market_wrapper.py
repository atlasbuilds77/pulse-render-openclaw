#!/usr/bin/env python3
"""
Pulse market wrapper.

Code-first entrypoint for market-shaped asks.
It classifies the ask, routes into the correct Pulse data path,
and returns a compact reply payload plus the raw routed payload.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from pulse_intent_router import route_message


def wrap_market_message(text: str, text_only: bool = False) -> Dict[str, Any]:
    try:
        routed = route_message(text)
    except Exception as exc:
        result = {
            'ok': False,
            'kind': 'route_error',
            'reply': f'Route failed: {exc}',
            'error': str(exc),
        }
        return {'ok': False, 'reply': result['reply'], 'error': result['error']} if text_only else result

    if not routed.get('ok'):
        result = {
            'ok': False,
            'kind': 'needs_symbol',
            'reply': routed.get('reply') or 'Which ticker?',
            'routed': routed,
        }
        return {'ok': result['ok'], 'reply': result['reply']} if text_only else result

    result = {
        'ok': True,
        'kind': 'market_reply',
        'symbol': routed.get('symbol'),
        'intent': routed.get('intent'),
        'reply': routed.get('reply'),
        'routed': routed,
    }
    return {'ok': True, 'reply': result['reply'], 'symbol': result['symbol'], 'intent': result['intent']} if text_only else result


def main() -> int:
    parser = argparse.ArgumentParser(description='Pulse wrapper for normal-language market asks.')
    parser.add_argument('message', nargs='+')
    parser.add_argument('--text-only', action='store_true', help='Return only the compact reply payload for live chat delivery.')
    args = parser.parse_args()
    text = ' '.join(args.message)
    json.dump(wrap_market_message(text, text_only=args.text_only), sys.stdout, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
