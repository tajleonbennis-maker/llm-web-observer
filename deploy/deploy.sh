#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${SURFACE_HOST:?Set SURFACE_HOST}"
: "${SURFACE_USER:?Set SURFACE_USER}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
release="${GITHUB_SHA:-$(git -C "$root" rev-parse --short HEAD)}"
tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=uv.lock \
  -czf "$tmp/release.tgz" -C "$root" .

ssh_opts=(-o StrictHostKeyChecking=yes)
[[ -n "${SURFACE_KNOWN_HOSTS:-}" ]] && ssh_opts+=(-o "UserKnownHostsFile=$SURFACE_KNOWN_HOSTS")
[[ -n "${SURFACE_IDENTITY_FILE:-}" ]] && ssh_opts+=(-i "$SURFACE_IDENTITY_FILE")
transport=()
if [[ -n "${SURFACE_PASSWORD:-}" ]]; then
  export SSHPASS=$SURFACE_PASSWORD
  transport=(sshpass -e)
fi

target="$SURFACE_USER@$SURFACE_HOST"
"${transport[@]}" scp "${ssh_opts[@]}" "$tmp/release.tgz" "$target:/tmp/lwo-release.tgz"
"${transport[@]}" ssh "${ssh_opts[@]}" "$target" \
  "sudo rm -rf /tmp/lwo-release && sudo mkdir -p /tmp/lwo-release && sudo tar -xzf /tmp/lwo-release.tgz -C /tmp/lwo-release && sudo bash /tmp/lwo-release/deploy/remote-install.sh /tmp/lwo-release '$release'"
