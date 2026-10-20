"""Liveness is independent of PostgreSQL; readiness verifies the connection."""

from unittest.mock import patch

import pytest
from django.db import OperationalError


def test_liveness_does_not_need_database(client):
    response = client.get("/health/live/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readiness_reports_database_available(client):
    response = client.get("/health/ready/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_hides_database_failure_details(client):
    with patch(
        "apps.core.views.connection.cursor",
        side_effect=OperationalError("password=secret; internal-host:5432"),
    ):
        response = client.get("/health/ready/")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "secret" not in response.content.decode()
