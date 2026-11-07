#!/bin/sh
# web    → migrate, create the cache table, refresh static files for the proxy, serve with gunicorn
# worker → background jobs (reminders, deadlines, delayed bots, scheduled starts, webhook retries) + nightly backup
# backup → one backup now
set -e
case "$1" in
  web)
    python manage.py migrate --noinput
    python manage.py createcachetable
    python manage.py collectstatic --noinput --clear -v 0  # the static volume outlives the image; keep it current
    exec gunicorn paperlane.wsgi --bind 0.0.0.0:8000 --workers "${WEB_WORKERS:-3}" --timeout 120 --access-logfile - ;;
  worker)
    python manage.py createcachetable
    if [ -n "$PAPERLANE_BACKUP_AT" ]; then
      ( while true; do
          now=$(date +%H:%M); [ "$now" = "$PAPERLANE_BACKUP_AT" ] && python manage.py backup || true; sleep 60
        done ) &
    fi
    exec python manage.py paperlane_tick --loop "${PAPERLANE_SCHEDULER_SECONDS:-30}" ;;
  backup)
    exec python manage.py backup ;;
  *)
    exec "$@" ;;
esac
