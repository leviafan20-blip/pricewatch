from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Project-owned user model, kept compatible with Django authentication."""

    email_notifications_enabled = models.BooleanField(default=False, verbose_name="Email-уведомления")
