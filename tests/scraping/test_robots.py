"""Robots decisions and safe fetching against deterministic HTTP responses."""

import socket
from contextlib import contextmanager

import httpx
import pytest

from apps.monitoring.scraping import robots
from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.fetcher import FetchPolicy, PageLoader


def rules(text):
    return robots.parse_robots(text, user_agent="PriceWatchBot")


def test_longest_matching_rule_wins_even_when_disallow_comes_first():
    parsed = rules("User-agent: *\nDisallow: /private\nAllow: /private/public\n")
    assert not parsed.can_fetch("https://shop.example/private/secret")
    assert parsed.can_fetch("https://shop.example/private/public/item")


def test_equal_specificity_allow_wins():
    parsed = rules("User-agent: *\nDisallow: /product\nAllow: /product\n")
    assert parsed.can_fetch("https://shop.example/product")


def test_specific_user_agent_takes_precedence_over_wildcard():
    parsed = rules(
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: PriceWatchBot\nAllow: /product\n"
    )
    assert parsed.can_fetch("https://shop.example/product")


def test_repeated_matching_groups_are_combined():
    parsed = rules(
        "User-agent: PriceWatchBot\nDisallow: /private\n\n"
        "User-agent: PriceWatchBot\nDisallow: /checkout\n"
    )
    assert not parsed.can_fetch("https://shop.example/private")
    assert not parsed.can_fetch("https://shop.example/checkout")
    assert parsed.can_fetch("https://shop.example/product")


def test_wildcard_and_end_anchor_are_supported():
    parsed = rules("User-agent: *\nDisallow: /*.pdf$\n")
    assert not parsed.can_fetch("https://shop.example/manual.pdf")
    assert parsed.can_fetch("https://shop.example/manual.pdf/details")


def test_query_string_participates_in_robots_matching():
    parsed = rules("User-agent: *\nDisallow: /*?private=\n")
    assert not parsed.can_fetch("https://shop.example/product?private=true")
    assert parsed.can_fetch("https://shop.example/product?variant=2")


def test_empty_disallow_allows_and_crawl_delay_is_extracted():
    parsed = rules("User-agent: *\nDisallow:\nCrawl-delay: 12\n")
    assert parsed.can_fetch("https://shop.example/product")
    assert parsed.crawl_delay == 12


def test_unreserved_percent_encoded_characters_match_their_literal_form():
    parsed = rules("User-agent: *\nDisallow: /private\n")
    assert not parsed.can_fetch("https://shop.example/%70rivate")


def test_reserved_encoded_slash_is_not_treated_as_a_path_separator():
    parsed = rules("User-agent: *\nDisallow: /private/\n")
    assert parsed.can_fetch("https://shop.example/private%2Fpublic")


def test_unicode_rules_match_utf8_encoded_url_paths():
    parsed = rules("User-agent: *\nDisallow: /личное\n")
    assert not parsed.can_fetch("https://shop.example/%D0%BB%D0%B8%D1%87%D0%BD%D0%BE%D0%B5")


def test_cached_rules_expire_and_never_live_longer_than_a_day(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(robots.time, "monotonic", lambda: clock[0])
    original = rules("User-agent: *\nDisallow: /\n")
    robots.cache_rules("cache-key", original, ttl=10 * 86400)
    assert robots.get_cached("cache-key") == original
    clock[0] = 86401.0
    assert robots.get_cached("cache-key") is None


class RecordingLimiter:
    def __init__(self):
        self.calls = []
        self.deferrals = []

    @contextmanager
    def slot(self, host, min_interval, lease_seconds):
        self.calls.append((host, min_interval))
        yield

    def defer(self, host, seconds):
        self.deferrals.append((host, seconds))


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    robots.clear_cache()
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )
    yield
    robots.clear_cache()


def make_loader(handler, limiter=None):
    return PageLoader(
        policy=FetchPolicy(allowed_hosts=("shop.example",), min_interval=0.01),
        transport=httpx.MockTransport(handler),
        limiter=limiter or RecordingLimiter(),
    )


def reply(status, text="", content_type="text/plain", **headers):
    return httpx.Response(
        status,
        headers={"content-type": content_type, **headers},
        stream=httpx.ByteStream(text.encode()),
    )


def test_disallowed_product_is_never_requested():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return reply(200, "User-agent: *\nDisallow: /private\n")

    with make_loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/private/product")
    assert caught.value.code == "robots_denied"
    assert paths == ["/robots.txt"]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_file_allows_product(status):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return reply(status)
        return reply(200, "<html>Demo</html>", "text/html")

    with make_loader(handle) as client:
        assert client.fetch("https://shop.example/product").status_code == 200
    assert paths == ["/robots.txt", "/product"]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_unavailable_or_restricted_robots_fails_closed(status):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return reply(status)

    with make_loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code in {"robots_denied", "robots_unavailable"}
    assert paths == ["/robots.txt"]


def test_robots_network_failure_prevents_product_fetch():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        raise httpx.ReadTimeout("details must not escape", request=request)

    with make_loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "robots_unavailable"
    assert paths == ["/robots.txt"]


def test_robots_redirect_is_safety_checked():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return reply(302, location="http://127.0.0.1/private")

    with make_loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code in {"robots_unavailable", "host_not_allowed"}
    assert paths == ["/robots.txt"]


def test_robots_cache_is_reused_across_loader_instances():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return reply(200, "User-agent: *\nAllow: /\n")
        return reply(200, "<html>Demo</html>", "text/html")

    with make_loader(handle) as first:
        first.fetch("https://shop.example/one")
    with make_loader(handle) as second:
        second.fetch("https://shop.example/two")
    assert paths == ["/robots.txt", "/one", "/two"]


def test_rate_limit_includes_robots_and_respects_crawl_delay():
    limiter = RecordingLimiter()

    def handle(request):
        if request.url.path == "/robots.txt":
            return reply(200, "User-agent: *\nAllow: /\nCrawl-delay: 12\n")
        return reply(200, "<html>Demo</html>", "text/html")

    with make_loader(handle, limiter) as client:
        client.fetch("https://shop.example/product")
    assert len(limiter.calls) == 2
    assert limiter.calls[0] == ("shop.example", 0.01)
    assert limiter.calls[1] == ("shop.example", 12)


def test_crawl_delay_applies_before_the_first_html_request():
    limiter = RecordingLimiter()

    def handle(request):
        if request.url.path == "/robots.txt":
            return reply(200, "User-agent: *\nAllow: /\nCrawl-delay: 12\n")
        assert ("shop.example", 12) in limiter.deferrals
        return reply(200, "<html>Demo</html>", "text/html")

    with make_loader(handle, limiter) as client:
        client.fetch("https://shop.example/product")


def test_retry_after_defers_host_without_immediate_retry():
    limiter = RecordingLimiter()
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return reply(200, "User-agent: *\nAllow: /\n")
        return reply(429, **{"retry-after": "120"})

    with make_loader(handle, limiter) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "http_error"
    assert limiter.deferrals == [("shop.example", 120)]
    assert paths == ["/robots.txt", "/product"]


def test_html_captcha_instead_of_robots_fails_closed():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return reply(200, "<html><h1>Verify you are human</h1></html>", "text/html")

    with make_loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "robots_unavailable"
    assert paths == ["/robots.txt"]


def test_oversized_robots_fails_closed():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return reply(200, "#" * (512 * 1024 + 1))

    with make_loader(handle) as client, pytest.raises(ScrapingError) as caught:
        client.fetch("https://shop.example/product")
    assert caught.value.code == "robots_unavailable"
    assert paths == ["/robots.txt"]

