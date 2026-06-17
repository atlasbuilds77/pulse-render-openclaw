#!/usr/bin/env python3
"""Public Render entrypoint proxy for Pulse.

Routes /pulse-api/* to the restricted local Pulse API and everything else to
OpenClaw Gateway. This lets the Discord agent use web_fetch against the public
Render URL without exposing shell/filesystem tools.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PULSE_API = os.getenv('PULSE_API_INTERNAL_URL', 'http://127.0.0.1:8787').rstrip('/')
GATEWAY = os.getenv('OPENCLAW_GATEWAY_INTERNAL_URL', 'http://127.0.0.1:18789').rstrip('/')
HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length'
}


def target_for(path: str) -> str:
    if path == '/pulse-api' or path.startswith('/pulse-api/'):
        stripped = path[len('/pulse-api'):] or '/'
        return PULSE_API + stripped
    return GATEWAY + path


class Proxy(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        print('[pulse-proxy]', fmt % args, flush=True)

    def do_GET(self):
        self.forward()

    def do_HEAD(self):
        self.forward(head_only=True)

    def forward(self, head_only: bool = False):
        parsed = urllib.parse.urlsplit(self.path)
        url = target_for(parsed.path)
        if parsed.query:
            url += '?' + parsed.query
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_HEADERS
        }
        headers['X-Forwarded-Host'] = self.headers.get('Host', '')
        headers['X-Forwarded-Proto'] = self.headers.get('X-Forwarded-Proto', 'https')
        req = urllib.request.Request(url, headers=headers, method='HEAD' if head_only else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = b'' if head_only else resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in HOP_HEADERS:
                        continue
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = b'' if head_only else exc.read()
            self.send_response(exc.code)
            for k, v in exc.headers.items():
                if k.lower() in HOP_HEADERS:
                    continue
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except Exception as exc:
            body = (f'proxy_error: {type(exc).__name__}: {exc}\n').encode()
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)


if __name__ == '__main__':
    host = os.getenv('PULSE_PROXY_HOST', '0.0.0.0')
    port = int(os.getenv('PORT', os.getenv('PULSE_PROXY_PORT', '10000')))
    print(f'[pulse-proxy] listening on {host}:{port}; /pulse-api -> {PULSE_API}; gateway -> {GATEWAY}', flush=True)
    ThreadingHTTPServer((host, port), Proxy).serve_forever()
