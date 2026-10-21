"""Keep Django's locale handling, with English before an explicit selection."""

from django.conf import settings
from django.middleware.locale import LocaleMiddleware as DjangoLocaleMiddleware
from django.utils import translation


class LocaleMiddleware(DjangoLocaleMiddleware):
    def process_request(self, request):
        super().process_request(request)
        # The portfolio defaults to English even in a Russian-language browser.
        # Explicit selections still use Django's validated language cookie.
        if request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) not in dict(settings.LANGUAGES):
            translation.activate(settings.LANGUAGE_CODE)
            request.LANGUAGE_CODE = translation.get_language()
