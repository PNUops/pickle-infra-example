#!/usr/bin/env python3
"""Verify a fresh encrypted production DB point through the independent reader."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import shutil
import stat
import subprocess
import sys
import tempfile
import time


class VerificationError(RuntimeError):
    pass


def protected(path, expected_sha=None):
    path = Path(path)
    metadata = path.lstat()
    if path.is_symlink() or not path.is_file() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise VerificationError("verification input must be root-owned and protected")
    data = path.read_bytes()
    if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
        raise VerificationError("verification input SHA-256 differs from its pin")
    return data


def load_native(config):
    folder = Path(config["native_library_dir"])
    for selected in (folder, *folder.parents):
        metadata = selected.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise VerificationError("native library ancestor directories are not protected")
    sys.dont_write_bytecode = True
    for name in ("db_pbs_backup.py", "isolated_core.py"):
        protected(folder / name, config["native_library_sha256"][name])
    metadata = folder.lstat()
    if folder.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise VerificationError("native library directory is not protected")
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location("release_native_db_pbs", folder / "db_pbs_backup.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def flyway_history(sql):
    lines = sql.splitlines()
    header = next((line for line in lines if line.startswith("COPY public.flyway_schema_history ")
                   and line.endswith(" FROM stdin;")), None)
    if not header:
        raise VerificationError("recovered dump does not expose Flyway COPY data")
    columns = header.partition("(")[2].partition(")")[0].split(", ")
    required = {"installed_rank", "version", "success"}
    if not required <= set(columns):
        raise VerificationError("recovered Flyway columns differ from expected schema")
    versions = []
    start = lines.index(header) + 1
    rows = 0
    for line in lines[start:]:
        if line == r"\.":
            break
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise VerificationError("recovered Flyway row is malformed")
        row = dict(zip(columns, cells))
        if row["success"] != "t":
            raise VerificationError("recovered dump contains a failed Flyway migration")
        rows += 1
        if row["version"] != r"\N":
            if not row["version"].isdecimal():
                raise VerificationError("recovered Flyway version is not numeric")
            versions.append(int(row["version"]))
    else:
        raise VerificationError("recovered Flyway COPY is incomplete")
    if not versions or rows < 1:
        raise VerificationError("recovered Flyway history is empty")
    return {"schema": max(versions), "history_rows": rows, "failed_migrations": 0}


def postgres_read(config, dump, options):
    command = config["postgres_restore_command"]
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise VerificationError("a reviewed native PostgreSQL reader command is required")
    if command[-1] != "/usr/lib/postgresql/18/bin/pg_restore":
        raise VerificationError("PostgreSQL reader must select the native version 18 tool")
    with dump.open("rb") as stream:
        result = subprocess.run([*command, *options], stdin=stream, capture_output=True,
                                timeout=120, check=False)
    if result.returncode:
        raise VerificationError("native PostgreSQL archive read failed")
    return result.stdout.decode()


def verify_payload(module, config, backup, client, record, epoch):
    with tempfile.TemporaryDirectory(prefix="release-db-readback-", dir=config["temporary_root"]) as folder:
        restored = Path(folder) / "payload"
        client.restore(record["snapshot"], restored)
        names = {entry.name for entry in restored.iterdir()}
        if names != {"database.dump", "roles.sql", "manifest.json"}:
            raise VerificationError("recovered DB payload inventory differs from the paired receipt")
        for name, expected in record["payload_sha256"].items():
            path = restored / name
            if path.is_symlink() or not path.is_file() or module.digest(path) != expected:
                raise VerificationError("recovered payload SHA-256 differs from its paired receipt")
        manifest = json.loads((restored / "manifest.json").read_text())
        source = manifest.get("source", {})
        if (manifest.get("instance_id") != backup.instance_id
                or manifest.get("recovery_point_epoch") != epoch
                or source.get("database") != backup.database
                or source.get("system_identifier") != backup.expected_system_identifier
                or source.get("recovery") is not False or source.get("failed_migrations") != 0
                or source.get("schema") != config["expected_schema_version"]):
            raise VerificationError("recovered DB manifest identity or schema differs from the production pin")
        toc = postgres_read(config, restored / "database.dump", ["--list"])
        if "TABLE DATA public flyway_schema_history " not in toc:
            raise VerificationError("native PostgreSQL TOC lacks Flyway table data")
        history = flyway_history(postgres_read(config, restored / "database.dump",
                                  ["--data-only", "--schema=public", "--table=flyway_schema_history", "--file=-"]))
        if history["schema"] != config["expected_schema_version"]:
            raise VerificationError("recovered Flyway history differs from the current production schema")
        return history


def verify(config, challenge):
    if socket.gethostname().split(".")[0] != config["reader_hostname"]:
        raise VerificationError("verification must run on the selected independent reader")
    module = load_native(config)
    protected(config["reader_profile"], config["reader_profile_sha256"])
    backup = module.Config.load(Path(config["reader_profile"]))
    module.require_local_host(backup, monitor=True)
    if (backup.database != config["database"]
            or backup.instance_id != config["instance_id"]
            or backup.expected_system_identifier != config["expected_system_identifier"]
            or backup.namespace != config["namespace"] or backup.backup_id != config["backup_id"]):
        raise VerificationError("reader profile does not select the pinned production DB group")
    root = Path(config["temporary_root"])
    metadata = root.lstat()
    if root.is_symlink() or not root.is_dir() or metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise VerificationError("private readback temporary root must already be prepared")
    client = module.Client(backup, module.Runner())
    proofs = client.snapshots(proofs=True)
    data = client.snapshots()
    if not proofs or proofs[0] not in data:
        raise VerificationError("latest encrypted DB point has no complete proof/data pair")
    epoch = proofs[0]
    if not 0 <= time.time() - epoch <= config["maximum_point_age_seconds"]:
        raise VerificationError("latest paired DB point is stale or in the future")
    proof_snapshot = module.snapshot_name(backup, epoch, proofs=True)
    module.manifest_fingerprint(backup, client.index(proof_snapshot), epoch, proofs=True)
    record = client.receipt(proof_snapshot)
    module.validate_receipt(backup, record, epoch)
    if record["verified_epoch"] > int(time.time()):
        raise VerificationError("paired receipt verification time is in the future")
    if record["system_identifier"] != backup.expected_system_identifier:
        raise VerificationError("paired receipt belongs to a different PostgreSQL instance")
    data_index = client.index(record["snapshot"])
    fingerprint = module.manifest_fingerprint(backup, data_index, epoch)
    archive = next(entry for entry in data_index["files"] if entry["filename"] == module.ARCHIVE_INDEX)
    size = archive.get("size")
    if type(size) is not int or size < 1 or shutil.disk_usage(root).free < max(size * 2, 1024**3):
        raise VerificationError("independent reader lacks protected file-recovery disk headroom")
    if fingerprint != record["manifest_fingerprint"]:
        raise VerificationError("current encrypted PBS data manifest differs from the paired receipt")
    history = verify_payload(module, config, backup, client, record, epoch)
    if module.manifest_fingerprint(backup, client.index(record["snapshot"]), epoch) != fingerprint:
        raise VerificationError("PBS manifest changed during independent file recovery")
    if not 0 <= time.time() - epoch <= config["maximum_point_age_seconds"]:
        raise VerificationError("point exceeded its release freshness limit during readback")
    return {"status": "VERIFIED", "challenge": challenge, "reader_hostname": config["reader_hostname"],
            "database": backup.database, "system_identifier": backup.expected_system_identifier,
            "instance_id": backup.instance_id, "snapshot": record["snapshot"],
            "manifest_fingerprint": fingerprint, "payload_sha256": record["payload_sha256"],
            "recovery_point_epoch": epoch, "verified_at": int(time.time()),
            "scope": "independent encrypted file recovery, native TOC and recovered Flyway data",
            "full_sql_restore": False, **history,
            "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--challenge", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{64}", args.challenge):
        raise VerificationError("root and a fresh release-check challenge are required")
    config = json.loads(protected(args.config))
    print(json.dumps(verify(config, args.challenge), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"production backup check refused: {exc}", file=sys.stderr)
        sys.exit(1)
