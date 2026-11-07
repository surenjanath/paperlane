#!/usr/bin/env bash
# Update a Paperlane install to the latest code: backup, pull, rebuild, migrate (on start), health check.
#
#   ./deploy/update.sh            # pull the current branch from git, then rebuild
#   ./deploy/update.sh --no-pull  # rebuild what is on disk (e.g. after editing .env or copying new code)
set -euo pipefail
cd "$(dirname "$0")/.."

PULL=1; [ "${1:-}" = "--no-pull" ] && PULL=0
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
COMPOSE="$DOCKER compose"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mx   %s\033[0m\n' "$*"; exit 1; }

[ -f .env ] || die "No .env here. Run ./deploy/install.sh first."

if [ -n "$($COMPOSE ps -q worker 2>/dev/null)" ]; then
  say "Backing up before the update"
  $COMPOSE exec -T worker python manage.py backup
fi

if [ "$PULL" = 1 ] && [ -d .git ]; then
  say "Pulling the latest code"
  git pull --ff-only
fi

say "Rebuilding and restarting"
$COMPOSE up -d --build --remove-orphans

say "Waiting for Paperlane to report healthy"
for i in $(seq 1 60); do
  $COMPOSE exec -T web python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz/')" >/dev/null 2>&1 && { ok=1; break; }
  sleep 3
done
[ "${ok:-0}" = 1 ] || { $COMPOSE logs --tail 60 web; die "Paperlane didn't become healthy after the update. Restore with ./deploy/manage.sh restore <latest backup>."; }

$DOCKER image prune -f >/dev/null
echo "Updated. $(git log -1 --format='Now at %h: %s' 2>/dev/null || true)"
