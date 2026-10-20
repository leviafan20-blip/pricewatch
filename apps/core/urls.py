from django.urls import path

from . import views

app_name = "core"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/live/", views.liveness, name="liveness"),
    path("health/ready/", views.readiness, name="readiness"),
]
