#!/usr/bin/env bash
# Day-to-day tasks for a Paperlane install made with deploy/install.sh. Run ./deploy/manage.sh help for the list.
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
COMPOSE="$DOCKER compose"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mx   %s\033[0m\n' "$*"; exit 1; }
healthy() {
  for i in $(seq 1 60); do
    $COMPOSE exec -T web python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz/')" >/dev/null 2>&1 && return 0
    sleep 3
  done
  $COMPOSE logs --tail 60 web; die "Paperlane didn't become healthy. The web log is above."
}
confirm() {
  [ "${YES:-0}" = 1 ] && return 0
  read -r -p "$1 Type yes to continue: " a </dev/tty; [ "$a" = yes ] || die "Cancelled."
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  status)
    $COMPOSE ps
    echo; $COMPOSE exec -T web python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz/').read().decode())" ;;

  logs)
    $COMPOSE logs --tail 200 -f "$@" ;;

  backup)
    say "Backing up the database and uploaded files"
    $COMPOSE exec -T worker python manage.py backup ;;

  backups)
    $COMPOSE exec -T worker sh -c 'ls -1 /backups | grep "^paperlane-" || echo "No backups yet."' ;;

  backup-copy)
    dest="${1:?usage: manage.sh backup-copy /folder (copies every backup out of the Docker volume)}"
    mkdir -p "$dest"
    id=$($COMPOSE ps -q worker); [ -n "$id" ] || die "The worker isn't running."
    $DOCKER cp "$id:/backups/." "$dest/"
    echo "Copied to $dest. Keep a copy on another machine or in object storage." ;;

  restore)
    src="${1:?usage: manage.sh restore paperlane-YYYYMMDD-HHMMSS  (a name from manage.sh backups, or a folder path)}"
    if [ -d "$src" ]; then
      name=$(basename "$src")
      $COMPOSE cp "$src" "worker:/backups/$name"
    else
      name="$src"
    fi
    $COMPOSE exec -T worker test -f "/backups/$name/manifest.json" || die "No backup called $name."
    confirm "This replaces ALL current documents, users and uploads with backup $name. A safety backup is taken first."
    say "Safety backup of the current state"
    $COMPOSE exec -T worker python manage.py backup
    say "Stopping web and worker"
    $COMPOSE stop web worker
    trap '$COMPOSE up -d >/dev/null 2>&1; echo "Restore failed; Paperlane was started again on whatever data the database now holds. The safety backup above has the state before the restore." >&2' ERR
    say "Restoring database and files"
    # the app image's client tools match the dump format, so restore from there rather than the db container
    $COMPOSE run --rm --no-deps -T worker sh -ec '
      export PGPASSWORD="$POSTGRES_PASSWORD"
      if [ -f "/backups/'"$name"'/db.dump" ]; then
        pg_restore --clean --if-exists --no-owner -h db -U paperlane -d paperlane "/backups/'"$name"'/db.dump"
      else
        echo "This backup has no PostgreSQL dump (db.dump); it was made from a SQLite install." >&2; exit 1
      fi
      if [ -f "/backups/'"$name"'/media.tar.gz" ]; then
        find /app/media -mindepth 1 -delete
        tar -xzf "/backups/'"$name"'/media.tar.gz" -C /app
      fi'
    trap - ERR
    $COMPOSE up -d
    healthy
    echo "Restored $name." ;;

  reset-admin)
    email="${1:?usage: manage.sh reset-admin admin@example.com}"
    pw=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
    $COMPOSE exec -T -e PAPERLANE_ADMIN_PASSWORD="$pw" web python manage.py bootstrap --email "$email" --reset-password
    echo "New password for $email: $pw  (shown once; change it after signing in)" ;;

  shell)
    $COMPOSE exec web python manage.py shell ;;

  manage)
    $COMPOSE exec web python manage.py "$@" ;;

  stop)
    $COMPOSE stop ;;

  start)
    $COMPOSE up -d; healthy; echo "Running." ;;

  help|*)
    cat <<'EOF'
Usage: ./deploy/manage.sh <command>

  status                 containers and the /healthz/ checks
  logs [service]         follow logs (web, worker, db, caddy)
  backup                 back up the database and uploads now
  backups                list backups in the Docker volume
  backup-copy DIR        copy every backup out of the volume to DIR
  restore NAME|DIR       restore a backup (asks first; takes a safety backup)
  reset-admin EMAIL      set a new random password for an account and make it super admin
  shell                  Django shell
  manage ARGS...         any manage.py command, e.g. manage seal_documents
  stop | start           stop or start everything

Updates: ./deploy/update.sh
EOF
    ;;
esac
