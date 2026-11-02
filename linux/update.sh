#!/usr/bin/env bash
# Build and restart the A.C.E.S. MCP server on this Linux host.
#
#   sudo linux/update.sh                 get the latest commit, build it, restart
#   sudo linux/update.sh --no-pull       build what's checked out now, restart
#   sudo linux/update.sh --restart       no build; restart so a settings change takes effect
#   sudo linux/update.sh --rollback TAG  go back to an earlier build (list: docker image ls aces-mcp)
#
# Each build is tagged with its commit, so earlier builds stay on the host for rollback.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/.." && pwd)
compose=(docker compose -f "$here/docker-compose.yml")

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo (the settings file is readable by root only)."
[[ -f /etc/aces/aces.env ]] || die "/etc/aces/aces.env is missing. Run sudo linux/install.sh first."

mode=build
pull=yes
tag=""
case "${1:-}" in
  "") ;;
  --no-pull) pull=no ;;
  --restart) mode=restart ;;
  --rollback)
    mode=rollback
    tag=${2:-}
    [[ -n $tag ]] || die "give the tag to go back to (list: docker image ls aces-mcp)"
    ;;
  *) die "unknown option: $1" ;;
esac

previous=$(docker inspect -f '{{.Config.Image}}' aces 2>/dev/null || true)

wait_healthy() {
  local status=starting
  for _ in $(seq 1 24); do
    sleep 5
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' aces 2>/dev/null || echo missing)
    case $status in
      healthy)
        echo "A.C.E.S. is up and healthy ($(docker inspect -f '{{.Config.Image}}' aces))."
        return 0
        ;;
      unhealthy | missing) break ;;
    esac
  done
  echo "A.C.E.S. isn't healthy (status: $status). Last log lines:" >&2
  docker logs --tail 40 aces >&2 || true
  if [[ -n $previous ]]; then
    echo "To go back to what was running before: sudo linux/update.sh --rollback ${previous#aces-mcp:}" >&2
  fi
  return 1
}

case $mode in
  restart)
    tag=${previous#aces-mcp:}
    [[ -n $tag ]] || die "nothing is running yet. Run sudo linux/update.sh --no-pull to build first."
    ACES_BUILD_TAG=$tag "${compose[@]}" up -d --no-build --force-recreate
    ;;
  rollback)
    docker image inspect "aces-mcp:$tag" >/dev/null 2>&1 \
      || die "no build aces-mcp:$tag on this host (list: docker image ls aces-mcp)"
    ACES_BUILD_TAG=$tag "${compose[@]}" up -d --no-build --force-recreate
    ;;
  build)
    # Run git as the repo's owner, so root doesn't trip git's ownership check.
    owner=$(stat -c %U "$repo")
    as_owner() { if [[ $owner == root ]]; then "$@"; else sudo -u "$owner" "$@"; fi; }
    if [[ $pull == yes ]]; then
      as_owner git -C "$repo" pull --ff-only
    fi
    tag=$(as_owner git -C "$repo" rev-parse --short HEAD)
    as_owner git -C "$repo" diff --quiet HEAD || tag="$tag-modified"
    echo "Building aces-mcp:$tag"
    ACES_BUILD_TAG=$tag "${compose[@]}" build
    ACES_BUILD_TAG=$tag "${compose[@]}" up -d
    ;;
esac

wait_healthy
