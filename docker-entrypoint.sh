#!/bin/sh
set -e

# Ensure the SQLite DB dir and media dir exist and are writable. On a host with
# a persistent volume (e.g. Sevalla) these point inside the mount; the mount may
# start empty, so create the paths before migrating.
mkdir -p "$(dirname "${DJANGO_DB_PATH:-/app/data/db.sqlite3}")" \
         "${DJANGO_MEDIA_ROOT:-/app/media}" 2>/dev/null || true

# Apply migrations against the mounted database before the server starts.
python manage.py migrate --noinput

# Clear any chunked-upload staging files left by interrupted uploads. A Sevalla
# cron job can't mount this disk, so cleanup runs here (every deploy/restart) and
# opportunistically in the web process; see files.views._maybe_sweep_stale_chunks.
python manage.py cleanup_chunks --hours 24 || true

exec "$@"
