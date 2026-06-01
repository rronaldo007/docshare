import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


# Local dev defaults are preserved; override via environment for deployment.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me-in-production")
DEBUG = _env_bool("DJANGO_DEBUG", True)

# In DEBUG, default to permissive local hosts. With DEBUG off you MUST set
# DJANGO_ALLOWED_HOSTS explicitly -- we never fall back to "*" in production,
# which would allow Host-header poisoning of the absolute share URLs built via
# request.build_absolute_uri.
_allowed_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
if _allowed_hosts:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts.split(",") if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
else:
    ALLOWED_HOSTS = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "files",
    "pages",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth flow
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "browse"
LOGOUT_REDIRECT_URL = "home"

# Email (console backend prints to logs in dev; configure SMTP for real delivery)
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_DEFAULT_FROM_EMAIL", "noreply@docshare.local")
CONTACT_EMAIL = os.environ.get("DJANGO_CONTACT_EMAIL", "hello@docshare.local")

# Allow PDFs to render in same-origin iframes for previews
X_FRAME_OPTIONS = "SAMEORIGIN"

# Never let the browser MIME-sniff a response into something more dangerous
# than the Content-Type we send (e.g. sniffing an octet-stream into HTML).
# Combined with the inline-serving allowlist in files.views, this blocks
# stored-XSS via uploaded files.
SECURE_CONTENT_TYPE_NOSNIFF = True

# Only trust X-Forwarded-For for the client IP when running behind a known,
# trusted reverse proxy that overwrites it (set this to "1" in that case).
# By default we use REMOTE_ADDR so a client cannot spoof XFF to defeat the
# anonymous-upload rate limit. See files.views._client_ip.
TRUST_X_FORWARDED_FOR = _env_bool("DJANGO_TRUST_X_FORWARDED_FOR", False)

# Per-file upload size cap (anonymous uploads only, by default). Owner uploads
# stay uncapped so large/high-quality files keep working byte-for-byte.
ANON_MAX_UPLOAD_BYTES = int(
    os.environ.get("DJANGO_ANON_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)  # 50 MB
)

# HTTPS-only hardening (secure cookies, HSTS, HTTP->HTTPS redirect). This only
# makes sense when the app is actually served over TLS; enabling it on a plain
# HTTP deployment makes the site unreachable (every request 301s to https and
# the secure cookies never set). It is therefore gated on BOTH not-DEBUG and an
# explicit DJANGO_HTTPS flag (default on), so a production deploy behind TLS is
# hardened by default, while a DEBUG=False container served over plain HTTP
# (e.g. the local docker-compose image) can opt out with DJANGO_HTTPS=False.
if not DEBUG and _env_bool("DJANGO_HTTPS", True):
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Honor the proxy's forwarded protocol so SECURE_SSL_REDIRECT doesn't loop.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Uploads are stored byte-for-byte, so files keep their original quality at any
# resolution -- nothing is ever recompressed or downscaled. Files larger than
# this threshold stream to a temp file on disk instead of being buffered in
# memory; there is no cap on an individual file's size.
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB

# This limit applies ONLY to non-file form data (e.g. the folder-upload "paths"
# list), never to the uploaded files, so large/high-quality files are unaffected.
DATA_UPLOAD_MAX_MEMORY_SIZE = 100 * 1024 * 1024  # 100 MB of form fields

# A folder upload posts two fields per file (the file plus its relative path),
# so a big folder tree can far exceed Django's defaults of 1000 fields / 100
# files per request. Use high-but-FINITE caps: generous enough for very large
# folder uploads, but not "unlimited" -- an unbounded field/file count is a
# memory/CPU DoS vector when parsing the multipart body. Override via env if a
# legitimate upload is even larger.
DATA_UPLOAD_MAX_NUMBER_FILES = int(
    os.environ.get("DJANGO_DATA_UPLOAD_MAX_NUMBER_FILES", 20000)
)
# paths + files + a few form fields per request; keep headroom above the file
# cap (roughly 2 fields per file plus overhead).
DATA_UPLOAD_MAX_NUMBER_FIELDS = int(
    os.environ.get("DJANGO_DATA_UPLOAD_MAX_NUMBER_FIELDS", 50000)
)
