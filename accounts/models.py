import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


def default_workspace_settings():
    return {
        "domain_join": False,
        "teammate_emails": True,
        "default_kind": "workflow",
        "recipient_registration": False,
        "multiple_documents": True,
        "autofill_signatures": True,
        "esign_consent": False,
        "certificate_of_completion": True,
        "prefill_from_api": False,
        "client_portal": False,
    }


def default_theme():
    return {"primary": "#1f1f1f", "accent": "#c8461b", "button_text": "#ffffff", "font": "Inter", "page_bg": "#f3f3f6",
            "header_bg": "#ffffff", "header_text": "#1f1f1f", "radius": "rounded", "field_style": "outlined",
            "show_logo": True, "show_name": True, "preset": "standard"}


THEME_PRESETS = {
    "standard": {"label": "Standard", "primary": "#1f1f1f", "accent": "#c8461b", "button_text": "#ffffff", "page_bg": "#f3f3f6",
                 "header_bg": "#ffffff", "header_text": "#1f1f1f", "font": "Inter", "radius": "rounded", "field_style": "outlined"},
    "ocean": {"label": "Ocean", "primary": "#0b5cad", "accent": "#0b5cad", "button_text": "#ffffff", "page_bg": "#eef4fb",
              "header_bg": "#0b3d73", "header_text": "#ffffff", "font": "Open Sans", "radius": "rounded", "field_style": "outlined"},
    "forest": {"label": "Forest", "primary": "#1f6f4a", "accent": "#2f9e68", "button_text": "#ffffff", "page_bg": "#f1f7f3",
               "header_bg": "#ffffff", "header_text": "#16402c", "font": "Lato", "radius": "pill", "field_style": "filled"},
    "sunset": {"label": "Sunset", "primary": "#c2410c", "accent": "#ea580c", "button_text": "#ffffff", "page_bg": "#fff7ed",
               "header_bg": "#ffffff", "header_text": "#431407", "font": "Roboto", "radius": "rounded", "field_style": "outlined"},
    "corporate": {"label": "Corporate", "primary": "#111827", "accent": "#b91c1c", "button_text": "#ffffff", "page_bg": "#f4f4f5",
                  "header_bg": "#111827", "header_text": "#ffffff", "font": "Source Serif 4", "radius": "sharp", "field_style": "underline"},
    "minimal": {"label": "Minimal", "primary": "#404040", "accent": "#a3a3a3", "button_text": "#ffffff", "page_bg": "#ffffff",
                "header_bg": "#ffffff", "header_text": "#262626", "font": "Inter", "radius": "sharp", "field_style": "filled"},
}


def default_thank_you():
    return {
        "title": "Thank you!",
        "body": "Your documents have been submitted successfully. You can close this page now.",
        "button_label": "",
        "button_url": "",
        "icon": "🎉", "show_reference": True, "next_steps": "", "contact": "", "redirect_url": "", "redirect_seconds": 0,
        "confetti": False,
    }


THANK_YOU_ICONS = ["🎉", "✅", "🙏", "📨", "👍", "⭐", "logo", "none"]


class Workspace(models.Model):
    INDUSTRIES = [(x, x) for x in [
        "Insurance", "Finance", "Healthcare", "Education", "Government", "Legal", "Real estate",
        "Retail", "Manufacturing", "Technology", "Non-profit", "Other"]]
    SIZES = [(x, x) for x in ["1-5", "6-50", "51-200", "201-1000", "1000+"]]

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=63, unique=True)
    description = models.TextField(blank=True)
    industry = models.CharField(max_length=60, choices=INDUSTRIES, blank=True)
    department = models.CharField(max_length=60, blank=True)
    size = models.CharField(max_length=20, choices=SIZES, blank=True)
    logo = models.ImageField(upload_to="logos/", blank=True)
    country = models.CharField(max_length=80, blank=True)
    address1 = models.CharField(max_length=200, blank=True)
    address2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    verified_domains = models.CharField(max_length=300, blank=True, help_text="Comma separated, e.g. example.com")
    settings = models.JSONField(default=default_workspace_settings)
    theme = models.JSONField(default=default_theme)
    thank_you = models.JSONField(default=default_thank_you)
    plan = models.CharField(max_length=40, default="Enterprise Plan")
    plan_expires = models.DateField(null=True, blank=True)
    credits_limit = models.PositiveIntegerField(default=11000)
    created_at = models.DateTimeField(auto_now_add=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_reason = models.CharField(max_length=300, blank=True)
    admin_notes = models.TextField(blank=True)
    feature_overrides = models.JSONField(default=dict, blank=True, help_text="{feature: true/false} — overrides platform defaults")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:50] or "workspace"
            slug, n = base, 1
            while Workspace.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                n += 1
                slug = f"{base}-{n}"
            self.slug = slug
        super().save(*args, **kwargs)

    def setting(self, key):
        merged = default_workspace_settings()
        merged.update(self.settings or {})
        return merged.get(key)

    def domain_list(self):
        return [d.strip().lstrip("@").lower() for d in self.verified_domains.split(",") if d.strip()]

    def feature(self, key):
        """Is a platform feature on for this workspace? (platform switch AND workspace override)"""
        from superadmin.models import get_settings

        if not get_settings().feature(key):
            return False
        return bool((self.feature_overrides or {}).get(key, True))


class Membership(models.Model):
    OWNER, SUPERVISOR, AUDITOR, CREATOR, FILLER, GUEST = "owner", "supervisor", "auditor", "creator", "filler", "guest"
    ROLES = [
        (OWNER, "Workspace Owner"),
        (SUPERVISOR, "Supervisor"),
        (AUDITOR, "Auditor"),
        (CREATOR, "Creator"),
        (FILLER, "Filler"),
        (GUEST, "Guest"),
    ]
    ROLE_HELP = {
        OWNER: "Full control of the workspace, billing and teammates.",
        SUPERVISOR: "Manages teammates, workflows and every document.",
        AUDITOR: "Can view every workflow and document, read-only.",
        CREATOR: "Creates workflows and templates and sends documents.",
        FILLER: "Fills out documents and runs shared workflows.",
        GUEST: "Only completes documents sent to them.",
    }
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLES, default=CREATOR)
    active = models.BooleanField(default=True)
    position = models.CharField(max_length=120, blank=True)
    fax = models.CharField(max_length=40, blank=True)
    video_link = models.URLField(blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "workspace")

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.role})"

    # capability helpers
    @property
    def can_manage(self):
        return self.role in (self.OWNER, self.SUPERVISOR)

    @property
    def can_create(self):
        return self.role in (self.OWNER, self.SUPERVISOR, self.CREATOR)

    @property
    def can_view_all(self):
        return self.role in (self.OWNER, self.SUPERVISOR, self.AUDITOR)

    @property
    def can_run(self):
        return self.role in (self.OWNER, self.SUPERVISOR, self.CREATOR, self.FILLER)


class Group(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="groups")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    members = models.ManyToManyField(Membership, blank=True, related_name="groups")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Invitation(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=Membership.ROLES, default=Membership.CREATOR)
    token = models.CharField(max_length=48, unique=True, default=secrets.token_urlsafe)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    phone = models.CharField(max_length=40, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    signature = models.TextField(blank=True, help_text="data: URL of the default signature")
    initials = models.TextField(blank=True, help_text="data: URL of the default initials")
    language = models.CharField(max_length=10, default="en")
    current_workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.SET_NULL)
    notification_prefs = models.JSONField(default=dict, blank=True)
    # two-factor sign-in (TOTP authenticator app); recovery codes are stored as SHA-256 hashes
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled_at = models.DateTimeField(null=True, blank=True)
    totp_last_step = models.BigIntegerField(default=0)
    recovery_codes = models.JSONField(default=list, blank=True)
    # out of office: new steps for this person go to the delegate until the end date
    ooo_from = models.DateField(null=True, blank=True)
    ooo_until = models.DateField(null=True, blank=True)
    ooo_delegate = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="delegating_profiles")
    ooo_message = models.CharField(max_length=240, blank=True)

    def out_of_office(self, on=None):
        """True when out-of-office is active on the given date (default today)."""
        on = on or timezone.localdate()
        return bool(self.ooo_until and on <= self.ooo_until and (not self.ooo_from or on >= self.ooo_from))

    @property
    def two_factor_on(self):
        return bool(self.totp_enabled_at and self.totp_secret)

    NOTIFICATION_KEYS = [
        ("assigned", "A document is waiting for me"),
        ("completed", "Documents I sent are completed"),
        ("declined", "A recipient declines a document"),
        ("autostart", "A workflow is started automatically"),
        ("access_request", "Someone requests access to a workflow I edit"),
        ("team", "Teammate changes in my workspace"),
    ]

    def wants(self, key):
        return (self.notification_prefs or {}).get(key, True)

    def __str__(self):
        return f"Profile of {self.user}"

    @property
    def display_initials(self):
        name = self.user.get_full_name() or self.user.username
        parts = [p for p in name.replace(".", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"


class Contact(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=40, blank=True)
    company = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} <{self.email}>"


class Connection(models.Model):
    """A connected account (integration credentials) usable by bots."""

    KINDS = [
        ("webhook", "Generic webhook / HTTP"),
        ("smtp", "SMTP mailbox"),
        ("google", "Google Workspace"),
        ("microsoft", "Microsoft 365"),
        ("salesforce", "Salesforce"),
        ("dropbox", "Dropbox"),
        ("box", "Box"),
        ("slack", "Slack"),
        ("other", "Other"),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connections")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="connections")
    kind = models.CharField(max_length=20, choices=KINDS)
    label = models.CharField(max_length=120)
    endpoint = models.URLField(blank=True)
    secret = models.CharField(max_length=255, blank=True)
    data = models.JSONField(default=dict, blank=True)  # OAuth tokens for Google Drive / Microsoft 365 (never shown)
    created_at = models.DateTimeField(auto_now_add=True)


class EmailTemplate(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="email_templates")
    key = models.CharField(max_length=60)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    enabled = models.BooleanField(default=True)  # off = this kind of email is not sent
    cc = models.CharField(max_length=500, blank=True)
    bcc = models.CharField(max_length=500, blank=True)
    button_label = models.CharField(max_length=60, blank=True)  # the {link} line becomes a button with this label
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("workspace", "key")


class UsageEvent(models.Model):
    SEND, BOT = "send", "bot"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="usage")
    workflow = models.ForeignKey("flows.Workflow", null=True, blank=True, on_delete=models.SET_NULL)
    workflow_name = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=10, choices=[(SEND, "Send"), (BOT, "Bot action")])
    credits = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


class ApiToken(models.Model):
    """A personal API token: acts as its user inside one workspace (REST API and the HTTP MCP endpoint)."""
    FULL, READ = "full", "read"
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=80)
    scope = models.CharField(max_length=10, choices=[(FULL, "Full access"), (READ, "Read only")], default=FULL)
    prefix = models.CharField(max_length=12, unique=True)
    token_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @staticmethod
    def digest(raw):
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, workspace, name, scope=FULL, days=None):
        """Create a token; returns (token, raw secret). The raw value is shown once and never stored."""
        import datetime

        prefix = secrets.token_hex(4)
        raw = f"sd_{prefix}_{secrets.token_urlsafe(30)}"
        expires = timezone.now() + datetime.timedelta(days=int(days)) if days else None
        return cls.objects.create(user=user, workspace=workspace, name=name[:80] or "API token", scope=scope,
                                  prefix=prefix, token_hash=cls.digest(raw), expires_at=expires), raw

    @classmethod
    def authenticate(cls, raw):
        import hmac

        parts = (raw or "").split("_")
        if len(parts) < 3 or parts[0] != "sd":
            return None
        tok = cls.objects.select_related("user", "workspace").filter(prefix=parts[1], revoked_at__isnull=True).first()
        if tok is None or not hmac.compare_digest(tok.token_hash, cls.digest(raw)):
            return None
        if tok.expires_at and tok.expires_at <= timezone.now() or not tok.user.is_active:
            return None
        if not Membership.objects.filter(user=tok.user, workspace=tok.workspace, active=True).exists():
            return None
        return tok

    @property
    def is_active(self):
        return not self.revoked_at and not (self.expires_at and self.expires_at <= timezone.now())


class SSOProvider(models.Model):
    """Single sign-on through OpenID Connect (Microsoft Entra ID, Google Workspace, Okta, Auth0, Keycloak…)."""

    name = models.CharField(max_length=80, help_text="Shown on the button: “Sign in with …”")
    kind = models.CharField(max_length=20, default="generic", choices=[("microsoft", "Microsoft"), ("google", "Google"),
                                                                        ("okta", "Okta"), ("generic", "Other OpenID Connect")])
    issuer = models.URLField(help_text="e.g. https://login.microsoftonline.com/<tenant-id>/v2.0 or https://accounts.google.com")
    client_id = models.CharField(max_length=255)
    client_secret = models.CharField(max_length=500, blank=True)
    scopes = models.CharField(max_length=200, default="openid email profile")
    allowed_domains = models.CharField(max_length=500, blank=True, help_text="Comma separated, e.g. example.com — required: only these email domains can sign in with this provider")
    create_users = models.BooleanField(default=False, help_text="Create an account the first time someone from an allowed domain signs in")
    workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                  help_text="Add new people to this workspace")
    default_role = models.CharField(max_length=20, default="filler")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def domains(self):
        return [d.strip().lower().lstrip("@") for d in self.allowed_domains.split(",") if d.strip()]
