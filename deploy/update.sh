#!/usr/bin/env bash
# Update HydroGeom on the CIROH AWS server: pull the latest code from GitHub,
# rebuild, and restart. Keep a copy at ~/update.sh on the server.
# Usage:  ~/update.sh                 (code changes)
#         ~/update.sh --clear-cache   (after replacing the data files)
set -e
cd ~/HydroGeom
git pull
docker build -t hydrogeom .          # if this fails, the running app is left alone

if [ "$1" = "--clear-cache" ]; then
  echo "Removing the saved CONUS files..."
  sudo rm -f ~/hydrogeom-cache/*
fi

docker rm -f hydrogeom
docker run -d --name hydrogeom --restart unless-stopped -p 8000:8000 \
  -v ~/hydrogeom-data/reaches.parquet:/app/hydrogeomapi/reaches.parquet:ro \
  -v ~/hydrogeom-data/gages.parquet:/app/hydrogeomapi/gages.parquet:ro \
  -v ~/hydrogeom-cache:/app/cache \
  hydrogeom

echo "Waiting for the app to start..."
for i in $(seq 1 30); do
  if curl -fs http://localhost:8000/health >/dev/null; then
    echo "HydroGeom is running."
    curl -s http://localhost:8000/status; echo
    if [ "$1" = "--clear-cache" ]; then
      echo "Rebuilding the CONUS files (about 3 minutes)..."
      for f in shapefile csv; do
        curl -s -o /dev/null -w "  $f: code %{http_code}, %{time_total}s\n" -X POST http://localhost:8000/extract \
          -H "Content-Type: application/json" -d "{\"request_type\":\"conus\",\"dataset\":\"reach\",\"format\":\"$f\"}"
      done
    fi
    exit 0
  fi
  sleep 2
done
echo "The app did not start. Recent log:"
docker logs hydrogeom --tail 30
exit 1
