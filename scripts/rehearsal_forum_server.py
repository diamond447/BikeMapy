#!/usr/bin/env python3
"""Serve a deterministic two-page forum fixture for the crawler rehearsal."""

from __future__ import annotations

import argparse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIRST_PAGE = """<!doctype html><html><body>
<h1>Launch rehearsal ridge</h1>
<article data-post-id="post-1"><a class="permalink" href="/t/42#post-1">post</a>
<span class="author">rider-one</span><div class="content">First imported post.</div>
<a href="https://mapy.com/s/rehearsal-one">Mapy one</a></article>
<a rel="next" href="/t/42?page=2">next</a>
</body></html>"""
SECOND_PAGE = """<!doctype html><html><body>
<h1>Launch rehearsal ridge</h1>
<article data-post-id="post-1"><a class="permalink" href="/t/42#post-1">post</a>
<span class="author">rider-one</span><div class="content">First imported post.</div>
<a href="https://mapy.com/s/rehearsal-one">Mapy one</a></article>
<article data-post-id="post-2"><a class="permalink" href="/t/42#post-2">post</a>
<span class="author">rider-two</span><div class="content">Resumed post.</div>
<a href="https://mapy.com/s/rehearsal-two">Mapy two</a></article>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    # Stay below the crawler's normal 15-second request timeout while leaving
    # enough time for the host harness to kill the worker mid-request.
    delay_seconds = 8

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/t/42":
            body = FIRST_PAGE
        elif self.path == "/t/42?page=2":
            # Leave a real worker in the HTTP call while the rehearsal kills it.
            time.sleep(self.delay_seconds)
            body = SECOND_PAGE
        else:
            self.send_error(404)
            return
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"rehearsal forum listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
