#!/usr/bin/env bash
# Start (or restart) Caddy, which serves HydroGeom over HTTPS at hydrogeom.ciroh.org
# and gets and renews its security certificate automatically.
# Keep a copy at ~/caddy.sh on the server.
docker rm -f caddy 2>/dev/null || true
docker run -d --name caddy --restart unless-stopped --network host \
  -v caddy_data:/data \
  caddy:2 caddy reverse-proxy --from hydrogeom.ciroh.org --to localhost:8000
