
from django.utils.translation import gettext_lazy as _
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("i18n/", include("django.conf.urls.i18n")),
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("monitors/", include("apps.monitoring.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("", include("apps.core.urls")),
]

admin.site.site_header = _("PriceWatch administration")
admin.site.site_title = "PriceWatch"
admin.site.index_title = _("Access management")
