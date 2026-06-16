#!/usr/bin/env python3
"""
Pulse orchestrator.

Merges:
- trader_brain core data snapshot
- optional X context file injection
- optional chart vision job scaffold
- final Pulse-style merged payload

This is the clean bridge layer so the core Python brain doesn't need to know
how every upstream tool is called.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__import__('os').getenv('PULSE_STATE_DIR', '/app/pulse/state'))
TRADER_BRAIN = ROOT / 'trader_brain.py'
CHART_VISION = ROOT / 'chart_vision.py'
X_CONTEXT = ROOT / 'pulse_x_context.py'


def _run_json(cmd: list[str]) -> Dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or 'command failed')
    try:
        return json.loads(proc.stdout.strip())
    except Exception as exc:
        raise RuntimeError(f'Invalid JSON output: {exc}') from exc


def build(symbol: str, include_x: bool, include_chart: bool, mode: str) -> Dict[str, Any]:
    core = _run_json([sys.executable, str(TRADER_BRAIN), symbol, '--mode', mode])

    x_payload: Dict[str, Any] = {}
    if include_x:
        try:
            x_payload = _run_json([sys.executable, str(X_CONTEXT), symbol])
        except Exception as exc:
            x_payload = {
                'headline_signals': [],
                'sentiment_skew': 'unknown',
                'options_flow_mentions': [],
                'repeated_narratives': [],
                'trusted_accounts': [],
                'rumor_risk': 'unknown',
                'x_confidence_score': 0.0,
                'error': str(exc),
            }

    chart_payload: Dict[str, Any] = {}
    if include_chart:
        try:
            chart_payload = _run_json([sys.executable, str(CHART_VISION), symbol])
        except Exception as exc:
            chart_payload = {'status': 'error', 'error': str(exc)}

    merged = dict(core)
    if include_x:
        merged['x_signals'] = x_payload
    if include_chart:
        merged['chart_vision_job'] = chart_payload

    if mode == 'pulse':
        merged['pulse_summary'] = {
            'regime': merged.get('regime') or merged.get('market_regime'),
            'data_says': merged.get('data_says'),
            'chart_says': merged.get('chart_says'),
            'x_says': merged.get('x_signals'),
            'setup': merged.get('setup'),
            'score': merged.get('score'),
            'verdict': merged.get('verdict'),
        }
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol')
    parser.add_argument('--mode', default='pulse', choices=['full', 'pulse', 'levels', 'news', 'bias', 'orderbook'])
    parser.add_argument('--no-x', action='store_true')
    parser.add_argument('--no-chart', action='store_true')
    args = parser.parse_args()

    payload = build(args.symbol.upper(), include_x=not args.no_x, include_chart=not args.no_chart, mode=args.mode)
    print(json.dumps(payload))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
