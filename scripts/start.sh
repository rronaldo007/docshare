#!/bin/sh
# Build (if needed) and start the DocShare container in the background.
set -e

cd "$(dirname "$0")/.."

docker compose up --build -d

echo
echo "DocShare is starting at http://127.0.0.1:8000/"
echo "View logs with:  docker compose logs -f web"
echo "Stop it with:    scripts/stop.sh"
