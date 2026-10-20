"""Explicit test-only Celery entry point; never imported by production settings.

The real loader validates a public DNS answer and uses its pinned transport.
Only the TCP endpoint is redirected to our controlled Docker HTTP fixture.
"""

from dataclasses import replace
import os
import socket

import httpcore

if os.environ.get("PRICEWATCH_INTEGRATION") != "1":
    raise RuntimeError("This worker is restricted to PRICEWATCH_INTEGRATION=1")

from config.celery import app  # noqa: E402
import django  # noqa: E402

django.setup()

from apps.monitoring import services  # noqa: E402
from apps.monitoring.scraping import safety  # noqa: E402
from apps.monitoring.scraping.fetcher import PageLoader  # noqa: E402
from apps.monitoring.scraping.transport import PinnedTransport  # noqa: E402


FIXTURE_HOST = "shop.pricewatch.example"
PUBLIC_IP = "93.184.216.34"
_original_resolve = socket.getaddrinfo


def fixture_dns(host, port, *args, **kwargs):
    if host == FIXTURE_HOST:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, int(port)))]
    return _original_resolve(host, port, *args, **kwargs)


class FixtureBackend(httpcore.NetworkBackend):
    def __init__(self):
        self.backend = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != PUBLIC_IP or port != 80:
            raise AssertionError("Integration transport received an unexpected destination")
        return self.backend.connect_tcp(
            "fixture", 8080, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )


def fixture_loader(policy):
    # A tiny nonzero interval keeps the production rate limiter in the path while
    # making repeated controlled fixture requests practical in the test suite.
    return PageLoader(
        replace(policy, min_interval=0.01),
        transport=PinnedTransport(network_backend=FixtureBackend()),
    )


# Patching is restricted to this explicit worker process. Production workers keep
# their original DNS resolver, transport and domain intervals.
safety.socket.getaddrinfo = fixture_dns
services.PageLoader = fixture_loader
