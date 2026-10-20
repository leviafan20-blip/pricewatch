"""Controlled real HTTP server for the isolated Docker integration profile.

No external product site is contacted. Redis counters make overlapping fetches
visible across independent Celery worker processes.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import redis


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scraping"
PRODUCT_PATH = re.compile(r"^/products/([a-f0-9]{32})/(success|slow|missing|error)$")


def counter_key(token, name):
    return f"pricewatch:integration:{token}:{name}"


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        # Fixtures contain no production data; keep Docker output readable.
        pass

    def respond(self, code, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.respond(200, "ok")
            return
        if path == "/robots.txt":
            self.respond(200, "User-agent: PriceWatchBot\nAllow: /\n")
            return
        match = PRODUCT_PATH.fullmatch(path)
        if not match:
            self.respond(404, "Unknown integration fixture")
            return
        token, outcome = match.groups()
        client = redis.Redis.from_url(
            os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/15"),
            decode_responses=True,
        )
        active = client.incr(counter_key(token, "active"))
        client.incr(counter_key(token, "requests"))
        if active > 1:
            client.set(counter_key(token, "overlap"), "1", ex=3600)
        for name in ("active", "requests"):
            client.expire(counter_key(token, name), 3600)
        try:
            if outcome == "slow":
                time.sleep(1)
            if outcome == "error":
                self.respond(500, "Controlled integration failure")
                return
            filename = "missing_price.html" if outcome == "missing" else "json_ld_product.html"
            body = (FIXTURES / filename).read_bytes()
            # Test-only controlled price changes keep the URL identical.
            price = client.get(counter_key(token, "price"))
            if price is not None and re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,4})?", price):
                body = body.replace(b"1299.95", price.encode("ascii"))
            self.respond(200, body, "text/html; charset=utf-8")
        finally:
            client.decr(counter_key(token, "active"))
            client.close()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), FixtureHandler).serve_forever()
