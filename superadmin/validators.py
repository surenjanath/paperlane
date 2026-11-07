from django.core.exceptions import ValidationError


class PlatformMinimumLengthValidator:
    """Minimum password length set by super admins (Platform settings › Security)."""

    def _min(self):
        from .models import get_settings

        try:
            return max(6, int(get_settings().password_min_length or 8))
        except Exception:  # database not ready (migrations)
            return 8

    def validate(self, password, user=None):
        n = self._min()
        if len(password or "") < n:
            raise ValidationError(f"This password is too short. It must contain at least {n} characters.",
                                  code="password_too_short")

    def get_help_text(self):
        return f"Your password must contain at least {self._min()} characters."
