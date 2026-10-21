import logging

from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.monitoring.models import Monitor

logger = logging.getLogger(__name__)


@login_required
@never_cache
@require_GET
def dashboard(request):
    return render(request, "core/dashboard.html", {
        "has_monitors": Monitor.objects.filter(owner=request.user).exists(),
    })


@never_cache
@require_GET
def liveness(request):
    return JsonResponse({"status": "ok"})


@never_cache
@require_GET
def readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        # Deliberately omit connection details and secrets from the HTTP response/log.
        logger.warning("Database readiness check failed")
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
