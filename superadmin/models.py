"""Platform-wide settings and the platform audit log (super admins only)."""
from django.conf import settings
from django.core.cache import cache
from django.db import models

FEATURES = [
    ("ai", "AI features", "AI bots, Ask AI on documents and Describe it."),
    ("connectors", "Custom connectors", "Workspaces can call their own HTTP/JSON APIs from bots and form lookups."),
    ("webhooks", "Outgoing webhooks", "Workspaces can send signed events to other systems."),
    ("api", "REST API & MCP", "API tokens, /api/v1/ and the remote MCP endpoint."),
    ("public_forms", "Public start forms", "Published start forms and public links can be filled by anyone."),
    ("create_workspaces", "Users can create workspaces", "New users may create their own workspace during onboarding."),
]
FEATURE_KEYS = [k for k, _, _ in FEATURES]

CACHE_KEY = "platform-settings:v1"


def default_features():
    return {k: True for k in FEATURE_KEYS}


class PlatformSettings(models.Model):
    SIGNUP_OPEN, SIGNUP_DOMAINS, SIGNUP_INVITE, SIGNUP_CLOSED = "open", "domains", "invite", "closed"
    SIGNUP_MODES = [(SIGNUP_OPEN, "Anyone can sign up"), (SIGNUP_DOMAINS, "Only emails from allowed domains"),
                    (SIGNUP_INVITE, "Only people with an invitation"), (SIGNUP_CLOSED, "Closed — super admins add users")]
    TWOFA_OFF, TWOFA_ADMINS, TWOFA_ALL = "off", "admins", "all"
    TWOFA = [(TWOFA_OFF, "Optional"), (TWOFA_ADMINS, "Required for owners, supervisors and super admins"),
             (TWOFA_ALL, "Required for everyone")]
    LEVELS = [("info", "Information"), ("warning", "Warning"), ("success", "Good news")]

    site_name = models.CharField(max_length=80, default="Paperlane")
    support_email = models.EmailField(blank=True)
    # sign-up & workspaces
    signup_mode = models.CharField(max_length=10, choices=SIGNUP_MODES, default=SIGNUP_OPEN)
    signup_domains = models.CharField(max_length=500, blank=True, help_text="Comma separated, e.g. company.com")
    default_plan = models.CharField(max_length=40, default="Enterprise Plan")
    default_plan_days = models.PositiveIntegerField(default=365)
    default_credits = models.PositiveIntegerField(default=11000)
    max_workspaces_per_user = models.PositiveIntegerField(default=5)
    # security
    password_min_length = models.PositiveSmallIntegerField(default=8)
    require_2fa = models.CharField(max_length=10, choices=TWOFA, default=TWOFA_OFF)
    session_idle_minutes = models.PositiveIntegerField(default=0, help_text="0 = never sign out idle users")
    max_upload_mb = models.PositiveIntegerField(default=50)
    blocked_extensions = models.CharField(max_length=300, default="exe,bat,cmd,com,msi,scr,js,vbs,ps1,jar,sh,dll")
    block_private_networks = models.BooleanField(default=False, help_text="Stop connectors and webhooks from calling "
                                                                          "localhost and private network addresses.")
    # operations
    maintenance_mode = models.BooleanField(default=False)
    maintenance_message = models.CharField(max_length=300, blank=True,
                                           default="Paperlane is being updated. Please try again in a few minutes.")
    announcement = models.CharField(max_length=300, blank=True)
    announcement_level = models.CharField(max_length=10, choices=LEVELS, default="info")
    announcement_until = models.DateTimeField(null=True, blank=True)
    features = models.JSONField(default=default_features, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = verbose_name_plural = "Platform settings"

    def __str__(self):
        return "Platform settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(CACHE_KEY)

    @property
    def public_signup(self):
        return self.signup_mode in (self.SIGNUP_OPEN, self.SIGNUP_DOMAINS)

    def feature(self, key):
        return bool({**default_features(), **(self.features or {})}.get(key, True))

    def domain_list(self):
        return [d.strip().lstrip("@").lower() for d in self.signup_domains.split(",") if d.strip()]

    def blocked_ext_list(self):
        return {e.strip().lstrip(".").lower() for e in self.blocked_extensions.split(",") if e.strip()}


def upload_problem(upload, field_max_mb=None):
    """Why an uploaded file is refused (platform size cap and blocked file types), or ""."""
    ps = get_settings()
    cap = float(ps.max_upload_mb or 50)
    limit = min(float(field_max_mb or cap), cap)
    ext = (upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else "")
    if ext and ext in ps.blocked_ext_list():
        return f"“{upload.name}”: .{ext} files are not allowed."
    if upload.size > limit * 1024 * 1024:
        return f"“{upload.name}” is larger than {limit:g} MB."
    return ""


def get_settings():
    """The platform settings (cached; one row with pk=1)."""
    obj = cache.get(CACHE_KEY)
    if obj is None:
        obj = PlatformSettings.objects.filter(pk=1).first()
        if obj is None:
            obj = PlatformSettings()
            obj.save()
        cache.set(CACHE_KEY, obj, 60)
    return obj


class PlatformEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_label = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=300, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.target}"


def log(request, action, target="", **details):
    user = getattr(request, "user", None)
    ip = (request.META.get("REMOTE_ADDR") or None) if request else None
    return PlatformEvent.objects.create(actor=user if user and user.is_authenticated else None,
                                        actor_label=getattr(user, "email", "") or "system", action=action,
                                        target=str(target)[:300], details=details, ip=ip)


class SignInEvent(models.Model):
    """Every sign-in attempt (success, failure, sign-out) — Super admin › Sign-in log and the user's page."""
    OK, FAILED, LOGOUT = "ok", "failed", "logout"
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    email = models.CharField(max_length=254, blank=True)
    result = models.CharField(max_length=10, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def device(self):
        ua = self.user_agent
        browser = next((b for b in ("Edg", "Chrome", "Firefox", "Safari") if b in ua), "")
        os_ = next((o for o in ("iPhone", "iPad", "Android", "Macintosh", "Windows", "Linux") if o in ua), "")
        return " on ".join(x for x in ({"Edg": "Edge"}.get(browser, browser), os_.replace("Macintosh", "Mac")) if x) or "Unknown device"


class Broadcast(models.Model):
    """A message from super admins to many users (in-app notification and optional email)."""
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=300, blank=True)
    audience = models.CharField(max_length=200)
    recipients = models.PositiveIntegerField(default=0)
    emailed = models.BooleanField(default=False)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
