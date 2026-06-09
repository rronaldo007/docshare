# DocShare

A small Django site for sharing documents and folders. Users get private,
nested folders and can hand out public share links (no account needed to view).

## Features

- User accounts (sign up, log in, log out)
- Nested folders + file uploads, scoped per user
- Inline previews: images, PDFs, and text files render in-browser
- Public share links for any file or folder, with optional expiry and an
  optional password (set when you create the link)
- Manage and revoke your share links from one page

## Stack

Django + Tailwind (via the Play CDN, so there is no build step). SQLite for
storage. Uploaded files live under `media/`.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser  # optional, for /admin
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign up.

## Run it with Docker

The image runs Django under gunicorn with WhiteNoise serving static assets,
and reads its configuration from environment variables (see
`docker-compose.yml`). SQLite and uploaded media are kept in named volumes so
they survive container rebuilds.

```bash
docker compose up --build
```

Open http://127.0.0.1:8000/. Migrations run automatically on container start.

Helper scripts in `scripts/` wrap the common actions:

```bash
scripts/start.sh            # build (if needed) and start in the background
scripts/refresh.sh          # rebuild from current source and restart, keeping data
scripts/stop.sh             # stop and remove the container (data volumes kept)
scripts/stop.sh --volumes   # also wipe the database and uploads
```

Before exposing the app, edit the environment in `docker-compose.yml`:

- `DJANGO_SECRET_KEY` - set a long random value
- `DJANGO_DEBUG` - keep `False`
- `DJANGO_ALLOWED_HOSTS` - comma-separated hostnames you serve from. **Required
  when `DEBUG=False`:** there is no `*` fallback, so if you leave it empty
  Django will reject every request (fail-closed, to prevent Host-header
  poisoning of the absolute share URLs).

Other supported variables:

- `DJANGO_DB_PATH` - SQLite file location
- `DJANGO_MEDIA_ROOT` - uploads directory (see the media note below)
- `DJANGO_TRUST_X_FORWARDED_FOR` - set to `1` **only** when the app runs behind
  a reverse proxy you control that overwrites `X-Forwarded-For`. Off by default
  so a client cannot spoof the header to defeat the anonymous-upload limit.
- `DJANGO_ANON_MAX_UPLOAD_BYTES` - per-file cap for no-account uploads
  (default 50 MB). Logged-in uploads stay uncapped.
- `DJANGO_MAX_UPLOAD_BYTES` - optional cap on a single logged-in upload
  assembled via the chunked uploader. `0` (default) means unlimited; set it
  when the persistent disk is finite so one huge file can't fill it.
- `DJANGO_HTTPS` - HTTPS-only hardening (secure cookies, HSTS, HTTP→HTTPS
  redirect). Defaults **on** when `DEBUG=False`. Set it to `False` for a
  `DEBUG=False` container served over plain HTTP (the bundled `docker-compose`
  does this) — otherwise every request 301s to a non-existent `https://` URL.
- `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_HSTS_SECONDS` - fine-tune the
  HTTPS hardening above (only relevant when `DJANGO_HTTPS` is on).
- `DJANGO_DATA_UPLOAD_MAX_NUMBER_FILES` / `DJANGO_DATA_UPLOAD_MAX_NUMBER_FIELDS`
  - raise these only if a single folder upload exceeds the defaults
  (20000 files / 50000 fields).
- `GUNICORN_WORKER_CLASS` / `GUNICORN_THREADS` / `GUNICORN_TIMEOUT` - gunicorn
  serving knobs (defaults `gthread` / `4` / `120`). A folder "Download all" zip
  and large single files are streamed and can take a long time to send;
  **threaded (`gthread`) workers are required** so a long download is not
  mistaken for a hung worker and SIGKILLed at `GUNICORN_TIMEOUT` mid-stream. Do
  not switch back to the `sync` worker class for this app unless you also raise
  `GUNICORN_TIMEOUT` high enough to cover the slowest expected download.

## Large file uploads

A reverse proxy in front of the app (e.g. Cloudflare, which fronts Sevalla)
typically rejects any single request body over ~100 MB. So a logged-in upload
larger than ~80 MB is sliced in the browser into smaller chunks, sent one at a
time, and reassembled server-side into the final file (smaller files and folder
uploads still post in batches as before). This bypasses the proxy body limit, so
single files can be arbitrarily large -- the only real ceiling is your
**persistent disk** (each file is stored whole on disk), which you should size
accordingly and optionally bound with `DJANGO_MAX_UPLOAD_BYTES`.

An interrupted large upload leaves a staging `.part` file under
`MEDIA_ROOT/.chunks/`. Run the cleanup command periodically (e.g. a daily cron)
to remove abandoned ones:

```bash
python manage.py cleanup_chunks --hours 24
```

## How sharing works

- Click **Share** next to any file or folder to mint a link at `/s/<token>/`.
- Folder links list the files inside; nested subfolders are shown but not
  browsable through the link (a deliberate, simple boundary).
- Set an expiry in days when creating a link, or leave it blank for no expiry.
- Set a password to protect a link; viewers must enter it before they can see
  or download the shared file or folder. Leave it blank for a public link.
- Revoke any link from the **Share links** page.

## Notes for production

`runserver` defaults to the bundled dev settings (`DEBUG=True`, a dev
`SECRET_KEY`, permissive localhost hosts). For a real deployment, drive the app
through the `DJANGO_*` environment variables above (the Docker setup already
does this). At minimum set `DJANGO_DEBUG=False`, a strong `DJANGO_SECRET_KEY`,
and a real `DJANGO_ALLOWED_HOSTS`.

**Serving uploaded media (important).** The raw `/media/` route is registered
**only when `DEBUG=True`**. With `DEBUG=False`, Django does *not* serve uploaded
files, and it must not: files would otherwise be reachable at predictable,
unauthenticated URLs that bypass login, share-link expiry, and the password
gate. In production you must:

- Keep `MEDIA_ROOT` (`DJANGO_MEDIA_ROOT`) **outside any web-served path**, and
- Let all file access flow through the app's guarded views
  (`inline_document` / `download_document` for owners, `share_preview` /
  `share_download` for public links). For performance behind nginx you can wire
  those views to `X-Accel-Redirect` (or Apache `X-Sendfile`) so the proxy
  streams the bytes while the view still enforces access — but never expose
  `MEDIA_ROOT` directly.

**HTTPS hardening.** When `DEBUG=False` the app automatically enables
`Secure` session/CSRF cookies, HSTS, and an HTTP→HTTPS redirect, and trusts
`X-Forwarded-Proto` from the proxy. Terminate TLS at your proxy and forward that
header. Set `DJANGO_SECURE_SSL_REDIRECT=0` only if TLS is handled elsewhere.

**Inline previews.** Only a fixed allowlist of inert types (PNG, JPEG, GIF,
WebP, BMP, PDF, plain text) render inline; everything else — including
HTML, SVG, JSON, and XML — is served as a download. This is deliberate: an
uploaded HTML/SVG file rendered inline could execute scripts on the app's
origin (stored XSS).

**Rate-limit & cache.** The share-link password gate is brute-force throttled
using Django's cache. The default in-process cache is per-worker; for a
multi-process deployment configure a shared cache backend (e.g. Redis or
Memcached) so the lockout is effective across workers.
