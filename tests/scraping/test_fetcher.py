"""Loader integration against HTTPX's transport boundary, never live stores."""

import gzip
import logging
import socket
import ssl
import time
import zlib
from contextlib import contextmanager

import httpcore
import httpx
import pytest

from apps.monitoring.scraping import robots
from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.fetcher import FetchPolicy, PageLoader
from apps.monitoring.scraping.transport import PinnedTransport


PUBLIC_IP = "93.184.216.34"
HTML = b"<!doctype html><html><body><h1>Demo product</h1></body></html>"


class NoWaitLimiter:
    """Only network tests skip the database throttle; real throttle has own tests."""

    @contextmanager
    def slot(self, host, min_interval, lease_seconds):
        yield

    def defer(self, host, seconds):
        pass


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    robots.clear_cache()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, 443))
        ],
    )
    yield
    robots.clear_cache()


def response(status=200, body=HTML, **headers):
    return httpx.Response(
        status,
        headers={"content-type": "text/html; charset=utf-8", **headers},
        stream=httpx.ByteStream(body),
    )


def loader(handler, **overrides):
    policy = FetchPolicy(
        allowed_hosts=("shop.example", "other.example"),
        min_interval=0.01,
        **overrides,
    )
    return PageLoader(
        policy=policy,
        transport=httpx.MockTransport(handler),
        limiter=NoWaitLimiter(),
    )


def permit_robots(handler):
    def route(request):
        if request.url.path == "/robots.txt":
            return response(200, b"User-agent: *\nAllow: /\n", **{"content-type": "text/plain"})
        return handler(request)

    return route


def test_loads_html_and_does_not_fetch_subresources():
    seen = []

    def handle(request):
        seen.append((request.url.host, request.url.path))
        assert request.headers["user-agent"].startswith("PriceWatchBot/")
        assert request.extensions["pinned_ip"] == PUBLIC_IP
        return response()

    with loader(permit_robots(handle)) as client:
        page = client.fetch("https://shop.example/product?variant=2")
    assert page.html == HTML.decode()
    assert page.url == "https://shop.example/product?variant=2"
    assert page.status_code == 200
    assert page.bytes_received == len(HTML)
    assert seen == [("shop.example", "/product")]


def test_sensitive_url_query_is_not_written_to_application_logs(caplog):
    caplog.set_level(logging.INFO)
    with loader(permit_robots(lambda request: response())) as client:
        client.fetch("https://shop.example/product?token=private-query-secret")
    assert "private-query-secret" not in caplog.text


def test_relative_redirect_is_followed_and_returns_final_url():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/old":
            return response(302, b"", location="/new?variant=2")
        return response()

    with loader(permit_robots(handle)) as client:
        page = client.fetch("https://shop.example/old")
    assert paths == ["/old", "/new"]
    assert page.url == "https://shop.example/new?variant=2"


def test_cross_host_redirect_checks_target_robots_before_product():
    seen = []

    def handle(request):
        seen.append((request.url.host, request.url.path))
        if request.url.path == "/robots.txt":
            rules = b"User-agent: *\nDisallow: /\n" if request.url.host == "other.example" else b"User-agent: *\nAllow: /\n"
            return response(200, rules, **{"content-type": "text/plain"})
        return response(302, b"", location="https://other.example/blocked")

    with loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/start")
    assert caught.value.code == "robots_denied"
    assert seen == [
        ("shop.example", "/robots.txt"),
        ("shop.example", "/start"),
        ("other.example", "/robots.txt"),
    ]


@pytest.mark.parametrize(
    ("target", "expected_code"),
    [
        ("http://127.0.0.1/admin", "unsafe_address"),
        ("http://169.254.169.254/latest/meta-data/", "unsafe_address"),
        ("https://shop.example.evil.example/product", "host_not_allowed"),
        ("file:///etc/passwd", "invalid_url"),
    ],
)
def test_dangerous_redirect_never_reaches_target(target, expected_code):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return response(302, b"", location=target)

    with loader(permit_robots(handle)) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/start")
    assert caught.value.code == expected_code
    assert seen == ["https://shop.example/start"]


def test_dns_is_rechecked_for_redirect_even_on_same_host(monkeypatch):
    calls = []
    current_address = [PUBLIC_IP]

    def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (current_address[0], 443))]

    def handle(request):
        calls.append(request.url.path)
        current_address[0] = "127.0.0.1"
        return response(302, b"", location="/redirected")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with loader(permit_robots(handle)) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "unsafe_address"
    assert calls == ["/product"]


def test_redirect_loop_is_bounded():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return response(302, b"", location="/loop")

    with loader(permit_robots(handle), max_redirects=2) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/loop")
    assert caught.value.code == "redirect_limit"
    assert 1 <= len(calls) <= 3


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_without_location_has_diagnostic(status):
    with loader(permit_robots(lambda request: response(status, b""))) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code in {"invalid_redirect", "http_error"}


def test_malformed_redirect_location_has_a_safe_diagnostic():
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return response(302, b"", location="https://[invalid")

    with loader(permit_robots(handle)) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "invalid_redirect"
    assert seen == ["/product"]


def test_unavailable_database_limiter_blocks_network_and_redacts_its_error():
    from django.db import OperationalError

    class UnavailableLimiter:
        @contextmanager
        def slot(self, host, min_interval, lease_seconds):
            raise OperationalError("password=database-secret-in-exception")
            yield

    def handle(request):
        pytest.fail("The loader must stop when its shared rate limit is unavailable")

    with PageLoader(
        policy=FetchPolicy(allowed_hosts=("shop.example",), min_interval=0.01),
        transport=httpx.MockTransport(handle),
        limiter=UnavailableLimiter(),
    ) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "rate_limit_unavailable"
    assert "database-secret" not in str(caught.value)
    assert "database-secret" not in str(caught.value.details)


def test_mislabeled_robots_html_with_comment_prefix_fails_closed():
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return response(
            body=b"<!-- challenge -->\n<html><form>captcha</form></html>",
            **{"content-type": "text/plain"},
        )

    with loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "robots_unavailable"
    assert seen == ["/robots.txt"]


@pytest.mark.parametrize("cache_control", ["no-cache", "no-store", "max-age=0"])
def test_robots_cache_control_can_require_refetching(cache_control):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(
                body=b"User-agent: *\nAllow: /\n",
                **{"content-type": "text/plain", "cache-control": cache_control},
            )
        return response()

    with loader(handle) as client:
        client.fetch("https://shop.example/one")
        client.fetch("https://shop.example/two")
    assert seen == ["/robots.txt", "/one", "/robots.txt", "/two"]


def test_robots_max_age_shortens_the_configured_cache_ttl(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(robots.time, "monotonic", lambda: clock[0])
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(
                body=b"User-agent: *\nAllow: /\n",
                **{"content-type": "text/plain", "cache-control": "public, max-age=5"},
            )
        return response()

    with loader(handle) as client:
        client.fetch("https://shop.example/one")
        clock[0] = 4.0
        client.fetch("https://shop.example/two")
        clock[0] = 6.0
        client.fetch("https://shop.example/three")
    assert seen == ["/robots.txt", "/one", "/two", "/robots.txt", "/three"]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_unsuccessful_product_http_status_does_not_return_html(status):
    with loader(permit_robots(lambda request: response(status))) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "http_error"
    assert caught.value.details["status_code"] == status


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectError, "network_error"),
        (httpx.RemoteProtocolError, "network_error"),
    ],
)
def test_network_failures_are_safe_and_classified(exception_type, expected_code):
    def handle(request):
        raise exception_type("secret-token-in-internal-exception", request=request)

    with loader(permit_robots(handle)) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product?token=private")
    assert caught.value.code == expected_code
    assert "secret-token" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert "secret-token" not in str(caught.value.details)


@pytest.mark.parametrize("content_type", ["application/json", "image/png", "application/pdf"])
def test_non_html_content_type_is_rejected(content_type):
    with loader(permit_robots(lambda request: response(**{"content-type": content_type}))) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "unsupported_content_type"


def test_declared_content_length_exceeding_limit_is_rejected():
    with loader(permit_robots(lambda request: response(**{"content-length": "4096"})), max_bytes=1024) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "response_too_large"


def test_streamed_body_without_content_length_is_bounded():
    with loader(permit_robots(lambda request: response(body=b"x" * 1025)), max_bytes=1024) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "response_too_large"


def test_decompressed_body_limit_rejects_small_gzip_with_large_payload():
    compressed = gzip.compress(b"x" * 4096)
    assert len(compressed) < 1024
    handler = permit_robots(
        lambda request: response(
            body=compressed,
            **{"content-encoding": "gzip", "content-length": str(len(compressed))},
        )
    )
    with loader(handler, max_bytes=1024) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "response_too_large"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_successfully_loads_compressed_html(encoding):
    encoded = gzip.compress(HTML) if encoding == "gzip" else zlib.compress(HTML)
    handler = permit_robots(
        lambda request: response(body=encoded, **{"content-encoding": encoding})
    )
    with loader(handler) as client:
        page = client.fetch("https://shop.example/product")
    assert page.html == HTML.decode()
    assert page.bytes_received == len(HTML)


@pytest.mark.parametrize("body", [b"not gzip", gzip.compress(HTML)[:-5]])
def test_corrupted_or_truncated_compression_has_a_safe_diagnostic(body):
    handler = permit_robots(
        lambda request: response(body=body, **{"content-encoding": "gzip"})
    )
    with loader(handler) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<html>Товар</html>".encode(), "<html>Товар</html>"),
        (b"\xef\xbb\xbf<html>Demo</html>", "<html>Demo</html>"),
        (
            '<meta charset="windows-1251"><h1>Товар</h1>'.encode("cp1251"),
            '<meta charset="windows-1251"><h1>Товар</h1>',
        ),
    ],
)
def test_missing_http_charset_uses_meta_or_utf8(body, expected):
    handler = permit_robots(
        lambda request: response(body=body, **{"content-type": "text/html"})
    )
    with loader(handler) as client:
        assert client.fetch("https://shop.example/product").html == expected


def test_unknown_declared_charset_has_a_diagnostic():
    handler = permit_robots(
        lambda request: response(**{"content-type": "text/html; charset=unknown-charset"})
    )
    with loader(handler) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "invalid_encoding"


def test_httpx_has_explicit_timeouts_and_no_automatic_redirects():
    def handle(request):
        timeout = request.extensions["timeout"]
        assert timeout["connect"] > 0
        assert timeout["read"] > 0
        assert timeout["write"] > 0
        assert timeout["pool"] > 0
        return response()

    with loader(permit_robots(handle)) as client:
        client.fetch("https://shop.example/product")



class RecordingStream(httpcore.NetworkStream):
    def __init__(self):
        self.writes = []
        self.tls = []
        self.chunks = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/html\r\n\r\nok"]
        self.closed = False

    def read(self, max_bytes, timeout=None):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((ssl_context, server_hostname))
        return self

    def get_extra_info(self, info):
        return None

    def close(self):
        self.closed = True


class RecordingBackend(httpcore.NetworkBackend):
    def __init__(self, stream=None):
        self.calls = []
        self.stream = stream or RecordingStream()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port, timeout))
        return self.stream


def test_pinned_transport_connects_to_ip_but_preserves_host_and_tls_identity():
    backend = RecordingBackend()
    with httpx.Client(transport=PinnedTransport(network_backend=backend), trust_env=False) as client:
        result = client.get(
            "https://shop.example/product?variant=2",
            extensions={"pinned_ip": PUBLIC_IP, "deadline": time.monotonic() + 30},
        )
    assert result.text == "ok"
    assert backend.calls[0][:2] == (PUBLIC_IP, 443)
    assert 0 < backend.calls[0][2] <= 30
    wire = b"".join(backend.stream.writes)
    assert b"GET /product?variant=2 HTTP/1.1\r\n" in wire
    assert b"Host: shop.example\r\n" in wire
    context, hostname = backend.stream.tls[0]
    assert hostname in {"shop.example", b"shop.example"}
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert backend.stream.closed


def test_pinned_transport_rejects_missing_checked_address(monkeypatch):
    def send(*args, **kwargs):
        pytest.fail("Transport must not resolve or connect without a checked IP")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", send)
    with httpx.Client(transport=PinnedTransport(), trust_env=False) as client, pytest.raises(ScrapingError):
        client.get("https://shop.example/product")


def test_pinned_transport_ignores_proxy_and_certificate_environment(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:8888")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/untrusted-ca.pem")
    backend = RecordingBackend()
    with httpx.Client(transport=PinnedTransport(network_backend=backend), trust_env=False) as client:
        client.get(
            "https://shop.example/product",
            extensions={"pinned_ip": PUBLIC_IP, "deadline": time.monotonic() + 30},
        )
    assert backend.calls[0][:2] == (PUBLIC_IP, 443)


def test_total_deadline_stops_slow_header_trickle(monkeypatch):
    from apps.monitoring.scraping import transport

    clock = [0.0]

    class TrickleStream(RecordingStream):
        def read(self, max_bytes, timeout=None):
            clock[0] += 1
            return b"H"

    backend = RecordingBackend(TrickleStream())
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    with httpx.Client(transport=PinnedTransport(network_backend=backend), trust_env=False) as client, pytest.raises(httpx.TimeoutException):
        client.get(
            "https://shop.example/product",
            extensions={"pinned_ip": PUBLIC_IP, "deadline": 2.5},
        )
    assert clock[0] <= 3
    assert backend.stream.closed
