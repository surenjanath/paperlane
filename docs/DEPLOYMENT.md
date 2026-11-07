# Deploying Paperlane

The development server (`manage.py runserver`) is for local use only.

## One-step install (recommended)

On a fresh Ubuntu or Debian server with a domain pointing at it:

```bash
git clone https://github.com/<your-account>/paperlane.git && cd paperlane
sudo ./deploy/install.sh --domain docs.example.com --email admin@example.com --workspace "Acme Insurance"
```

It installs Docker if needed, opens ports 80/443 on ufw, writes `.env` with fresh secrets, starts PostgreSQL 17,
the web app, the jobs worker and Caddy (automatic HTTPS certificate), waits for `/healthz/`, creates the super admin
and the first workspace, and prints the sign-in details (the password is shown once). Without options it asks for
the domain, the admin email, the workspace name and the SMTP server. Re-running it is safe: secrets, data and the
admin password are kept. Add `--yes` to skip questions, `--no-proxy` to use your own TLS proxy (web then listens on
`127.0.0.1:8000`). Try it locally with `--domain localhost` (self-signed certificate).

Afterwards:

| Task | Command |
|---|---|
| Update to the latest code (backs up first) | `./deploy/update.sh` |
| Apply `.env` changes | `./deploy/update.sh --no-pull` |
| Status and health checks | `./deploy/manage.sh status` |
| Logs | `./deploy/manage.sh logs [web\|worker\|db\|caddy]` |
| Back up now / list backups | `./deploy/manage.sh backup` / `backups` |
| Copy backups off the volume | `./deploy/manage.sh backup-copy /mnt/offsite` |
| Restore (asks first, takes a safety backup) | `./deploy/manage.sh restore paperlane-YYYYMMDD-HHMMSS` |
| New password for an account (makes it super admin) | `./deploy/manage.sh reset-admin admin@example.com` |

Only workspace logos and profile pictures are served straight from `/media/`; documents, uploads and sealed PDFs
always go through Paperlane's own permission-checked views.

The rest of this page is the manual setup, for servers where Docker isn't an option.

## 1. Server and code

```bash
git clone <this repo> paperlane && cd paperlane
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt gunicorn
```

## 2. Configuration

Copy `.env.example` to `.env` and set at least:

| Variable | Value |
|---|---|
| `PAPERLANE_SECRET_KEY` | A long random string, e.g. `python -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `PAPERLANE_DEBUG` | `0` |
| `PAPERLANE_ALLOWED_HOSTS` | Your domain, e.g. `docs.example.com` |
| `PAPERLANE_SITE_URL` | `https://docs.example.com` (used in every email link) |
| `PAPERLANE_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` |
| `PAPERLANE_EMAIL_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_TLS` | Your SMTP server |
| `PAPERLANE_FROM_EMAIL` | `Your Company <no-reply@example.com>` |
| `PAPERLANE_HTTPS` | `1` behind TLS: secure cookies, HSTS, HTTPS redirect, the proxy header, and trusted CSRF origins from your allowed hosts |

The **Super admin › System & health** page lists Django's deployment checks
(`manage.py check --deploy`) so you can confirm each item.

## 3. Database, static files and the first super admin

```bash
set -a; source .env; set +a
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
```

Sign in, open the shield icon (**Super admin**) and create the first workspace for its owner.

## 4. Processes

Run the web app with gunicorn behind nginx or another reverse proxy that terminates TLS and serves `/static/`
and `/media/`:

```bash
.venv/bin/gunicorn paperlane.wsgi --bind 127.0.0.1:8000 --workers 3
```

Run background jobs (reminders, deadlines, delayed bots, scheduled starts and webhook retries) once, as a
separate process, with `PAPERLANE_SCHEDULER=0` set for the web workers so they don't also run the in-process
scheduler:

```bash
.venv/bin/python manage.py paperlane_tick --loop 30
```

The worker checks the job queue every 3 seconds (`--jobs-every`) and runs the full scheduler every 30. Bots marked
**Run in the background** (Bot settings › Options) and delayed bots are queued there, so slow AI models and connectors
never make a recipient wait; a failed run is retried after 1, 5 and 15 minutes. Several workers can run at once on
PostgreSQL (jobs are claimed with `SKIP LOCKED`).

The scheduler's heartbeat, the queue and retrying jobs are shown on **Super admin › Background jobs** and on the
Overview health checks.

## 5. AI (optional)

Install Ollama on the server (or another machine on the network) and pull a model, e.g. `ollama pull qwen3.5:9b`.
Each workspace can point to its own Ollama address in **My workspace › AI & models**, and can add its own
Anthropic, NVIDIA, OpenAI, or custom OpenAI-compatible key on that page. `ANTHROPIC_API_KEY` and
`PAPERLANE_OPENAI_BASE_URL` in `.env.example` still apply when a workspace has not saved that provider.

## 6. Backups

`python manage.py backup` writes a timestamped folder to `PAPERLANE_BACKUP_DIR` (default `./backups`) with:

- `db.sqlite3` — a consistent online copy (safe while Paperlane runs), or `db.dump` from `pg_dump` on PostgreSQL;
- `media.tar.gz` — uploaded files, sealed PDFs and bot storage;
- `manifest.json` — SHA-256 of each file.

It keeps the newest `PAPERLANE_BACKUP_KEEP` (default 14). With Docker Compose the worker runs it every night at
`PAPERLANE_BACKUP_AT` (02:30). Otherwise schedule it with cron, e.g. `30 2 * * * cd /srv/paperlane && .venv/bin/python manage.py backup`.
**Copy backups off the server** (object storage, another host) — a backup on the same disk doesn't survive losing it.
`/healthz/` reports the time of the last backup.

**Restore** (stop web and worker first):

```bash
# SQLite
cp backups/paperlane-YYYYMMDD-HHMMSS/db.sqlite3 db.sqlite3
# PostgreSQL
pg_restore --clean --no-owner -d "$PAPERLANE_DATABASE_URL" backups/paperlane-YYYYMMDD-HHMMSS/db.dump
# uploaded files
tar -xzf backups/paperlane-YYYYMMDD-HHMMSS/media.tar.gz -C .
```

Test a restore on a spare machine now and then — then check a few documents at `/verify/`.

## 7. Docker Compose

```bash
cp .env.example .env      # set PAPERLANE_SECRET_KEY, PAPERLANE_ALLOWED_HOSTS, PAPERLANE_SITE_URL, POSTGRES_PASSWORD, email
docker compose --profile https up -d --build   # or without --profile https behind your own proxy
docker compose exec web python manage.py createsuperuser
```

Services: `db` (PostgreSQL 17), `web` (gunicorn, runs migrations on start), `worker` (background jobs + nightly backup).
Volumes keep the database, uploads and backups. Put a TLS proxy in front of port 8000 and set `PAPERLANE_HTTPS=1`.

## 8. Monitoring

`GET /healthz/` returns `200 {"status": "ok"}` with checks for the database, the shared cache, the jobs heartbeat
and the last backup (`503` when the database or cache fails). Point your uptime monitor or load balancer at it.
Logs go to stdout/stderr (`PAPERLANE_LOG_LEVEL`).

### Uploaded files on S3-compatible storage

Set `PAPERLANE_S3_BUCKET` (plus `PAPERLANE_S3_ENDPOINT`, `PAPERLANE_S3_REGION`, `PAPERLANE_S3_ACCESS_KEY`,
`PAPERLANE_S3_SECRET_KEY`; see `.env.example`) to keep uploads, PDFs and bot storage in AWS S3, Cloudflare R2,
Backblaze B2, MinIO or DigitalOcean Spaces instead of the media volume. Files stay private: Paperlane hands out signed
links that expire after `PAPERLANE_S3_LINK_SECONDS`. `manage.py backup` then backs up the database only; protect the
bucket with its own versioning or replication. Copy existing files across once with your provider's sync tool
(e.g. `aws s3 sync media/ s3://bucket/`).

### Word templates to PDF

The Word-template bot always saves the filled .docx; it also saves a PDF when LibreOffice is installed (`soffice` on PATH,
or `PAPERLANE_SOFFICE=/path/to/soffice`). In Docker, add `libreoffice-writer` to the image if you need PDFs.

### Google Drive and Microsoft 365

Register an OAuth app with Google (scope `drive.file`) and/or Microsoft Entra (delegated `Files.ReadWrite.All`,
`offline_access`) with the redirect URI `<PAPERLANE_SITE_URL>/account/connections/oauth/google/callback/` (or `…/microsoft/…`),
then set `PAPERLANE_GOOGLE_CLIENT_ID/SECRET` and `PAPERLANE_MS_CLIENT_ID/SECRET/TENANT`. Users then connect under
Account › Connections.

### Error reporting

Set `PAPERLANE_SENTRY_DSN` to send unhandled errors to Sentry (or a compatible service such as GlitchTip). No personal
data is sent; `PAPERLANE_SENTRY_TRACES=0.1` adds performance traces for 10% of requests.

## 9. Shared cache

With more than one process (gunicorn workers, the jobs worker) the cache must be shared — login throttling, the
verify rate limit and the jobs heartbeat use it. When `PAPERLANE_DEBUG=0` Paperlane uses a database cache by default;
create its table once with `python manage.py createcachetable` (the Docker entrypoint does this).
