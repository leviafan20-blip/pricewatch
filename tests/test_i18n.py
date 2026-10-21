"""Language negotiation, safe switching and locale-independent application data."""

from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile
import json
import re

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape
import pytest

from apps.monitoring.forms import MonitorForm
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.templatetags.monitoring import amount, translated_diagnostic
from apps.notifications.forms import EmailPreferencesForm
from apps.notifications.services import create_price_notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def localized_monitor(user):
    monitor = Monitor.objects.create(owner=user, name='Кофемашина <Demo>', url='https://shop.example/item')
    now = timezone.now()
    for price in ('1299.9500', '999.9500'):
        run = CheckRun.objects.create(monitor=monitor, status='succeeded', url_snapshot=monitor.url,
                                      queued_at=now, started_at=now, finished_at=now)
        Observation.objects.create(check_run=run, title=monitor.name, price=Decimal(price),
                                   currency='USD', availability='in_stock', source='json_ld', observed_at=now)
    CheckRun.objects.create(monitor=monitor, status='failed', url_snapshot=monitor.url,
                            finished_at=now, queued_at=now, error_code='timeout',
                            error_message='Сайт не ответил за отведённое время.')
    return monitor


def select(client, language, next_url='/'):
    return client.post(reverse('set_language'), {'language': language, 'next': next_url})


@pytest.mark.parametrize('browser_language', ['en', 'ru', 'fr'])
def test_default_language_is_english(client, browser_language):
    assert settings.LANGUAGE_CODE == 'en'
    response = client.get(reverse('accounts:login'), HTTP_ACCEPT_LANGUAGE=browser_language)
    assert response.status_code == 200
    assert '<html lang="en">' in response.content.decode()
    assert 'Sign in to PriceWatch' in response.content.decode()


@pytest.mark.parametrize('language,label', [('ru', 'Войти в PriceWatch'), ('en', 'Sign in to PriceWatch')])
def test_switch_is_available_before_login_and_cookie_persists(client, language, label):
    login = client.get(reverse('accounts:login'))
    assert reverse('set_language') in login.content.decode()
    response = select(client, language, reverse('accounts:login') + '?next=%2Fmonitors%2F')
    assert response.url == reverse('accounts:login') + '?next=%2Fmonitors%2F'
    cookie = response.cookies[settings.LANGUAGE_COOKIE_NAME]
    assert cookie.value == language and int(cookie['max-age']) == 365 * 24 * 60 * 60
    assert cookie['samesite'] == 'Lax'
    reopened = Client()
    reopened.cookies[settings.LANGUAGE_COOKIE_NAME] = cookie.value
    assert label in reopened.get(reverse('accounts:login'), HTTP_ACCEPT_LANGUAGE='fr').content.decode()


def test_cookie_overrides_browser_language_without_losing_session(client, user):
    client.force_login(user)
    identity = client.session[SESSION_KEY]
    for language, other in [('ru', 'en'), ('en', 'ru')]:
        assert select(client, language, '/monitors/?state=archived&page=2').url == '/monitors/?state=archived&page=2'
        response = client.get('/monitors/', HTTP_ACCEPT_LANGUAGE=other)
        assert response.status_code == 200 and response['Content-Language'] == language
        assert client.session[SESSION_KEY] == identity


def test_set_language_requires_csrf_and_get_does_not_select_language():
    client = Client(enforce_csrf_checks=True)
    assert select(client, 'ru').status_code == 403
    response = client.get(reverse('set_language'), {'language': 'ru', 'next': '/'})
    assert settings.LANGUAGE_COOKIE_NAME not in response.cookies
    client.get(reverse('accounts:login'))
    token = client.cookies['csrftoken'].value
    response = client.post(reverse('set_language'), {'language': 'ru', 'next': '/', 'csrfmiddlewaretoken': token})
    assert response.status_code == 302 and response.cookies[settings.LANGUAGE_COOKIE_NAME].value == 'ru'


@pytest.mark.parametrize('destination', ['https://evil.example/', '//evil.example/', 'javascript:alert(1)', '/\\evil.example/'])
def test_language_switch_rejects_external_redirects(client, destination):
    response = select(client, 'ru', destination)
    assert response.status_code == 302 and response.url == '/'


def test_unknown_language_does_not_set_cookie(client):
    response = select(client, 'xx')
    assert settings.LANGUAGE_COOKIE_NAME not in response.cookies


@pytest.mark.parametrize('language', ['en', 'ru'])
@pytest.mark.parametrize('view,label_en,label_ru', [
    ('core:dashboard', 'Workspace', 'Рабочее пространство'),
    ('monitoring:list', 'My monitors', 'Мои мониторинги'),
    ('monitoring:create', 'Add product', 'Добавить товар'),
    ('monitoring:detail', 'Last known price', 'Последняя известная цена'),
    ('monitoring:edit', 'Edit product', 'Редактировать товар'),
    ('monitoring:archive', 'Monitor management', 'Управление мониторингом'),
    ('monitoring:compare', 'Product comparison', 'Сравнение товаров'),
    ('monitoring:export_all', 'Export history', 'Экспорт истории'),
    ('notifications:list', 'Notifications', 'Уведомления'),
    ('notifications:settings', 'Email preferences', 'Настройки email'),
])
def test_main_pages_in_both_languages(client, user, localized_monitor, language, view, label_en, label_ru):
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    args = [localized_monitor.pk] if view in {'monitoring:detail', 'monitoring:edit', 'monitoring:archive'} else []
    response = client.get(reverse(view, args=args))
    assert response.status_code == 200
    assert (label_en if language == 'en' else label_ru) in response.content.decode()
    assert f'<html lang="{language}">' in response.content.decode()


def test_chart_payload_and_user_data_are_unchanged_by_locale(client, user, localized_monitor):
    client.force_login(user)
    payloads = []
    for language in ('en', 'ru'):
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = client.get(reverse('monitoring:detail', args=[localized_monitor.pk]), {'period':'all'})
        content = response.content.decode()
        assert escape(localized_monitor.name) in content
        assert ('The site did not respond in time.' if language == 'en' else 'Сайт не ответил за отведённое время.') in content
        payloads.append(json.loads(re.search(r'<script id="price-chart-data" type="application/json">(.*?)</script>', content, re.S)[1]))
        assert client.get('/health/ready/').json() == {'status':'ok'}
    assert payloads[0] == payloads[1]
    assert payloads[0]['currency'] == 'USD'
    assert payloads[0]['series'][0]['label'] == localized_monitor.name
    assert Decimal(payloads[0]['series'][0]['points'][0]['price']) == Decimal('1299.95')


@pytest.mark.parametrize('language', ['en','ru'])
def test_translated_validation_errors_and_labels(language):
    with translation.override(language):
        form = MonitorForm({'name':'Demo', 'url':'http://127.0.0.1/'})
        assert not form.is_valid()
        assert ('Internal and reserved IP addresses are not allowed.' if language == 'en' else 'Внутренние и служебные IP-адреса запрещены.') in str(form.errors)
        email = EmailPreferencesForm({'email_notifications_enabled':'on'})
        assert not email.is_valid()
        assert ('Enter a valid email address' if language == 'en' else 'укажите корректный адрес') in str(email.errors)
        assert str(Monitor.Interval.HOUR.label) == ('Every hour' if language == 'en' else 'Каждый час')
        assert str(CheckRun.Status.QUEUED.label) == ('Queued' if language == 'en' else 'В очереди')


@pytest.mark.parametrize('language,expected', [('en','1,299.9500000001'),('ru','1\u00a0299,9500000001')])
def test_money_format_keeps_all_decimal_digits(language, expected):
    with translation.override(language):
        assert amount(Decimal('1299.9500000001')) == expected
        assert amount(Decimal('-0.0000000001')) == ('-0.0000000001' if language == 'en' else '-0,0000000001')
        assert translated_diagnostic('availability_unknown') == ('Availability could not be determined.' if language == 'en' else 'Не удалось определить наличие товара.')


@pytest.mark.parametrize('file_format',['csv','xlsx'])
def test_exports_keep_stable_headers_prices_and_codes(client, user, localized_monitor, file_format):
    client.force_login(user)
    outputs = []
    for language in ('en','ru'):
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = client.get(reverse('monitoring:export',args=[localized_monitor.pk]), {'period':'all','format':file_format})
        assert response.status_code == 200
        output = b''.join(response.streaming_content)
        if file_format == 'xlsx':
            with ZipFile(BytesIO(output)) as archive:
                output = archive.read('xl/worksheets/sheet1.xml')
                ElementTree.fromstring(output)
        outputs.append(output)
    assert outputs[0] == outputs[1]
    assert b'Price exact' in outputs[0] and b'1299.9500000000' in outputs[0]
    assert b'USD' in outputs[0]
    if file_format == 'csv':
        assert localized_monitor.name.encode() in outputs[0]


def test_notifications_switch_language_without_rewriting_events(client, user, localized_monitor):
    localized_monitor.notification_mode = 'any'
    observations = list(Observation.objects.filter(check_run__monitor=localized_monitor).order_by('pk'))
    notice = create_price_notification(localized_monitor, observations[-1])
    assert notice is not None
    client.force_login(user)
    for language, label in [('en','Price change'),('ru','Изменение цены')]:
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = client.get(reverse('notifications:list'))
        assert label in response.content.decode()
        assert response.context['unread_notification_count'] == 1
    notice.refresh_from_db()
    assert notice.read_at is None and notice.observation.price == Decimal('999.95')


@pytest.mark.parametrize('language,label', [('en','PriceWatch administration'),('ru','PriceWatch — администрирование')])
def test_custom_admin_labels_follow_language_cookie(client, admin_user, language, label):
    client.force_login(admin_user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    assert label in client.get(reverse('admin:index')).content.decode()


@pytest.mark.parametrize('language,label', [('en','Could not sign in.'),('ru','Не удалось войти.')])
def test_login_errors_follow_selected_language(client, language, label):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.post(reverse('accounts:login'), {'username':'unknown','password':'wrong'})
    assert response.status_code == 200 and label in response.content.decode()


@pytest.mark.parametrize('language', ['en','ru'])
def test_other_users_history_stays_private_after_switch(client, django_user_model, localized_monitor, language):
    other = django_user_model.objects.create_user(username='other-i18n')
    client.force_login(other)
    select(client,language)
    for view in ('monitoring:detail','monitoring:edit','monitoring:export'):
        assert client.get(reverse(view,args=[localized_monitor.pk])).status_code == 404


@pytest.mark.parametrize('count,english,russian', [(1,'1 record','1 запись'),(2,'2 records','2 записи'),(5,'5 records','5 записей'),(21,'21 records','21 запись')])
def test_record_count_plural_forms(count, english, russian):
    for language,expected in [('en',english),('ru',russian)]:
        with translation.override(language):
            assert translation.ngettext('%(count)s record','%(count)s records',count) % {'count':count} == expected


@pytest.mark.parametrize('language', ['en','ru'])
def test_catalogs_have_no_fuzzy_or_empty_entries(language):
    catalog = (settings.BASE_DIR / 'locale' / language / 'LC_MESSAGES' / 'django.po').read_text(encoding='utf-8')
    assert not re.search(r'^#,.*\bfuzzy\b', catalog, re.M)
    assert not re.search(r'^msgstr(?:\[\d+\])? ""\n(?=\n|$)', catalog, re.M)
