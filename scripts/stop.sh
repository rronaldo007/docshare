#!/bin/sh
# Stop and remove the DocShare container and network.
# Data lives in named volumes, so it survives this; pass --volumes to wipe it.
set -e

cd "$(dirname "$0")/.."

if [ "$1" = "--volumes" ]; then
    docker compose down --volumes
    echo "Stopped and removed volumes (database and uploads wiped)."
else
    docker compose down
    echo "Stopped. Data volumes kept (use 'scripts/stop.sh --volumes' to wipe them)."
fi
