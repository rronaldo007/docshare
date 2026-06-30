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
- `DJANGO_S3_BUCKET` - name of an S3-compatible bucket (e.g. Sevalla/Cloudflare
  R2) to store uploaded files in instead of the local disk. **Leave unset to keep
  files on disk** (the default; local dev and tests need no S3). When set, also
  provide `DJANGO_S3_ACCESS_KEY_ID`, `DJANGO_S3_SECRET_ACCESS_KEY`, and
  `DJANGO_S3_ENDPOINT_URL` (the bucket's S3 API endpoint); `DJANGO_S3_REGION`
  defaults to `auto` (fine for R2). See "Object storage" below.
- `DJANGO_DIRECT_UPLOAD` - set to `1` to let the browser upload file bytes
  **straight to the bucket** via presigned URLs (bypassing this app and the proxy
  body limit entirely). Off by default, and ignored unless `DJANGO_S3_BUCKET` is
  set. **Requires a CORS rule on the bucket** allowing your app origin to `PUT` --
  see "Direct uploads" below. `DJANGO_DIRECT_UPLOAD_EXPIRY` (default `3600`) sets
  the presigned-URL (and per-part) lifetime in seconds.
- `DJANGO_DIRECT_UPLOAD_PART_BYTES` - part size for the multipart direct upload
  used when a file exceeds the 5 GB single-PUT limit (default `268435456`, i.e.
  256 MB). S3/R2 allow at most 10,000 parts, so 256 MB covers files up to ~2.5 TB;
  raise it for larger files. Only relevant with direct upload on.
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
single files can be arbitrarily large -- the only real ceiling is your storage
(the **persistent disk** by default, or the **object-storage bucket** if you set
one up -- see below), which you should size accordingly and optionally bound with
`DJANGO_MAX_UPLOAD_BYTES`.

An interrupted large upload leaves a staging `.part` file under
`MEDIA_ROOT/.chunks/`. These are cleaned automatically: the app sweeps stale
staging files opportunistically after uploads (throttled to once an hour), and
again on every deploy/restart via the entrypoint. Cleanup must run **in the web
process**, which owns the disk -- a platform cron job (e.g. on Sevalla) runs as
a separate process with no access to the persistent disk, so it can't be used
here. You can also sweep manually:

```bash
python manage.py cleanup_chunks --hours 24
```

## Object storage (optional)

By default uploaded files are stored on the local disk under `MEDIA_ROOT`. For a
file-sharing app that grows, an S3-compatible **object-storage bucket** (e.g.
Sevalla / Cloudflare R2) is usually a better backend: far cheaper per GB, free
egress on R2, effectively unlimited capacity, and it removes the single-instance
limit a mounted persistent disk imposes.

To switch, set `DJANGO_S3_BUCKET` (plus `DJANGO_S3_ACCESS_KEY_ID`,
`DJANGO_S3_SECRET_ACCESS_KEY`, `DJANGO_S3_ENDPOINT_URL`, and optionally
`DJANGO_S3_REGION`). With those set, new uploads go to the bucket; with them
unset, nothing changes and files stay on disk. No code change, no migration of
existing rows is required for new files.

Things to know:

- **Keep the bucket private.** File bytes are still streamed through the app's
  guarded views (so every share-link guard keeps applying); they are never served
  from a public object URL. Do not make the bucket or its objects public.
- **The chunk staging dir stays on local disk.** Large uploads still assemble in
  `MEDIA_ROOT/.chunks/` and are streamed up to the bucket only once complete, so
  `MEDIA_ROOT` must remain a writable local path even with a bucket configured.
  Because staging is per-instance, keep uploads on a single instance (as the
  persistent-disk setup already does) unless you give all instances shared
  staging.
- Existing files already on disk are not moved automatically. This setup is for a
  fresh cutover; migrating old files into the bucket would be a separate one-off.

### Direct uploads (optional, object storage only)

With `DJANGO_DIRECT_UPLOAD=1` (and a bucket configured), the browser uploads file
bytes **straight to the bucket** instead of through this app: it asks the app for
a short-lived presigned `PUT` URL, PUTs the file to the bucket, then asks the app
to record it. The bytes never transit the app server or the proxy's ~100 MB body
limit, so even very large files upload in one shot.

A single presigned PUT tops out at S3/R2's 5 GB object limit. **Files over 5 GB
are uploaded with multipart**: the app opens a multipart session, hands out one
presigned `PUT` per part, the browser slices the file and PUTs each part straight
to the bucket, and the app finalizes by reading the part list and size **from the
bucket** (never the client). This keeps even a 15 GB upload entirely off the app
server and its local disk -- the binding limit becomes S3's 5 TB / 10,000 parts,
not the box's RAM or disk. Part size is `DJANGO_DIRECT_UPLOAD_PART_BYTES`. On
cancel or failure the browser asks the app to abort the session; also add a bucket
**lifecycle rule to auto-abort incomplete multipart uploads** after a few days so
an abandoned upload (closed tab) doesn't leave billable orphan parts. With direct
upload **off**, files over 5 GB fall back to the chunked uploader, which stages
the whole file on local disk first -- fine for a roomy disk, but it will fill a
small one, so direct upload (multipart) is the right path for multi-GB files.

This is **off by default and safe to leave off** -- uploads keep working through
the chunked/batched path. Before turning it on:

- **The bucket needs a CORS rule** allowing your app's origin to `PUT` (and to
  read the response). Browser-to-bucket uploads are cross-origin and fail without
  it. A minimal rule:

  ```json
  [
    {
      "AllowedOrigins": ["https://your-app-origin.example"],
      "AllowedMethods": ["PUT"],
      "AllowedHeaders": ["*"],
      "MaxAgeSeconds": 3600
    }
  ]
  ```

  Confirm your provider exposes bucket CORS config (on Cloudflare R2 it is under
  the bucket's Settings; whether Sevalla surfaces it may need a support check).
  The same `PUT` rule covers multipart -- because the app reads each part's ETag
  server-side via `ListParts` (rather than the browser reading it from the PUT
  response), no `ExposeHeaders: ETag` entry is needed.

The app side stays fail-closed: the object key is minted server-side under the
user's own `user_<id>/` prefix (a client can never presign or commit outside it),
and the commit step refuses any key it didn't mint or any object that wasn't
actually uploaded, reading the stored size from the bucket rather than trusting
the client. Bytes are still delivered by streaming through the guarded views, so
the bucket stays private.

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
