import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("storage", Path(__file__).resolve().parents[1] / "check-backup-storage.py")
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageCollectionTest(unittest.TestCase):
    @patch.object(storage.os, "ST_NOEXEC", 8, create=True)
    def test_noexec_filesystem_is_refused_without_remounting(self):
        metadata = SimpleNamespace(st_mode=0o40700, st_uid=0)
        with patch.object(storage.Path, "lstat", return_value=metadata), patch.object(storage.os, "statvfs", return_value=SimpleNamespace(f_flag=storage.os.ST_NOEXEC)):
            with self.assertRaisesRegex(ValueError, "does not permit"):
                storage.execution_parent()

    @patch.object(storage.os, "ST_NOEXEC", 8, create=True)
    def test_execution_parent_must_be_root_private_directory(self):
        for mode, uid in ((0o40755, 0), (0o40700, 1000), (0o120700, 0)):
            with patch.object(storage.Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=uid)):
                with self.assertRaises(ValueError):
                    storage.execution_parent()
        with patch.object(storage.Path, "lstat", return_value=SimpleNamespace(st_mode=0o40700, st_uid=0)), patch.object(storage.os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
            self.assertEqual(storage.execution_parent(), Path("/root"))

    def test_spawn_error_is_incomplete_but_device_exit_code_is_not_health_verdict(self):
        result = {"smart_version": {"exit_code": 0}, "raid": [{"exit_code": 255}], "smart": [{"exit_code": 8}]}
        self.assertTrue(storage.collection_complete(result))
        result["raid"][0] = {"error": "Permission denied"}
        self.assertFalse(storage.collection_complete(result))
        result["raid"][0] = {"timed_out": True}
        self.assertFalse(storage.collection_complete(result))
        result["raid"][0] = {"exit_code": -11}
        self.assertFalse(storage.collection_complete(result))
        result["raid"][0] = {"exit_code": 255}
        result["smart_version"] = {"exit_code": 127}
        self.assertFalse(storage.collection_complete(result))

    def test_tool_is_copied_and_hash_checked_before_execution(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "input", Path(root) / "copy"
            source.write_bytes(b"\x7fELFfixture")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            storage.snapshot(str(source), digest, target)
            source.write_bytes(b"replaced")
            self.assertEqual(target.read_bytes(), b"\x7fELFfixture")
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(ValueError):
                storage.snapshot(str(source), digest, Path(root) / "second")
            self.assertFalse((Path(root) / "second").exists())

    def test_symlink_and_script_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "input", Path(root) / "copy"
            source.write_bytes(b"#!/bin/sh\ntrue\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                storage.snapshot(str(source), digest, target)
            link = Path(root) / "link"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                storage.snapshot(str(link), digest, target)

    def test_collection_never_issues_a_mutating_command(self):
        def response(argv, directory):
            return {"exit_code": 0, "output": "fixture"}

        with patch.object(storage, "run", side_effect=response) as run, patch.object(storage.Path, "exists", return_value=True):
            result = storage.collect("smartctl", "perccli64", Path("/tmp"))
        for call in run.call_args_list:
            argv = call.args[0]
            if argv[0] == "perccli64":
                expected = ["show", "all", "noforeign", "J"] if argv[1] == "/call" else ["show", "all", "J"]
                self.assertEqual(argv[2:], expected)
            else:
                self.assertIn(argv[1], ("--version", "--all"))
                if argv[1] == "--all":
                    self.assertEqual(argv[2:5], ["--json", "--device", "nvme"])
                    self.assertIn(argv[5], ("/dev/nvme0n1", "/dev/nvme1n1"))
        self.assertEqual(result["health_assessment"], "REQUIRES_REVIEW")

    def test_diagnostic_environment_does_not_inherit_loader_overrides(self):
        with patch.object(storage.subprocess, "run") as run:
            run.return_value.stdout, run.return_value.stderr = "{}", ""
            run.return_value.returncode = 8
            result = storage.run(["/run/tool", "show"], Path("/run/private"))
        self.assertEqual(result["exit_code"], 8)
        self.assertEqual(set(run.call_args.kwargs["env"]), {"PATH", "LANG", "LC_ALL", "HOME"})

    def test_timeout_retains_partial_diagnostic_output(self):
        error = storage.subprocess.TimeoutExpired(["tool"], 90, output=b"partial", stderr="warning")
        with patch.object(storage.subprocess, "run", side_effect=error):
            result = storage.run(["tool", "show"], Path("/run/private"))
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["partial_output"], "partial")
        self.assertEqual(result["partial_stderr"], "warning")

    def test_missing_nvme_devices_remain_visible(self):
        with patch.object(storage, "run", return_value={}), patch.object(storage.Path, "exists", return_value=False):
            result = storage.collect("smartctl", "perccli64", Path("/tmp"))
        self.assertEqual(len(result["smart"]), 2)
        self.assertTrue(all("error" in entry for entry in result["smart"]))


if __name__ == "__main__":
    unittest.main()
