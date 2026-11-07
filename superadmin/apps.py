from django.apps import AppConfig


class SuperadminConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "superadmin"
    verbose_name = "Super admin"

    def ready(self):
        from . import signals  # noqa: F401 — sign-in log
