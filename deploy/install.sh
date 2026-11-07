#!/usr/bin/env bash
# One-step production install of Paperlane on a fresh Linux server (Ubuntu/Debian; other distros need Docker first).
#
#   sudo ./deploy/install.sh                                   # asks for what it needs
#   sudo ./deploy/install.sh --domain docs.example.com --email admin@example.com --workspace "Acme Insurance" --yes
#
# What it does: installs Docker if missing, opens ports 80/443 on ufw, writes .env with fresh secrets (an existing
# .env is kept and only missing keys are added), builds and starts PostgreSQL + web + worker + Caddy (automatic
# HTTPS), waits for /healthz/, creates the super admin and first workspace, and prints the sign-in details.
# Re-running it is safe: secrets, data and the admin password are left alone.
#
# Options: --domain D  --email E  --workspace NAME  --smtp-host H --smtp-port P --smtp-user U --smtp-password PW
#          --from "Name <addr>"  --no-proxy (bring your own TLS proxy; web listens on 127.0.0.1:8000)  --yes
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
ENV_FILE="$ROOT/.env"

DOMAIN="" EMAIL="" WORKSPACE="" SMTP_HOST="" SMTP_PORT="587" SMTP_USER="" SMTP_PASSWORD="" FROM="" PROXY=1 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift ;;
    --email) EMAIL="$2"; shift ;;
    --workspace) WORKSPACE="$2"; shift ;;
    --smtp-host) SMTP_HOST="$2"; shift ;;
    --smtp-port) SMTP_PORT="$2"; shift ;;
    --smtp-user) SMTP_USER="$2"; shift ;;
    --smtp-password) SMTP_PASSWORD="$2"; shift ;;
    --from) FROM="$2"; shift ;;
    --no-proxy) PROXY=0 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
  shift
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!   %s\033[0m\n' "$*"; }
die() { printf '\033[31mx   %s\033[0m\n' "$*"; exit 1; }
ask() {  # ask VAR "Question" [default]
  local __v="$1" __q="$2" __d="${3:-}" __a
  [ -n "${!__v}" ] && return 0
  [ "$YES" = 1 ] && { printf -v "$__v" '%s' "$__d"; return 0; }
  read -r -p "$__q${__d:+ [$__d]}: " __a </dev/tty || true
  printf -v "$__v" '%s' "${__a:-$__d}"
}
envget() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
envset() {  # set KEY only if .env doesn't already have a value for it
  local cur; cur=$(envget "$1")
  [ -n "$cur" ] && return 0
  if grep -qE "^$1=" "$ENV_FILE" 2>/dev/null; then
    local tmp; tmp=$(mktemp); grep -vE "^$1=" "$ENV_FILE" > "$tmp"; cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
  fi
  printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
}
secret() { head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-50}"; }

# ---------------------------------------------------------------------------------------------------------------
say "Checking the server"
SUDO=""
if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || die "Run as root, or install sudo."
  SUDO="sudo"
fi

if ! command -v docker >/dev/null 2>&1; then
  [ "$(uname -s)" = "Linux" ] || die "Install Docker Desktop first, then re-run."
  say "Installing Docker (get.docker.com)"
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"
$DOCKER info >/dev/null 2>&1 || die "Docker is installed but not running (try: systemctl start docker)."
$DOCKER compose version >/dev/null 2>&1 || die "The Docker Compose plugin is missing (apt-get install docker-compose-plugin)."
COMPOSE="$DOCKER compose"

# ---------------------------------------------------------------------------------------------------------------
say "Settings"
DOMAIN=${DOMAIN:-$(envget PAPERLANE_DOMAIN)}
EMAIL=${EMAIL:-$(envget PAPERLANE_ACME_EMAIL)}
ask DOMAIN "Domain Paperlane will run on (DNS must point here), e.g. docs.example.com"
ask EMAIL "Super admin email (also used for the HTTPS certificate)"
ask WORKSPACE "Name of the first workspace (blank to create it later in Super admin)" ""
[ -n "$DOMAIN" ] || die "A domain is required (use localhost to try it on this machine)."
[[ "$EMAIL" == *@*.* || "$EMAIL" == *@localhost ]] || die "That email address doesn't look right: $EMAIL"
DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN%%/*}

if [ -z "$(envget PAPERLANE_EMAIL_HOST)" ] && [ -z "$SMTP_HOST" ] && [ "$YES" = 0 ]; then
  echo "Outgoing email (invitations, document links, reminders). Leave the host blank to set it up later in .env."
  ask SMTP_HOST "SMTP host" ""
  if [ -n "$SMTP_HOST" ]; then
    ask SMTP_PORT "SMTP port" "587"
    ask SMTP_USER "SMTP user" ""
    read -r -s -p "SMTP password: " SMTP_PASSWORD </dev/tty || true; echo
  fi
fi

if [ "$DOMAIN" != "localhost" ] && command -v getent >/dev/null; then
  ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}') || true
  me=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
  if [ -z "$ip" ]; then warn "$DOMAIN doesn't resolve yet. HTTPS will start working once its DNS points to this server."
  elif [ -n "$me" ] && [ "$ip" != "$me" ]; then warn "$DOMAIN points to $ip but this server is $me. Check the DNS record."
  fi
fi

# ---------------------------------------------------------------------------------------------------------------
say "Writing .env"
[ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)" || : > "$ENV_FILE"
chmod 600 "$ENV_FILE"
envset PAPERLANE_DOMAIN "$DOMAIN"
envset PAPERLANE_ACME_EMAIL "$EMAIL"
envset PAPERLANE_SECRET_KEY "$(secret 64)"
envset POSTGRES_PASSWORD "$(secret 32)"
envset PAPERLANE_DEBUG 0
envset PAPERLANE_ALLOWED_HOSTS "$DOMAIN,127.0.0.1,localhost"
if [ "$PROXY" = 1 ] || [ "$DOMAIN" != "localhost" ]; then envset PAPERLANE_SITE_URL "https://$DOMAIN"; else envset PAPERLANE_SITE_URL "http://localhost:8000"; fi
envset PAPERLANE_HTTPS "$([ "$PROXY" = 1 ] && echo 1 || echo 0)"
envset PAPERLANE_SCHEDULER 0
envset PAPERLANE_CACHE db
envset PAPERLANE_BACKUP_KEEP 14
envset PAPERLANE_BACKUP_AT "02:30"
envset PAPERLANE_TIME_ZONE "America/Port_of_Spain"
envset PAPERLANE_LOG_LEVEL INFO
envset COMPOSE_PROFILES "$([ "$PROXY" = 1 ] && echo https || echo '')"
if [ -n "$SMTP_HOST" ]; then
  envset PAPERLANE_EMAIL_BACKEND django.core.mail.backends.smtp.EmailBackend
  envset PAPERLANE_EMAIL_HOST "$SMTP_HOST"
  envset PAPERLANE_EMAIL_PORT "$SMTP_PORT"
  envset PAPERLANE_EMAIL_USER "$SMTP_USER"
  envset PAPERLANE_EMAIL_PASSWORD "$SMTP_PASSWORD"
  envset PAPERLANE_EMAIL_TLS 1
  envset PAPERLANE_FROM_EMAIL "${FROM:-Paperlane <no-reply@$DOMAIN>}"
fi
MAIL_READY=$([ -n "$(envget PAPERLANE_EMAIL_HOST)" ] && echo 1 || echo 0)

# ---------------------------------------------------------------------------------------------------------------
if [ "$(uname -s)" = "Linux" ] && command -v ufw >/dev/null && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  say "Opening ports 80 and 443 on the firewall"
  $SUDO ufw allow 80/tcp >/dev/null; $SUDO ufw allow 443/tcp >/dev/null; $SUDO ufw allow 443/udp >/dev/null
fi

say "Building and starting Paperlane (the first build takes a few minutes)"
$COMPOSE up -d --build --remove-orphans

say "Waiting for Paperlane to report healthy"
for i in $(seq 1 60); do
  if $COMPOSE exec -T web python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz/')" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 3
done
[ "${ok:-0}" = 1 ] || { $COMPOSE logs --tail 60 web; die "Paperlane didn't become healthy. The web log is above."; }

# ---------------------------------------------------------------------------------------------------------------
say "Creating the super admin"
PASSWORD="$(secret 20)"  # only used if the account doesn't exist yet
out=$($COMPOSE exec -T -e PAPERLANE_ADMIN_PASSWORD="$PASSWORD" web \
  python manage.py bootstrap --email "$EMAIL" ${WORKSPACE:+--workspace "$WORKSPACE"}) || { echo "$out"; die "Creating the super admin failed."; }
echo "$out"
echo "$out" | grep -q "^Created super admin" || PASSWORD=""

if [ "$PROXY" = 1 ]; then
  URL="https://$DOMAIN"
  say "Checking HTTPS (the first certificate can take a minute)"
  CURL="curl -fsS --max-time 10 -o /dev/null"; [ "$DOMAIN" = localhost ] && CURL="$CURL -k"
  for i in $(seq 1 20); do
    $CURL "$URL/healthz/" 2>/dev/null && { https_ok=1; break; }
    sleep 6
  done
  [ "${https_ok:-0}" = 1 ] || warn "HTTPS isn't answering yet. Check DNS and that ports 80/443 are open, then: $COMPOSE logs caddy"
else
  URL="$(envget PAPERLANE_SITE_URL)"
fi

# ---------------------------------------------------------------------------------------------------------------
cat <<EOF

  Paperlane is running.

    Open        $URL
    Sign in     $EMAIL
EOF
if [ -n "$PASSWORD" ]; then
  echo "    Password    $PASSWORD"
  echo
  echo "  This password is shown once. Sign in, change it under Account > Security, and turn on two-step sign-in."
else
  echo "    Password    (unchanged; reset it with: ./deploy/manage.sh reset-admin $EMAIL)"
fi
cat <<EOF

  Backups run every night at $(envget PAPERLANE_BACKUP_AT) into the "backups" volume.
  Copy them off this server: ./deploy/manage.sh backup-copy /path/on/other/disk

  Update later with:   ./deploy/update.sh
  Other tasks:         ./deploy/manage.sh help
EOF
[ "$MAIL_READY" = 1 ] || warn "Email isn't set up, so invitations and document links won't be delivered. Add PAPERLANE_EMAIL_* to .env and run ./deploy/update.sh."
