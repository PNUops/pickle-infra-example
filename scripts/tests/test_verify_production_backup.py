"""Failure paths for independent encrypted DB file-recovery verification."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "verify-production-backup.py"
SPEC = importlib.util.spec_from_file_location("verify_backup", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

HISTORY = """COPY public.flyway_schema_history (installed_rank, version, description, type, script, checksum, installed_by, installed_on, execution_time, success) FROM stdin;
1\t136\texample\tSQL\tV136__example.sql\t123\tpostgres\t2026-10-01\t1\tt
2\t137\texample\tSQL\tV137__example.sql\t456\tpostgres\t2026-10-01\t1\tt
\\.
"""


class VerifyProductionBackupTest(unittest.TestCase):
    def test_flyway_readback_rejects_failed_missing_and_truncated_copy(self):
        self.assertEqual(verify.flyway_history(HISTORY), {"schema": 137, "history_rows": 2, "failed_migrations": 0})
        for invalid in (HISTORY.replace("\t1\tt", "\t1\tf"), "select 1;", HISTORY.replace("\\.\n", ""),
                        HISTORY.replace("\t137\t", "\tbad\t")):
            with self.assertRaises(verify.VerificationError):
                verify.flyway_history(invalid)

    def test_payload_readback_requires_three_exact_files_hashes_and_current_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            source = {"database": "pickle_prod", "system_identifier": "123456789", "recovery": False,
                      "failed_migrations": 0, "schema": 137}
            payload = {"database.dump": b"example custom archive", "roles.sql": b"CREATE ROLE example_role;",
                       "manifest.json": json.dumps({"instance_id": "example-instance", "recovery_point_epoch": 123,
                                                    "source": source}).encode()}
            record = {"snapshot": "host/example-production/2026-10-01T00:00:00Z",
                      "payload_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in payload.items()}}
            backup = SimpleNamespace(instance_id="example-instance", database="pickle_prod", expected_system_identifier="123456789")
            config = {"temporary_root": folder, "expected_schema_version": 137}
            module = SimpleNamespace(digest=lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
            mutation = {"kind": None}
            def restore(_snapshot, target):
                target.mkdir()
                for name, value in payload.items():
                    (target / name).write_bytes(value)
                if mutation["kind"] == "hash":
                    (target / "database.dump").write_bytes(b"different archive")
                elif mutation["kind"] == "extra":
                    (target / "extra").write_text("unexpected")
                elif mutation["kind"] == "link":
                    (target / "roles.sql").unlink()
                    (target / "roles.sql").symlink_to(target / "database.dump")
            client = SimpleNamespace(restore=restore)
            with patch.object(verify, "postgres_read", side_effect=lambda _c, _p, options: HISTORY if "--data-only" in options else "TABLE DATA public flyway_schema_history example_role"):
                self.assertEqual(verify.verify_payload(module, config, backup, client, record, 123)["schema"], 137)
                for kind in ("hash", "extra", "link"):
                    mutation["kind"] = kind
                    with self.assertRaises(verify.VerificationError):
                        verify.verify_payload(module, config, backup, client, record, 123)
                mutation["kind"] = None
                config["expected_schema_version"] = 139
                with self.assertRaisesRegex(verify.VerificationError, "manifest identity or schema"):
                    verify.verify_payload(module, config, backup, client, record, 123)

    def test_native_postgres_reader_failure_and_wrong_tool_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            dump = Path(folder) / "database.dump"
            dump.write_bytes(b"archive")
            with patch.object(verify.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=b"")):
                with self.assertRaisesRegex(verify.VerificationError, "archive read failed"):
                    verify.postgres_read({"postgres_restore_command": ["/usr/lib/postgresql/18/bin/pg_restore"]}, dump, ["--list"])
            with self.assertRaisesRegex(verify.VerificationError, "native version 18"):
                verify.postgres_read({"postgres_restore_command": ["echo"]}, dump, ["--list"])

    def test_independent_reader_refuses_missing_pair_stale_point_and_wrong_pg_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            backup = SimpleNamespace(database="pickle_prod", instance_id="example-instance",
                                     expected_system_identifier="123456789", namespace="example-platform", backup_id="example-production")
            record = {"snapshot": "host/example-production/2026-10-01T00:00:00Z", "verified_epoch": 990,
                      "system_identifier": "123456789", "manifest_fingerprint": "f"*64,
                      "payload_sha256": {"database.dump": "a"*64, "roles.sql": "b"*64, "manifest.json": "c"*64}}
            client = Mock()
            client.snapshots.side_effect = lambda proofs=False: [980]
            client.receipt.return_value = record
            client.index.return_value = {"files": [{"filename": "platform-db.pxar.didx", "size": 1024}]}
            module = SimpleNamespace(Config=SimpleNamespace(load=lambda _path: backup),
                                     require_local_host=lambda *args, **kwargs: None,
                                     Client=lambda *args: client, Runner=lambda: None,
                                     snapshot_name=lambda *args, **kwargs: "snapshot",
                                     manifest_fingerprint=lambda *args, **kwargs: "f"*64,
                                     validate_receipt=lambda *args: None, ARCHIVE_INDEX="platform-db.pxar.didx")
            config = {"reader_hostname": "example-reader", "reader_profile": "/example/profile.json",
                      "reader_profile_sha256": "d"*64, "database": "pickle_prod", "instance_id": "example-instance",
                      "expected_system_identifier": "123456789", "namespace": "example-platform",
                      "backup_id": "example-production", "temporary_root": folder, "maximum_point_age_seconds": 600}
            with patch.object(verify.socket, "gethostname", return_value="example-reader"), \
                 patch.object(verify, "load_native", return_value=module), patch.object(verify, "protected"), \
                 patch.object(verify.Path, "lstat", return_value=SimpleNamespace(st_uid=0, st_mode=0o40700)), \
                 patch.object(verify.time, "time", return_value=1000), \
                 patch.object(verify, "verify_payload", return_value={"schema": 137, "history_rows": 2, "failed_migrations": 0}) as payload:
                self.assertEqual(verify.verify(config, "a"*64)["status"], "VERIFIED")
                client.snapshots.side_effect = lambda proofs=False: [980] if proofs else []
                with self.assertRaisesRegex(verify.VerificationError, "complete proof/data pair"):
                    verify.verify(config, "a"*64)
                client.snapshots.side_effect = lambda proofs=False: [1]
                with self.assertRaisesRegex(verify.VerificationError, "stale"):
                    verify.verify(config, "a"*64)
                client.snapshots.side_effect = lambda proofs=False: [980]
                record["system_identifier"] = "999"
                with self.assertRaisesRegex(verify.VerificationError, "different PostgreSQL"):
                    verify.verify(config, "a"*64)
                self.assertEqual(payload.call_count, 1)


if __name__ == "__main__":
    unittest.main()
