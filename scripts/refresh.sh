#!/bin/sh
# Rebuild the image from current source and restart, keeping data volumes.
# Use after changing code or dependencies.
set -e

cd "$(dirname "$0")/.."

docker compose down
docker compose up --build -d

echo
echo "DocShare rebuilt and restarted at http://127.0.0.1:8000/"
echo "Migrations run automatically on start; data volumes were kept."
