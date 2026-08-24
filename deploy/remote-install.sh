#!/usr/bin/env bash
set -euo pipefail

release=${1:?release path required}
image="llm-web-observer:${2:-latest}"
data_dir=${LWO_DATA_DIR:-/var/lib/llm-web-observer}
env_file=${LWO_ENV_FILE:-$data_dir/mitmproxy/observer.env}
network=${LWO_NETWORK:-deeptutor-net}

test -r "$env_file" || { echo "Missing readable environment file: $env_file" >&2; exit 1; }
docker network inspect "$network" >/dev/null
docker build -t "$image" "$release"
install -m 0644 "$release/integrations/mitmproxy/lwo_addon.py" "$data_dir/mitmproxy/addons/lwo_mitm_addon.py"
install -m 0644 "$release/deploy/deeptutor-proxy.conf" /var/lib/buildproof-surface/proxy/deeptutor.conf

docker rm -f llm-web-observer >/dev/null 2>&1 || true
docker run -d --name llm-web-observer --restart unless-stopped -p 8080:8080 \
  --env-file "$env_file" -e LWO_DATABASE=/data/observer.db \
  -v "$data_dir:/data" "$image" >/dev/null
docker network connect "$network" llm-web-observer

if docker container inspect deeptutor-mitm >/dev/null 2>&1; then
  docker restart deeptutor-mitm >/dev/null
fi

docker rm -f deeptutor-proxy >/dev/null 2>&1 || true
docker run -d --name deeptutor-proxy --restart unless-stopped --network "$network" \
  -p 3783:80 -v /var/lib/buildproof-surface/proxy/deeptutor.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:1.27-alpine >/dev/null

for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8080/health >/dev/null \
    && curl -fsS http://127.0.0.1:3783/ | grep -q '/_lwo/client.js' \
    && curl -fsS http://127.0.0.1:3783/_lwo/client.js | grep -q fingerprint \
    && { echo "Deployment verified: observer=:8080 deeptutor=:3783"; exit 0; }
  sleep 2
done
echo "Deployment health check failed" >&2
exit 1
