"""Authentication contracts, including redirects, escaping and CSRF."""

from urllib.parse import urlencode

import pytest
from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse
from django.utils.html import escape


pytestmark = pytest.mark.django_db


def test_anonymous_dashboard_requires_login(client):
    response = client.get(reverse("core:dashboard"))

    assert response.status_code == 302
    assert response.url == f"{reverse('accounts:login')}?next=/"


def test_successful_login_creates_session(client, user, password):
    response = client.post(
        reverse("accounts:login"),
        {"username": user.username, "password": password},
    )

    assert response.status_code == 302
    assert response.url == reverse("core:dashboard")
    assert client.session[SESSION_KEY] == str(user.pk)
    assert client.get(reverse("core:dashboard")).status_code == 200


def test_wrong_credentials_do_not_disclose_whether_user_exists(client, user):
    errors = []
    for username in (user.username, "does-not-exist"):
        response = client.post(
            reverse("accounts:login"),
            {"username": username, "password": "incorrect-password"},
        )

        assert response.status_code == 200
        assert SESSION_KEY not in client.session
        form = response.context["form"]
        assert form.errors
        errors.append(form.non_field_errors().as_data())

    assert errors[0][0].code == errors[1][0].code
    assert errors[0][0].messages == errors[1][0].messages


def test_inactive_user_cannot_login(client, user, password):
    user.is_active = False
    user.save(update_fields=["is_active"])

    response = client.post(
        reverse("accounts:login"),
        {"username": user.username, "password": password},
    )

    assert response.status_code == 200
    assert response.context["form"].errors
    assert SESSION_KEY not in client.session


@pytest.mark.parametrize("source", ["query", "post"])
@pytest.mark.parametrize(
    "destination",
    ["https://evil.example/collect", "//evil.example/collect"],
)
def test_login_rejects_external_redirects(client, user, password, source, destination):
    url = reverse("accounts:login")
    data = {"username": user.username, "password": password}
    if source == "query":
        url = f"{url}?{urlencode({'next': destination})}"
    else:
        data["next"] = destination

    response = client.post(url, data)

    assert response.status_code == 302
    assert response.url == reverse("core:dashboard")


@pytest.mark.parametrize("source", ["query", "post"])
def test_login_accepts_local_next(client, user, password, source):
    destination = "/?view=summary"
    url = reverse("accounts:login")
    data = {"username": user.username, "password": password}
    if source == "query":
        url = f"{url}?{urlencode({'next': destination})}"
    else:
        data["next"] = destination

    response = client.post(url, data)

    assert response.status_code == 302
    assert response.url == destination


def test_authenticated_user_can_still_open_login_page(client, user):
    client.force_login(user)

    assert client.get(reverse("accounts:login")).status_code == 200


def test_dashboard_escapes_username(client, django_user_model, password):
    unsafe_username = '<script>alert("x")</script>'
    user = django_user_model.objects.create_user(
        username=unsafe_username, password=password
    )
    client.force_login(user)

    response = client.get(reverse("core:dashboard"))

    assert response.status_code == 200
    content = response.content.decode()
    assert unsafe_username not in content
    assert escape(unsafe_username) in content


def test_login_requires_valid_csrf_token(user, password):
    csrf_client = Client(enforce_csrf_checks=True)
    url = reverse("accounts:login")
    data = {"username": user.username, "password": password}

    assert csrf_client.post(url, data).status_code == 403
    assert SESSION_KEY not in csrf_client.session

    assert csrf_client.get(url).status_code == 200
    token = csrf_client.cookies["csrftoken"].value
    response = csrf_client.post(url, {**data, "csrfmiddlewaretoken": token})

    assert response.status_code == 302
    assert csrf_client.session[SESSION_KEY] == str(user.pk)


def test_logout_is_post_only_and_clears_session(client, user):
    client.force_login(user)
    url = reverse("accounts:logout")

    assert client.get(url).status_code == 405
    assert client.session[SESSION_KEY] == str(user.pk)

    response = client.post(url)

    assert response.status_code == 302
    assert response.url == reverse("accounts:login")
    assert SESSION_KEY not in client.session
    assert client.get(reverse("core:dashboard")).status_code == 302


def test_logout_requires_valid_csrf_token(user):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    csrf_client.get(reverse("accounts:login"))
    url = reverse("accounts:logout")

    assert csrf_client.post(url).status_code == 403
    assert csrf_client.session[SESSION_KEY] == str(user.pk)

    token = csrf_client.cookies["csrftoken"].value
    response = csrf_client.post(url, {"csrfmiddlewaretoken": token})

    assert response.status_code == 302
    assert SESSION_KEY not in csrf_client.session


@pytest.mark.parametrize(
    "url",
    ["/accounts/signup/", "/accounts/register/", "/accounts/password_reset/"],
)
def test_public_account_creation_and_reset_are_not_exposed(client, url):
    assert client.get(url).status_code == 404


def test_user_password_is_hashed(user, password):
    assert user.password != password
    assert user.check_password(password)
    assert not user.check_password("incorrect-password")
