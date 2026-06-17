#!/usr/bin/env python3
"""Restricted local HTTP API for Pulse market tools.

Runs only inside the Render container on 127.0.0.1:8787. It lets OpenClaw use
Polygon/Titan/trader_brain without exposing raw API keys or shell access to Discord.
"""
from __future__ import annotations

import json, os, subprocess, sys, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.getenv('PULSE_STATE_DIR', '/app/pulse/state'))
DATA_DIR = Path(os.getenv('PULSE_DATA_DIR', os.getenv('OPENCLAW_STATE_DIR', '/data'))) / 'pulse'
TITAN_URL = os.getenv('TITAN_URL', 'https://titangex.com').rstrip('/')
TITAN_API_KEY = os.getenv('TITAN_API_KEY', '').strip()


def run_json(args: list[str], timeout: int = 240):
    proc = subprocess.run([sys.executable, *args], cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or '').strip()
    if proc.returncode != 0 and not out:
        return {'ok': False, 'error': (proc.stderr or 'command failed').strip()[:2000]}
    try:
        payload = json.loads(out)
    except Exception as exc:
        return {'ok': False, 'error': f'invalid_json: {exc}', 'stdout': out[:2000], 'stderr': (proc.stderr or '')[:1000]}
    if isinstance(payload, dict) and 'ok' not in payload:
        payload = {'ok': not bool(payload.get('error')), **payload}
    return payload


def titan_get(symbol: str):
    """Fetch Orion's TitanGEX levels.

    Current production API lives at titangex.com and exposes
    /api/v1/gamma/:ticker with bearer auth. Keep a couple legacy fallbacks so
    old deployments/envs do not hard-fail if TITAN_URL points elsewhere.
    """
    safe = urllib.parse.quote(symbol.upper().strip(), safe='')
    base = TITAN_URL.rstrip('/')
    paths = [
        f'/api/v1/gamma/{safe}',
        f'/api/gamma/{safe}',
        f'/v1/map?ticker={safe}',
    ]
    headers = {'User-Agent': 'pulse-render-openclaw/1.0'}
    if TITAN_API_KEY:
        headers['Authorization'] = f'Bearer {TITAN_API_KEY}'
        headers['x-api-key'] = TITAN_API_KEY
    errors = []
    for path in paths:
        url = base + path
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode('utf-8')
            try:
                data = json.loads(body)
            except Exception:
                data = {'raw': body[:4000]}
            return {
                'ok': True,
                'source': 'titangex',
                'url_path': path.split('?', 1)[0],
                'symbol': symbol.upper(),
                'data': normalize_titan_payload(data),
                'raw': data,
            }
        except Exception as exc:
            errors.append(f'{path}: {type(exc).__name__}: {exc}')
    return {'ok': False, 'source': 'titangex', 'symbol': symbol.upper(), 'error': 'No TitanGEX endpoint responded', 'attempts': errors[-3:]}


def normalize_titan_payload(data):
    if not isinstance(data, dict):
        return data
    return {
        'ticker': data.get('ticker') or data.get('symbol'),
        'spot': data.get('spotPrice') or data.get('spot'),
        'call_wall': data.get('callWall') or data.get('call_wall'),
        'put_wall': data.get('putWall') or data.get('put_wall'),
        'zero_gex': data.get('zeroGEX') or data.get('zero_gex') or data.get('gammaFlip'),
        'hvl': data.get('hvl'),
        'net_gex': data.get('netGEX') or data.get('net_gex') or data.get('totalGamma'),
        'net_call_gex': data.get('netCallGEX'),
        'net_put_gex': data.get('netPutGEX'),
        'king_nodes': data.get('kingNodes') or data.get('king_nodes'),
        'data_source': data.get('dataSource'),
        'timestamp': data.get('timestamp'),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('[pulse-api]', fmt % args, flush=True)

    def send_json(self, payload, code=200):
        body = json.dumps(payload, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip('/') or '/'
        try:
            if path == '/healthz':
                return self.send_json({'ok': True, 'service': 'pulse-api'})
            if path == '/quote':
                symbol = (qs.get('symbol') or [''])[0]
                if not symbol: return self.send_json({'ok': False, 'error': 'symbol required'}, 400)
                return self.send_json(run_json([str(ROOT / 'polygon_quote.py'), symbol], 60))
            if path == '/brain':
                symbol = (qs.get('symbol') or [''])[0]
                mode = (qs.get('mode') or ['pulse'])[0]
                if mode not in {'full','pulse','levels','news','bias','orderbook'}: mode = 'pulse'
                if not symbol: return self.send_json({'ok': False, 'error': 'symbol required'}, 400)
                return self.send_json(run_json([str(ROOT / 'trader_brain.py'), symbol, '--mode', mode], 240))
            if path == '/route':
                message = (qs.get('message') or [''])[0]
                if not message: return self.send_json({'ok': False, 'error': 'message required'}, 400)
                return self.send_json(run_json([str(ROOT / 'pulse_market_wrapper.py'), message], 240))
            if path == '/titan':
                symbol = (qs.get('symbol') or [''])[0]
                if not symbol: return self.send_json({'ok': False, 'error': 'symbol required'}, 400)
                return self.send_json(titan_get(symbol))
            return self.send_json({'ok': False, 'error': 'not found', 'paths': ['/healthz','/quote?symbol=SPY','/brain?symbol=SPY&mode=pulse','/route?message=SPY%20levels','/titan?symbol=SPY']}, 404)
        except Exception as exc:
            return self.send_json({'ok': False, 'error': str(exc)}, 500)


if __name__ == '__main__':
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    host = os.getenv('PULSE_API_HOST', '127.0.0.1')
    port = int(os.getenv('PULSE_API_PORT', '8787'))
    print(f'[pulse-api] listening on {host}:{port}', flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
