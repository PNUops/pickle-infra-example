#!/usr/bin/env python3
"""Exercise the vzdump guard's allowed path and fail-closed boundaries."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "candidate-core-vzdump-hook.sh"
VMIDS = (1200, 1201, 1202, 1204)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / "lxc"
        config.mkdir()
        script = SOURCE.read_text()
        script = script.replace("/etc/pve/lxc/", str(config) + "/")
        for vmid in VMIDS:
            payload = f"hostname: test-{vmid}\n".encode()
            (config / f"{vmid}.conf").write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            line = next(line for line in script.splitlines() if line.strip().startswith(f"{vmid}) expected="))
            script = script.replace(line, f"    {vmid}) expected={digest} ;;")
        self.script = self.root / "hook.sh"
        self.script.write_text(script)
        for command, body in {
            "hostname": "echo \"${TEST_NODE:-pve-node-2}\"",
            "pvecm": "printf 'Cluster information\\nQuorate:          %s\\n' \"${TEST_QUORATE:-Yes}\"",
            "pct": "if [ \"${TEST_BAD_CT:-}\" = \"$2\" ]; then echo 'status: stopped'; else echo 'status: running'; fi",
        }.items():
            stub = self.root / command
            stub.write_text("#!/bin/sh\n" + body + "\n")
            stub.chmod(0o755)
        stub = self.root / "sha256sum"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import hashlib, sys\n"
            "path = sys.argv[-1]\n"
            "print(hashlib.sha256(open(path, 'rb').read()).hexdigest(), path)\n"
        )
        stub.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.root}:/usr/bin:/bin", STOREID="pbs-example-core-write", VMTYPE="lxc")

    def run_hook(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args], env=self.env | env,
                              capture_output=True, text=True, check=False)

    def test_expected_job_and_ct_pass(self):
        result = self.run_hook("job-init")
        self.assertEqual(result.returncode, 0, result.stderr)
        for vmid in VMIDS:
            config = self.root / f"lxc/{vmid}.conf"
            original = config.read_bytes()
            config.write_bytes(original + b"lock: backup\n")
            result = self.run_hook("backup-start", "snapshot", str(vmid))
            self.assertEqual(result.returncode, 0, result.stderr)
            config.write_bytes(original)

    def test_wrong_node_storage_quorum_and_ct_state_fail(self):
        for change in ({"TEST_NODE": "pve-node-3"}, {"STOREID": "pbs-example-core-read"},
                       {"TEST_QUORATE": "No"}, {"TEST_BAD_CT": "1202"}, {"DUMPDIR": "/tmp"}):
            with self.subTest(change=change):
                self.assertNotEqual(self.run_hook("job-init", **change).returncode, 0)

    def test_config_drift_and_wrong_backup_identity_fail(self):
        (self.root / "lxc/1201.conf").write_text("hostname: changed\n")
        self.assertNotEqual(self.run_hook("job-init").returncode, 0)
        (self.root / "lxc/1201.conf").write_text("hostname: test-1201\n")
        for args, env in ((('backup-start', 'stop', '1200'), {}),
                          (('backup-start', 'snapshot', '1203'), {}),
                          (('backup-start', 'snapshot', '1200'), {'VMTYPE': 'qemu'})):
            with self.subTest(args=args, env=env):
                self.assertNotEqual(self.run_hook(*args, **env).returncode, 0)

    def test_backup_lock_is_exact_and_only_on_selected_ct(self):
        selected = self.root / "lxc/1200.conf"
        original = selected.read_bytes()
        for suffix in (b"", b"lock: snapshot\n", b"lock: backup\nlock: backup\n",
                       b"lock: backup\nhostname: drift\n"):
            with self.subTest(suffix=suffix):
                selected.write_bytes(original + suffix)
                self.assertNotEqual(self.run_hook("backup-start", "snapshot", "1200").returncode, 0)
        selected.write_bytes(original + b"lock: backup\n")
        other = self.root / "lxc/1201.conf"
        other.write_bytes(other.read_bytes() + b"lock: backup\n")
        self.assertNotEqual(self.run_hook("backup-start", "snapshot", "1200").returncode, 0)


if __name__ == "__main__":
    unittest.main()
