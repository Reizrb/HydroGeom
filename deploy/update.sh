#!/usr/bin/env bash
# Update HydroGeom on the CIROH AWS server: pull the latest code from GitHub,
# rebuild, and restart. Keep a copy at ~/update.sh on the server.
# Usage:  ~/update.sh                  code changes, or after editing ~/hydrogeom.env
#         ~/update.sh --prepare-maps   also build any missing state and HUC2 map data
#         ~/update.sh --clear-cache    after replacing the data files: remove all saved
#                                      files, then rebuild the CONUS files and map data
set -e
CLEAR=0; MAPS=0
for arg in "$@"; do
  case "$arg" in
    --clear-cache)  CLEAR=1; MAPS=1 ;;
    --prepare-maps) MAPS=1 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

cd ~/HydroGeom
git pull
docker build -t hydrogeom .          # if this fails, the running app is left alone

if [ "$CLEAR" = 1 ]; then
  echo "Removing the saved files (CONUS downloads and map data)..."
  sudo rm -rf ~/hydrogeom-cache/*
fi

touch ~/hydrogeom.env                # server settings, e.g. CG_MAX_MAP_ROWS=470000
docker rm -f hydrogeom
docker run -d --name hydrogeom --restart unless-stopped -p 8000:8000 \
  --env-file ~/hydrogeom.env \
  -v ~/hydrogeom-data/reaches.parquet:/app/hydrogeomapi/reaches.parquet:ro \
  -v ~/hydrogeom-data/gages.parquet:/app/hydrogeomapi/gages.parquet:ro \
  -v ~/hydrogeom-cache:/app/cache \
  hydrogeom

echo "Waiting for the app to start..."
for i in $(seq 1 30); do
  if curl -fs http://localhost:8000/health >/dev/null; then
    echo "HydroGeom is running."
    curl -s http://localhost:8000/status; echo
    if [ "$CLEAR" = 1 ]; then
      echo "Rebuilding the CONUS files (about 8 minutes)..."
      for f in shapefile csv geojson; do
        curl -s -o /dev/null -w "  $f: code %{http_code}, %{time_total}s\n" \
          "http://localhost:8000/download/conus?dataset=reach&format=$f"
      done
    fi
    if [ "$MAPS" = 1 ]; then
      echo "Preparing map data for every state and HUC2 region (about 10 minutes)..."
      docker exec hydrogeom python -c "import sys; sys.path.insert(0, 'hydrogeomapi'); import app; app.prepare_map_cache()"
    fi
    exit 0
  fi
  sleep 2
done
echo "The app did not start. Recent log:"
docker logs hydrogeom --tail 30
exit 1
