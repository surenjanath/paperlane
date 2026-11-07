"""Operations: backup command and the health endpoint."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings


class OpsTests(TestCase):
    def test_healthz(self):
        r = self.client.get("/healthz/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["checks"]["database"], "ok")


class BackupTests(TransactionTestCase):
    # The command runs outside any transaction in real use (cron, the worker). Inside TestCase's wrapping transaction
    # the connection holds SQLite's write lock (IMMEDIATE mode) and the online backup waits on it forever.
    def test_backup_writes_db_media_manifest_and_rotates(self):
        engine = settings.DATABASES["default"]["ENGINE"]
        if "postgresql" in engine and not shutil.which("pg_dump"):
            raise unittest.SkipTest("pg_dump is not installed")
        db_file = "db.sqlite3" if "sqlite" in engine else "db.dump"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as media:
            (Path(media) / "a.txt").write_text("hello")
            with override_settings(MEDIA_ROOT=media):
                for _ in range(3):
                    call_command("backup", dir=tmp, keep=2, stdout=StringIO())
                    import time
                    time.sleep(1.05)  # folders are named per second
            dirs = sorted(Path(tmp).glob("paperlane-*"))
            self.assertEqual(len(dirs), 2)  # rotated
            last = dirs[-1]
            manifest = json.loads((last / "manifest.json").read_text())
            self.assertIn(db_file, manifest["files"])
            self.assertIn("media.tar.gz", manifest["files"])
            if db_file == "db.sqlite3":
                con = sqlite3.connect(last / "db.sqlite3")  # a usable database
                self.assertTrue(con.execute("select count(*) from sqlite_master").fetchone()[0] > 10)
                con.close()
            else:
                self.assertGreater((last / "db.dump").stat().st_size, 1000)
