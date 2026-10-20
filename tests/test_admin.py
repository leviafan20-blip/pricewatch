"""The custom user remains manageable through Django's admin."""

import pytest
from django.urls import reverse


pytestmark = pytest.mark.django_db


def test_nonstaff_user_cannot_open_admin(client, user):
    client.force_login(user)

    response = client.get(reverse("admin:index"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("admin:login"))


def test_admin_adds_custom_user_with_hashed_password(
    client, admin_user, django_user_model, password
):
    client.force_login(admin_user)
    url = reverse("admin:accounts_user_add")

    response = client.get(url)

    assert response.status_code == 200
    form = response.context["adminform"].form
    assert form._meta.model is django_user_model
    assert {"username", "password1", "password2"} <= set(form.fields)

    response = client.post(
        url,
        {
            "username": "created-in-admin",
            "email": "created@example.test",
            "password1": password,
            "password2": password,
            "usable_password": "true",
            "_save": "Save",
        },
    )

    assert response.status_code == 302
    created = django_user_model.objects.get(username="created-in-admin")
    assert created.password != password
    assert created.check_password(password)


def test_admin_edits_custom_user_without_changing_password(
    client, admin_user, user, django_user_model, password
):
    client.force_login(admin_user)
    url = reverse("admin:accounts_user_change", args=[user.pk])
    response = client.get(url)

    assert response.status_code == 200
    assert response.context["adminform"].form._meta.model is django_user_model

    response = client.post(
        url,
        {
            "username": user.username,
            "email": "updated@example.test",
            "first_name": "Maxim",
            "last_name": "",
            "is_active": "on",
            "date_joined_0": user.date_joined.strftime("%Y-%m-%d"),
            "date_joined_1": user.date_joined.strftime("%H:%M:%S"),
            "_save": "Save",
        },
    )

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.email == "updated@example.test"
    assert user.first_name == "Maxim"
    assert user.check_password(password)
