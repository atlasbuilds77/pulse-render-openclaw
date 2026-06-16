#!/usr/bin/env python3
"""
Chart vision scaffold for Pulse.

This does not call a vision model directly yet.
It creates a structured chart-analysis job payload and file locations so
runtime wrappers can attach screenshots + invoke image analysis.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Dict, List

DEFAULT_DIR = Path('/app/pulse/state/chart_vision')


def build_job(symbol: str, asset_type: str) -> Dict[str, object]:
    ts = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base = DEFAULT_DIR / symbol.replace('/', '-') / ts
    screenshots = {
        'daily': str(base / 'daily.png'),
        '1h': str(base / '1h.png'),
        '15m': str(base / '15m.png'),
        '5m': str(base / '5m.png'),
    }
    return {
        'symbol': symbol,
        'asset_type': asset_type,
        'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'storage_dir': str(base),
        'screenshots': screenshots,
        'prompt_fields': [
            'visual_regime',
            'trend_geometry',
            'pattern_candidates',
            'level_confluence',
            'structure_quality_score',
            'momentum_visual_score',
            'chart_cleanliness_score',
            'visual_warning_flags',
            'best_case',
            'bear_case',
            'conditions_for_confirmation',
        ],
        'prompt_template': (
            'Analyze this trading chart set across daily, 1h, 15m, and 5m. '
            'Return structure quality, trend geometry, compression/expansion, level confluence, '
            'pattern candidates, momentum quality, warning flags, and whether the chart is clean or messy.'
        ),
    }


def persist_job(job: Dict[str, object]) -> None:
    base = Path(str(job['storage_dir']))
    base.mkdir(parents=True, exist_ok=True)
    (base / 'job.json').write_text(json.dumps(job, indent=2), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol')
    parser.add_argument('--asset-type', default='equity')
    args = parser.parse_args()
    job = build_job(args.symbol.upper(), args.asset_type)
    persist_job(job)
    print(json.dumps(job))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
