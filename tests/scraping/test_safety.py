"""Adversarial URL and DNS checks. No test resolves a real hostname."""

import socket

import pytest

from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.safety import resolve_public_ips, validate_url


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "ftp://shop.example/product",
        "//shop.example/product",
        "https://user:password@shop.example/product",
        "https://shop.example:8443/product",
        "http://shop.example:443/product",
        "https://shop.example:80/product",
        "https://shop.example\\@127.0.0.1/product",
        "https://shop.example/\r\nHeader: injected",
        "https://shop.example/\x00product",
        "https://shop.example/product#fragment",
        "https://%73hop.example/product",
        "https://shop.example:/product",
        "https://shop.example:bad/product",
        "https:///product",
        "https://shop..example/product",
        "https://-shop.example/product",
    ],
)
def test_rejects_ambiguous_or_unsupported_urls(raw):
    with pytest.raises(ScrapingError) as caught:
        validate_url(raw, ("shop.example",))
    assert caught.value.code == "invalid_url"


@pytest.mark.parametrize(
    "host",
    ["evil.example", "shop.example.evil.example", "sub.shop.example"],
)
def test_allowlist_matches_exact_host_only(host):
    with pytest.raises(ScrapingError) as caught:
        validate_url(f"https://{host}/product", ("shop.example",))
    assert caught.value.code == "host_not_allowed"


def test_empty_allowlist_fails_closed():
    with pytest.raises(ScrapingError) as caught:
        validate_url("https://shop.example/product", ())
    assert caught.value.code == "host_not_allowed"


def test_normalizes_host_and_default_port_without_changing_query():
    result = validate_url(
        "HTTPS://SHOP.EXAMPLE:443/product?variant=2&campaign=a%2Fb",
        ("shop.example",),
    )
    assert result.host == "shop.example"
    assert result.port == 443
    assert result.origin == "https://shop.example"
    assert result.url == "https://shop.example/product?variant=2&campaign=a%2Fb"


def test_unicode_hostname_is_normalized_before_exact_allowlist_match():
    result = validate_url("https://магазин.example/product", ("xn--80aairftm.example",))
    assert result.host == "xn--80aairftm.example"


def dns_answers(*addresses):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443, 0, 0) if ":" in address else (address, 443),
        )
        for address in addresses
    ]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.0.1",
        "169.254.169.254", "100.64.0.1", "192.0.2.1", "198.51.100.1",
        "203.0.113.1", "224.0.0.1", "255.255.255.255", "::", "::1",
        "fc00::1", "fe80::1", "ff02::1", "2001:db8::1", "::ffff:127.0.0.1",
    ],
)
def test_rejects_non_public_dns_addresses(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_answers(address))
    with pytest.raises(ScrapingError) as caught:
        resolve_public_ips("shop.example", 443)
    assert caught.value.code == "unsafe_address"


def test_rejects_mixed_public_and_private_dns_results(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: dns_answers("93.184.216.34", "::1"),
    )
    with pytest.raises(ScrapingError) as caught:
        resolve_public_ips("shop.example", 443)
    assert caught.value.code == "unsafe_address"


def test_returns_public_ipv4_and_ipv6_without_duplicates(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: dns_answers(
            "93.184.216.34", "2606:4700:4700::1111", "93.184.216.34"
        ),
    )
    assert resolve_public_ips("shop.example", 443) == (
        "93.184.216.34", "2606:4700:4700::1111",
    )


@pytest.mark.parametrize("empty", [True, False])
def test_dns_failure_has_safe_diagnostic(monkeypatch, empty):
    def resolve(*args, **kwargs):
        if empty:
            return []
        raise socket.gaierror("internal resolver failure detail")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(ScrapingError) as caught:
        resolve_public_ips("shop.example", 443)
    assert caught.value.code == "dns_error"
    assert "internal resolver" not in str(caught.value)


def test_dns_resolution_has_a_deadline(monkeypatch):
    from threading import Event

    release = Event()

    def resolve(*args, **kwargs):
        assert release.wait(timeout=2)
        return dns_answers("93.184.216.34")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        with pytest.raises(ScrapingError) as caught:
            resolve_public_ips("shop.example", 443, timeout=0.001)
        assert caught.value.code == "timeout"
    finally:
        release.set()
