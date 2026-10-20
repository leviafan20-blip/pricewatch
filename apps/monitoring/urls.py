from django.urls import path

from . import views

app_name = "monitoring"
urlpatterns = [
    path("", views.monitor_list, name="list"),
    path("new/", views.monitor_create, name="create"),
    path("compare/", views.monitor_compare, name="compare"),
    path("<int:pk>/", views.monitor_detail, name="detail"),
    path("<int:pk>/edit/", views.monitor_edit, name="edit"),
    path("<int:pk>/archive/", views.monitor_archive, name="archive"),
    path("<int:pk>/check/", views.monitor_check, name="check"),
    path("<int:pk>/schedule/", views.monitor_schedule, name="schedule"),
]
