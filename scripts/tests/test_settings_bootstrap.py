#!/usr/bin/env python3
"""Exercise bootstrap reruns with a local SQL fixture and no host connection."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "gpu_unattached_review_hours": 12,
    "gpu_low_util_window_hours": 12,
    "gpu_low_util_threshold_percent": 5,
    "gpu_low_util_snooze_hours": 12,
    "gpu_lease_notice_hours": [24, 1],
}

# Only the transport is replaced. SQLite executes the INSERT/ON CONFLICT
# statements emitted by the real script after PostgreSQL casts are removed.
# This fixture does not validate PostgreSQL types or production connectivity.
PCT_FIXTURE = r'''
import os
import sqlite3
import sys

if sys.argv[1] == "config":
    print("hostname: pickle-app")
    sys.exit(0)
if sys.argv[1] != "exec":
    raise SystemExit("unexpected container operation")
sql = sys.stdin.read()
with sqlite3.connect(os.environ["SETTINGS_TEST_DATABASE"]) as connection:
    connection.create_function("now", 0, lambda: "bootstrap-time")
    connection.create_function("to_char", 2, lambda value, _: value)
    sql = sql.replace("::jsonb", "").replace("::text", "")
    sql = sql.replace(" at time zone 'Asia/Seoul'", "")
    sql = sql.replace("left(value, 48)", "substr(value, 1, 48)")
    if sql.lstrip().lower().startswith("select"):
        for row in connection.execute(sql):
            print("|".join(str(value) for value in row))
    else:
        connection.executescript(sql)
'''


class SettingsBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="settings-bootstrap-")
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.database = self.tmp / "settings.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("""create table settings (
                key text primary key, value text not null,
                description text not null, updated_at text not null
            )""")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        pct = self.bin / "pct"
        pct.write_text(f"#!{sys.executable}\n" + PCT_FIXTURE)
        pct.chmod(0o700)
        self.data = self.tmp / "data"
        self.data.mkdir()
        for filename in ("reserved-subdomains.txt", "profanity-subdomains.txt"):
            (self.data / filename).write_text("reserved-example\n")
        self.environment = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                                SETTINGS_TEST_DATABASE=str(self.database),
                                PICKLE_DATA_DIR=str(self.data), PICKLE_CONTACT_EMAIL="none",
                                PICKLE_ROOT_DOMAIN="example.test")

    def run_bootstrap(self):
        result = subprocess.run(["bash", str(ROOT / "scripts/apply-settings.sh")],
                                env=self.environment, text=True, capture_output=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def gpu_rows(self):
        with sqlite3.connect(self.database) as connection:
            return {key: (json.loads(value), description, updated)
                    for key, value, description, updated in connection.execute(
                        "select key, value, description, updated_at from settings where key like 'gpu_%'")}

    def test_first_run_inserts_review_settings_without_lease_defaults(self):
        self.run_bootstrap()
        rows = self.gpu_rows()
        self.assertEqual({key: row[0] for key, row in rows.items()}, EXPECTED)

    def test_rerun_preserves_operator_values_and_value_timestamps(self):
        self.run_bootstrap()
        changes = {key: (24 if isinstance(value, int) else [6, 1])
                   for key, value in EXPECTED.items()}
        with sqlite3.connect(self.database) as connection:
            for key, value in changes.items():
                connection.execute("update settings set value=?, description='stale', "
                                   "updated_at='operator-time' where key=?", (json.dumps(value), key))
        self.run_bootstrap()
        rows = self.gpu_rows()
        self.assertEqual({key: row[0] for key, row in rows.items()}, changes)
        for row in rows.values():
            self.assertNotEqual(row[1], "stale")
            self.assertEqual(row[2], "operator-time")


if __name__ == "__main__":
    if shutil.which("jq") is None:
        raise SystemExit("jq is required to exercise apply-settings.sh")
    unittest.main()
