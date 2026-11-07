"""Paperlane — document workflow automation platform (Django settings)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("PAPERLANE_SECRET_KEY", "dev-insecure-paperlane-change-me")
DEBUG = os.environ.get("PAPERLANE_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("PAPERLANE_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],.localhost,.test").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "accounts",
    "flows",
    "core",
    "superadmin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "accounts.middleware.WorkspaceMiddleware",
    "superadmin.middleware.PlatformMiddleware",
]

ROOT_URLCONF = "paperlane.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.shell",
            ],
        },
    },
]

WSGI_APPLICATION = "paperlane.wsgi.application"

def _database(url):
    """PAPERLANE_DATABASE_URL: postgres://user:password@host:5432/name (or sqlite:///path). Default: db.sqlite3."""
    from urllib.parse import unquote, urlparse

    # SQLite is shared by requests, the scheduler, webhook and simulation threads. WAL lets readers carry on while one
    # writer works; IMMEDIATE takes the write lock up front so writers queue on the timeout instead of failing at once
    # with "database is locked" (a deferred transaction that upgrades from read to write can't wait).
    sqlite_opts = {"timeout": 20, "transaction_mode": "IMMEDIATE",
                   "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=20000;"}
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3", "OPTIONS": sqlite_opts}
    u = urlparse(url)
    if u.scheme == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": u.path or BASE_DIR / "db.sqlite3", "OPTIONS": sqlite_opts}
    if u.scheme in ("postgres", "postgresql"):
        return {"ENGINE": "django.db.backends.postgresql", "NAME": u.path.lstrip("/"), "USER": unquote(u.username or ""),
                "PASSWORD": unquote(u.password or ""), "HOST": u.hostname or "", "PORT": str(u.port or 5432),
                "CONN_MAX_AGE": int(os.environ.get("PAPERLANE_DB_CONN_MAX_AGE", "60")), "CONN_HEALTH_CHECKS": True}
    raise ValueError(f"Unsupported PAPERLANE_DATABASE_URL scheme: {u.scheme}")


DATABASES = {"default": _database(os.environ.get("PAPERLANE_DATABASE_URL", ""))}

# Shared cache: login throttling, the verify rate limit and the scheduler heartbeat must be shared by every web worker
# and the jobs worker, so production uses the database (run `manage.py createcachetable` once). Dev: in memory.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "paperlane_cache"}
          if os.environ.get("PAPERLANE_CACHE", "memory" if DEBUG else "db") == "db"
          else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

if not DEBUG and SECRET_KEY == "dev-insecure-paperlane-change-me":
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Set PAPERLANE_SECRET_KEY to a long random value before running with PAPERLANE_DEBUG=0.")

# Error reporting: set PAPERLANE_SENTRY_DSN (Sentry or a compatible service such as GlitchTip). No personal data is sent.
if os.environ.get("PAPERLANE_SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(dsn=os.environ["PAPERLANE_SENTRY_DSN"], send_default_pii=False,
                    environment=os.environ.get("PAPERLANE_ENVIRONMENT", "production" if not DEBUG else "development"),
                    traces_sample_rate=float(os.environ.get("PAPERLANE_SENTRY_TRACES", "0")))

LOGGING = {  # everything to stdout/stderr, so Docker / systemd / your log shipper collects it
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("PAPERLANE_LOG_LEVEL", "INFO")},
    "loggers": {"django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False}},
}
BACKUP_DIR = Path(os.environ.get("PAPERLANE_BACKUP_DIR", BASE_DIR / "backups"))
BACKUP_KEEP = int(os.environ.get("PAPERLANE_BACKUP_KEEP", "14"))

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "superadmin.validators.PlatformMinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en"
# Recipient pages (fill, sign, thank-you) can be shown in these languages; the team app stays in English.
LANGUAGES = [("en", "English"), ("es", "Español")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = os.environ.get("PAPERLANE_TIME_ZONE", "America/Port_of_Spain")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Uploaded files on S3-compatible storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO, DigitalOcean Spaces …) when a
# bucket is set; otherwise the local media folder. Files stay private: links are signed and expire (default 1 hour).
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
if os.environ.get("PAPERLANE_S3_BUCKET"):
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {
        "bucket_name": os.environ["PAPERLANE_S3_BUCKET"],
        "endpoint_url": os.environ.get("PAPERLANE_S3_ENDPOINT") or None,  # leave empty for AWS
        "region_name": os.environ.get("PAPERLANE_S3_REGION") or None,
        "access_key": os.environ.get("PAPERLANE_S3_ACCESS_KEY") or None,
        "secret_key": os.environ.get("PAPERLANE_S3_SECRET_KEY") or None,
        "location": os.environ.get("PAPERLANE_S3_PREFIX", ""),
        "default_acl": None, "querystring_auth": True, "file_overwrite": False,
        "querystring_expire": int(os.environ.get("PAPERLANE_S3_LINK_SECONDS", "3600")),
    }}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

EMAIL_BACKEND = os.environ.get("PAPERLANE_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("PAPERLANE_FROM_EMAIL", "Paperlane <no-reply@paperlane.local>")
SITE_URL = os.environ.get("PAPERLANE_SITE_URL", "http://127.0.0.1:8000")

# Storage folder used by the "Save to storage" bots (local stand-in for cloud drives).
BOT_STORAGE_ROOT = MEDIA_ROOT / "bot_storage"

MESSAGE_TAGS = {40: "error"}
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

# In-process scheduler (reminders, deadlines, autostarts, webhook retries) for single-server setups.
# Turn off with PAPERLANE_SCHEDULER=0 when running `manage.py paperlane_tick --loop 30` separately.
PAPERLANE_SCHEDULER = os.environ.get("PAPERLANE_SCHEDULER", "1") == "1"
PAPERLANE_SCHEDULER_SECONDS = int(os.environ.get("PAPERLANE_SCHEDULER_SECONDS", "30"))

# ---- SMTP and HTTPS hardening from the environment (see docs/DEPLOYMENT.md) ----
EMAIL_HOST = os.environ.get("PAPERLANE_EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("PAPERLANE_EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.environ.get("PAPERLANE_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("PAPERLANE_EMAIL_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("PAPERLANE_EMAIL_TLS", "0") == "1"
# Start workflows from email: addresses are <token>@PAPERLANE_INBOUND_DOMAIN; providers POST to /inbound/email/<secret>/
PAPERLANE_INBOUND_DOMAIN = os.environ.get("PAPERLANE_INBOUND_DOMAIN", "")
PAPERLANE_INBOUND_SECRET = os.environ.get("PAPERLANE_INBOUND_SECRET", "")
if os.environ.get("PAPERLANE_HTTPS") == "1":  # behind TLS: secure cookies, HSTS and HTTPS redirect
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]  # container health checks call plain HTTP inside the network
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("PAPERLANE_HSTS_SECONDS", "31536000"))
    CSRF_TRUSTED_ORIGINS = [f"https://{h.lstrip('.')}" for h in ALLOWED_HOSTS if h and h not in ("localhost", "127.0.0.1", "[::1]")]
