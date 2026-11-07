"""Back up the database and uploaded files into one timestamped folder, and keep the newest N.

SQLite: a consistent online copy (sqlite3 backup API, safe while the app runs).
PostgreSQL: `pg_dump --format=custom` (pg_dump must be installed). Uploaded files: media.tar.gz.
Restore: see docs/DEPLOYMENT.md › Backups."""
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Command(BaseCommand):
    help = "Back up the database and uploaded files (rotates old backups)."

    def add_arguments(self, parser):
        parser.add_argument("--dir", help="Backup folder (default PAPERLANE_BACKUP_DIR or ./backups)")
        parser.add_argument("--keep", type=int, help="How many backups to keep (default PAPERLANE_BACKUP_KEEP or 14)")
        parser.add_argument("--no-media", action="store_true", help="Database only")

    def handle(self, *args, **o):
        root = Path(o.get("dir") or settings.BACKUP_DIR)
        keep = o.get("keep") or settings.BACKUP_KEEP
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = root / f"paperlane-{stamp}"
        target.mkdir(parents=True, exist_ok=False)
        db = settings.DATABASES["default"]
        manifest = {"created": stamp, "engine": db["ENGINE"], "files": {}}
        if db["ENGINE"].endswith("sqlite3"):
            out = target / "db.sqlite3"
            from django.db import connection

            connection.ensure_connection()  # Django's own connection: same settings (WAL, URI test databases), no
            dst = sqlite3.connect(str(out))  # second connection waiting on this process's own transaction
            with dst:
                connection.connection.backup(dst)
            dst.close()
        elif db["ENGINE"].endswith("postgresql"):
            if not shutil.which("pg_dump"):
                raise CommandError("pg_dump is not installed — install the PostgreSQL client tools.")
            out = target / "db.dump"
            env = {**os.environ, "PGPASSWORD": db.get("PASSWORD", "")}
            subprocess.run(["pg_dump", "--format=custom", "--no-owner", "-h", db.get("HOST") or "localhost", "-p", str(db.get("PORT") or 5432),
                            "-U", db.get("USER", ""), "-f", str(out), db["NAME"]], check=True, env=env)
        else:
            raise CommandError(f"Unsupported database engine {db['ENGINE']}")
        manifest["files"][out.name] = _sha256(out)
        media = Path(settings.MEDIA_ROOT)
        on_s3 = "s3" in settings.STORAGES["default"]["BACKEND"].lower()
        if on_s3 and not o["no_media"]:
            self.stdout.write("Uploaded files are on S3-compatible storage: use the bucket's versioning or replication to back them up.")
        if not o["no_media"] and not on_s3 and media.exists():
            arc = target / "media.tar.gz"
            with tarfile.open(arc, "w:gz") as tar:
                tar.add(media, arcname="media")
            manifest["files"][arc.name] = _sha256(arc)
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
        old = sorted(p for p in root.glob("paperlane-*") if p.is_dir())[:-keep]
        for p in old:
            shutil.rmtree(p)
        size = sum(f.stat().st_size for f in target.iterdir())
        self.stdout.write(self.style.SUCCESS(f"Backup written to {target} ({size / 1e6:.1f} MB); removed {len(old)} old backup(s)."))
        from django.core.cache import cache

        cache.set("last-backup", {"at": stamp, "path": str(target), "bytes": size}, None)
