"""Shared fixtures for the project foundation's behaviour tests."""

import pytest


@pytest.fixture
def password():
    return "Correct-horse-battery-49!"


@pytest.fixture
def user(db, django_user_model, password):
    return django_user_model.objects.create_user(
        username="maxim",
        email="maxim@example.test",
        password=password,
    )
