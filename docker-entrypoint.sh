#!/bin/sh
set -e

# Apply migrations against the mounted database before the server starts.
python manage.py migrate --noinput

exec "$@"
