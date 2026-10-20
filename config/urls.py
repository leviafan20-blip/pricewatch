from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("monitors/", include("apps.monitoring.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("", include("apps.core.urls")),
]

admin.site.site_header = "PriceWatch — администрирование"
admin.site.site_title = "PriceWatch"
admin.site.index_title = "Управление доступом"
