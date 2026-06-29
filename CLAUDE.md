# CLAUDE.md

Context for Claude Code working on **DocShare**, a small Django site for
sharing documents and folders.

## What this project is

Users sign up, organize files into nested folders (private to them), and hand
out public share links so anyone can view or download a file or folder without
an account. Images, PDFs, and text files preview inline.

## Stack

- **Django 5/6**, SQLite (`db.sqlite3`)
- **Tailwind via the Play CDN** loaded in `templates/base.html` — there is NO
  build step, no npm, no `tailwind.config.js`. Style by writing utility classes
  directly in templates. Do not introduce a Tailwind build pipeline unless asked.
- Uploaded files live under `media/` (served by Django in DEBUG only)
- Plain Django templates, no Cotton, no Alpine, no jQuery. Vanilla JS only, and
  only where genuinely needed (currently just `confirm()` on delete forms).

## Layout

```
config/          settings, root urls, wsgi/asgi
accounts/        signup view + built-in login/logout wiring
files/           Folder, Document, ShareLink models + all file/share views
templates/
  base.html              navbar, messages, Tailwind CDN
  registration/login.html
  accounts/signup.html
  files/                 browse, preview, links, share_folder, share_document
media/                   uploads (gitignored)
```

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver          # http://127.0.0.1:8000/
python manage.py createsuperuser    # optional, for /admin
python manage.py makemigrations files && python manage.py migrate  # after model changes
```

There is no test suite yet. If you add features, add tests under
`files/tests.py` and run `python manage.py test`.

## Data model

- **Folder** — self-referential (`parent` FK to self, null = root), `owner` FK.
  `breadcrumbs` property walks the parent chain for the path UI.
- **Document** — `file`, `folder` (null = root level), `owner`, `content_type`,
  `size`. `kind` property returns `image|pdf|text|other` and drives which
  preview renderer the template uses. `pretty_size` formats bytes.
- **ShareLink** — `token` (UUID, the public URL part), exactly one of `folder`
  or `document` set, optional `expires_at`. `is_expired` and `target` helpers.

## Conventions to respect

- **Ownership scoping is non-negotiable.** Every owner-facing view fetches
  objects with `get_object_or_404(Model, pk=..., owner=request.user)`. Never
  query by pk alone in an authenticated view.
- **Public access goes through guards.** `_valid_link(token)` rejects expired
  links; `_is_descendant(doc, root_folder)` confirms a file is actually
  reachable through the shared folder before serving it; `_shared_doc_or_404`
  scopes the lookup to the link's owner and fails closed. Any new public route
  MUST use these. The core invariant: a share link must never expose a file
  outside its target. There is an e2e check for this behavior — preserve it.
- **Never serve raw uploaded files.** All file bytes (owner and public) flow
  through the guarded views via `_serve_file`; the `/media/` route exists only
  under `DEBUG`. Do not add `doc.file.url` to a template or re-enable public
  media — that bypasses every guard above. This holds whether files live on the
  local disk or in an object-storage bucket (see below): the bucket MUST stay
  private and bytes always stream through `_serve_file`, never via a public
  object URL or a presigned link handed out before the guards run.
- **Storage backend is env-driven.** Files live on the local disk
  (`FileSystemStorage`) by default; setting `DJANGO_S3_BUCKET` (+ keys/endpoint)
  switches the default storage to a private S3-compatible bucket (Sevalla/R2)
  with zero code change. Local dev and the test suite run on the disk with no S3.
  Serving, previews, and the "Download all" zip already stream via
  `doc.file.open("rb")`, so they work over either backend unchanged. The chunk
  staging dir (`.chunks/`) always stays on local disk regardless. See README's
  "Object storage" section.
- **Large uploads are chunked, not single-request.** A reverse proxy (Cloudflare
  on Sevalla) caps a single request body at ~100 MB. Files over ~80 MB are
  sliced client-side and POSTed to `upload_chunk` / `upload_chunk_complete`,
  which append to a per-user staging file under `MEDIA_ROOT/.chunks/` (never
  web-served) and move it into final storage via `_store_assembled_file` (local
  disk: `os.replace`, no multi-GB second copy; object storage: stream up, then
  drop the local part). The server keeps NO state between chunk requests — the
  `.part` file's size is the state — so it stays correct across multiple gunicorn
  workers. These views
  are owner-scoped; `upload_id` must be a UUID and paths are sanitized exactly
  like `upload_folder`. Don't add chunked uploads to the anonymous path.
- **File delivery is XSS-safe by construction.** Content-Type is re-derived
  server-side with `_safe_content_type` (the client-sent header is untrusted),
  and only the `INLINE_CONTENT_TYPES` allowlist (images/PDF/plain text) is
  served inline; everything else (HTML, SVG, JSON, XML, ...) is forced to
  download, with `X-Content-Type-Options: nosniff`. Don't widen the allowlist
  to active types.
- **Form-invalid paths fail closed.** Mutating views that take a form (e.g.
  `create_share`) must NOT act on invalid input — never mint a share link (or
  any object) from a form that didn't validate.
- **URL names are the API.** Templates reverse by name (`browse`,
  `create_share`, `share_view`, `share_download`, `share_preview`, `my_links`,
  etc.). Keep names stable or update every `{% url %}` together.
- Redirect helpers return to the parent folder when one exists, else `browse`.
- Use Django `messages` for user feedback (already wired into `base.html`).
- Design language: slate/indigo Tailwind palette, `max-w-5xl` content column,
  cards are `bg-white border border-slate-200 rounded-lg`. Match it.
- No emojis in code comments or commit messages. (Folder/file glyphs in
  templates are intentional UI, leave those.)

## Current state

Working and tested end to end (`files/tests.py`): signup/login, nested folders,
single-file upload, whole-folder upload (rebuilds the tree), chunked large-file
upload (bypasses the ~100 MB proxy body limit), folder move, inline previews,
public share links with optional expiry and optional password, link
management + revoke. A security audit hardened file serving, share-link
creation, the anonymous-upload path, and production settings — see the
conventions above and the README's production notes.

## Production settings (env-driven, see README)

Settings read from `DJANGO_*` env vars with safe defaults. Notably: with
`DEBUG=False` there is NO `ALLOWED_HOSTS='*'` fallback (fail-closed), the
`/media/` route is not registered, secure-cookie/HSTS/SSL-redirect hardening
turns on, `X-Forwarded-For` is trusted only when `DJANGO_TRUST_X_FORWARDED_FOR`
is set, and the multipart parser has high-but-finite field/file caps. Keep these
behaviors; don't reintroduce wildcard hosts, public media, or unbounded caps.

## Deliberate simplifications (only change if asked)

- A shared folder lists its subfolders but they are NOT browsable through the
  public link — only files directly in the shared folder (and, for download,
  nested files via `_is_descendant`) are reachable.
- Drag-and-drop is not wired up (folder upload uses a `webkitdirectory` input).

## Likely next tasks

Browsable subfolders in shared views; drag-and-drop multi-file upload; storage
quota per user; rename files and folders; a shared cache backend (the password
brute-force throttle uses Django's cache, per-process by default).

## Working style

Make focused changes, run `python manage.py check` and `runserver` to verify,
and explain what you changed and why. When touching models, always create and
apply the migration. Ask before adding new dependencies — the appeal of this
project is that it is small and runs with just Django.
