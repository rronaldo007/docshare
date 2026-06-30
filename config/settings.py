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
                "files.context_processors.user_flags",
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

# Object storage (S3 / Cloudflare R2 via Sevalla) is used for uploaded files
# when DJANGO_S3_BUCKET is set; otherwise files stay on the local disk. This
# keeps local dev and the test suite zero-config (no boto3, no network) while a
# production deploy points at a private, S3-compatible bucket by setting a few
# env vars. Buckets MUST stay private: file bytes are still streamed through the
# guarded views (files.views._serve_file), never via a public object URL, so
# every share-link guard keeps applying. See README.
_S3_BUCKET = os.environ.get("DJANGO_S3_BUCKET", "").strip()
if _S3_BUCKET:
    # botocore >= 1.36 defaults to adding data-integrity checksums (CRC32) to
    # uploads and validating them on responses. Cloudflare R2 (and several other
    # S3-compatible stores) do not handle these the way the SDK now expects,
    # which breaks CompleteMultipartUpload (HTTP 500 at the finalize step) and
    # can affect plain PUTs too. Restore the pre-1.36 behaviour -- checksums only
    # when the API actually requires them. setdefault so a real AWS deployment
    # can still opt back in via the env. Must run before any boto3 client is
    # built; settings import happens well before that.
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
    # Belt-and-suspenders: also pin the checksum behaviour directly on the boto3
    # client config, so it applies regardless of when the env vars are read. We
    # pass a full client_config, which means django-storages stops deriving the
    # client config from the individual signature_version/addressing_style
    # options below -- so those MUST be repeated here (s3v4 + path-style are
    # required for R2). request/response_checksum_calculation="when_required"
    # restores pre-botocore-1.36 behaviour that R2's multipart finalize needs.
    from botocore.config import Config as _BotoConfig

    _r2_client_config = _BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    _default_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": _S3_BUCKET,
            "access_key": os.environ.get("DJANGO_S3_ACCESS_KEY_ID", ""),
            "secret_key": os.environ.get("DJANGO_S3_SECRET_ACCESS_KEY", ""),
            # Sevalla/R2 give a custom endpoint; region "auto" is fine for R2.
            "endpoint_url": os.environ.get("DJANGO_S3_ENDPOINT_URL", ""),
            "region_name": os.environ.get("DJANGO_S3_REGION", "auto"),
            "signature_version": "s3v4",
            # Path-style addressing is the safe choice for custom S3-compatible
            # endpoints (endpoint/bucket/key). R2 does not support ACLs, so do
            # not send one; the bucket stays private by its own config.
            "addressing_style": "path",
            # Authoritative client config (carries the checksum fix + the s3v4 /
            # path-style settings, since providing this overrides the individual
            # signature_version/addressing_style options above).
            "client_config": _r2_client_config,
            "default_acl": None,
            # UUID-namespaced keys never collide, but never silently overwrite.
            "file_overwrite": False,
            # Signed URLs aren't used for delivery (we stream through Django),
            # but keep auth on so any accidental .url() stays non-public.
            "querystring_auth": True,
        },
    }
else:
    _default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}

STORAGES = {
    "default": _default_storage,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
# Local disk path. Still used for the chunked-upload staging dir (.chunks/) even
# when files land in object storage, so it must remain a writable local path.
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media")

# Presigned direct-to-bucket uploads: the browser PUTs file bytes straight to
# the object-storage bucket (bypassing this app AND the proxy's ~100 MB body
# limit), then asks the app to record the file. This ONLY works with object
# storage configured AND requires a CORS rule on the bucket allowing the app's
# origin to PUT -- so it is OFF by default even when S3 is on. Turn it on with
# DJANGO_DIRECT_UPLOAD=1 only after the bucket's CORS is configured and tested.
# When off, uploads use the existing chunked/batched path unchanged. See README.
DIRECT_UPLOAD_ENABLED = bool(_S3_BUCKET) and _env_bool("DJANGO_DIRECT_UPLOAD", False)
# How long a presigned PUT URL stays valid (seconds). Short by design: the URL
# is a bearer capability to write one object key for its lifetime.
DIRECT_UPLOAD_EXPIRY = int(os.environ.get("DJANGO_DIRECT_UPLOAD_EXPIRY", 3600))
# A single presigned PUT tops out at S3's 5 GB object limit; this is the size at
# which the client switches from a one-shot PUT to a multipart upload (below).
DIRECT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024 * 1024
# Multipart direct upload: files over the single-PUT ceiling are uploaded to the
# bucket in parts, each via its own short-lived presigned PUT, lifting the limit
# to S3's 5 TB / 10,000-part maximum. S3/R2 require every part except the last to
# be at least 5 MB, so the part size must stay well above that; 256 MB covers
# files up to ~2.5 TB before hitting the 10,000-part cap. Env-tunable.
DIRECT_UPLOAD_PART_BYTES = int(
    os.environ.get("DJANGO_DIRECT_UPLOAD_PART_BYTES", 256 * 1024 * 1024)
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth flow
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "browse"
LOGOUT_REDIRECT_URL = "home"

# Email (console backend prints to logs in dev; configure SMTP for real delivery).
# To actually send mail (e.g. share-link emails), set DJANGO_EMAIL_BACKEND to
# "django.core.mail.backends.smtp.EmailBackend" and the DJANGO_EMAIL_HOST/PORT/
# USER/PASSWORD/USE_TLS vars below.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("DJANGO_EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("DJANGO_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _env_bool("DJANGO_EMAIL_USE_TLS", True)
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

# Optional cap on a single owner upload assembled via the chunked uploader.
# 0 (the default) means unlimited. Set this when the persistent disk is finite
# (e.g. on Sevalla) so one huge file can't fill it; the chunk endpoint rejects
# the upload once the running total would exceed the cap. See files.views.
MAX_UPLOAD_BYTES = int(os.environ.get("DJANGO_MAX_UPLOAD_BYTES", 0))

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
# memory; there is no cap on an individual file's size. Kept small (Django's
# default) so a multi-file upload doesn't hold many files in RAM at once -- the
# browser also batches large uploads, but this keeps per-request memory low on
# small instances.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024  # 2 MB

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
