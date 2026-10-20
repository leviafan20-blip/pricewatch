"""Database leases and cooldowns coordinate every process accessing a host."""

import pytest

from apps.monitoring.scraping.errors import ScrapingError


@pytest.mark.django_db
def test_domain_rate_limit_is_shared_by_independent_instances():
    from apps.monitoring.scraping.rates import DomainRateLimiter

    with DomainRateLimiter().slot("shop.example", 10, 30):
        pass
    with pytest.raises(ScrapingError) as caught:
        with DomainRateLimiter().slot("shop.example", 10, 30):
            pytest.fail("A second instance must share the database cooldown")
    assert caught.value.code == "rate_limited"
    assert caught.value.details["retry_after_seconds"] > 0
    with DomainRateLimiter().slot("other.example", 10, 30):
        pass


@pytest.mark.django_db
def test_active_domain_lease_prevents_overlapping_requests():
    from apps.monitoring.scraping.rates import DomainRateLimiter

    with DomainRateLimiter().slot("shop.example", 0.01, 30):
        with pytest.raises(ScrapingError) as caught:
            with DomainRateLimiter().slot("shop.example", 0.01, 30):
                pytest.fail("A different limiter must not overlap an active request")
    assert caught.value.code == "rate_limited"


@pytest.mark.django_db
def test_error_releases_lease_but_preserves_request_gap():
    from apps.monitoring.models import SiteAccessState
    from apps.monitoring.scraping.rates import DomainRateLimiter

    with pytest.raises(RuntimeError):
        with DomainRateLimiter().slot("shop.example", 10, 30):
            raise RuntimeError("simulated network failure")
    state = SiteAccessState.objects.get(hostname="shop.example")
    assert state.lease_until is None
    assert state.lease_token is None
    with pytest.raises(ScrapingError) as caught:
        with DomainRateLimiter().slot("shop.example", 10, 30):
            pytest.fail("Network failures must not bypass the cooldown")
    assert caught.value.code == "rate_limited"


@pytest.mark.django_db
def test_defer_survives_other_limiter_instances():
    from apps.monitoring.scraping.rates import DomainRateLimiter

    DomainRateLimiter().defer("shop.example", 120)
    with pytest.raises(ScrapingError) as caught:
        with DomainRateLimiter().slot("shop.example", 10, 30):
            pytest.fail("A site's Retry-After must defer every loader")
    assert caught.value.code == "rate_limited"
    assert 100 < caught.value.details["retry_after_seconds"] <= 120


@pytest.mark.django_db
def test_expired_lease_can_be_replaced_without_old_owner_clearing_it(monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from apps.monitoring.models import SiteAccessState
    from apps.monitoring.scraping import rates

    clock = [timezone.now()]
    monkeypatch.setattr(rates.timezone, "now", lambda: clock[0])
    first = rates.DomainRateLimiter().slot("shop.example", 0.01, 1)
    first.__enter__()
    clock[0] += timedelta(seconds=2)
    replacement = rates.DomainRateLimiter().slot("shop.example", 0.01, 60)
    replacement.__enter__()
    token = SiteAccessState.objects.get(hostname="shop.example").lease_token
    try:
        first.__exit__(None, None, None)
        assert SiteAccessState.objects.get(hostname="shop.example").lease_token == token
    finally:
        replacement.__exit__(None, None, None)


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_requests_have_only_one_domain_lease():
    """Exercise PostgreSQL creation race, not just an existing row's lock."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from django.db import connection, connections

    from apps.monitoring.scraping.rates import DomainRateLimiter

    if connection.vendor != "postgresql":
        pytest.skip("Concurrent row locking is verified on PostgreSQL in Docker")

    start = Barrier(2)
    loser_finished = Event()

    def contend():
        try:
            start.wait(timeout=5)
            with DomainRateLimiter().slot("shop.example", 10, 30):
                assert loser_finished.wait(timeout=5)
                return "acquired"
        except ScrapingError as error:
            loser_finished.set()
            return error.code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: contend(), range(2)))
    assert sorted(outcomes) == ["acquired", "rate_limited"]
