#!/usr/bin/env python3
"""Offline failure checks for explicit artifact deployment compatibility paths."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]
LEGACY = ("CTID", "EXPECTED_CT_HOSTNAME", "API_DIR", "CONSOLE_DIR", "WEB_ROOT", "RELEASES_DIR",
          "HEALTH_TICKS", "LLMGW_DIR", "RP", "GO", "REMOTE_BIN", "REMOTE_KEYGEN", "REMOTE_UNIT", "KEEP", "VITE_VM_NETWORK_POLICY_ENABLED", "VITE_PUBLIC_SOURCE_POLICY_ENABLED")


class DeployCompatibilityPathsTest(unittest.TestCase):
    def run_adapter(self, service, arguments=(), overrides=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            trace = root / "trace"
            binaries = root / "bin"
            binaries.mkdir()
            for name in ("pct", "mvn", "npm", "systemctl"):
                path = binaries / name
                path.write_text('#!/usr/bin/env bash\nprintf "unexpected:%s\\n" "$0" >> "$TRACE"\nexit 99\n')
                path.chmod(0o755)
            python = binaries / "python3"
            python.write_text(f'#!{sys.executable}\nimport json,os,sys\nopen(os.environ["TRACE"],"w").write(json.dumps(sys.argv[1:]))\nsys.exit(int(os.environ.get("DELEGATE_EXIT","0")))\n')
            python.chmod(0o755)
            environment = os.environ.copy()
            for key in LEGACY:
                environment.pop(key, None)
            environment.update(PATH=f'{binaries}:{environment["PATH"]}', TRACE=str(trace))
            environment.update(overrides or {})
            result = subprocess.run(["bash", str(SCRIPTS / f"deploy-{service}.sh"), *arguments],
                                    env=environment, text=True, capture_output=True, check=False)
            return result, trace.read_text() if trace.exists() else None

    def test_old_default_invocation_refuses_before_build_or_target_lookup(self):
        for service in ("api", "console", "llm-gateway"):
            result, trace = self.run_adapter(service)
            self.assertEqual(result.returncode, 2)
            self.assertIn("explicit", (SCRIPTS / f"deploy-{service}.sh").read_text())
            self.assertIsNone(trace)

    def test_source_ct_and_checkout_overrides_cannot_select_a_target(self):
        arguments = ("apply", "dev", "a"*40, "/root/pickle-deploy-staging/artifact", "b"*64)
        for service in ("api", "console", "llm-gateway"):
            for overrides in ({"CTID": "904"}, {"CTID": "905"}, {"CTID": "911"},
                              {"EXPECTED_CT_HOSTNAME": "example-source"}, {"API_DIR": "/source/api"},
                              {"CONSOLE_DIR": "/source/console"}, {"LLMGW_DIR": "/source/gateway"}, {"RP": "900"}, {"VITE_VM_NETWORK_POLICY_ENABLED": "1"}):
                result, trace = self.run_adapter(service, arguments, overrides)
                self.assertEqual(result.returncode, 2)
                self.assertIn("legacy", result.stderr)
                self.assertIsNone(trace)

    def test_explicit_check_preserves_arguments_and_delegates_only_to_protected_tool(self):
        artifact = "/root/pickle-deploy-staging/a bundle; $(touch unexpected).tar.gz"
        for service in ("api", "console", "llm-gateway"):
            arguments = ("check", "dev", "a"*40, artifact, "b"*64)
            result, trace = self.run_adapter(service, arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(trace), [str(SCRIPTS / "deploy-environment.py"), "check", "dev",
                                               service, "a"*40, artifact, "b"*64])

    def test_production_apply_keeps_explicit_action_and_propagates_guard_refusal(self):
        arguments = ("apply", "prod", "a"*40, "/root/pickle-deploy-staging/artifact", "b"*64, "--backup-check")
        for service in ("api", "console", "llm-gateway"):
            result, trace = self.run_adapter(service, arguments, {"DELEGATE_EXIT": "17"})
            self.assertEqual(result.returncode, 17)
            self.assertEqual(json.loads(trace)[1:4], ["apply", "prod", service])

    def test_ambiguous_action_or_environment_refuses_without_delegation(self):
        for arguments in (("dev", "check", "a"*40, "artifact", "b"*64),
                          ("apply", "source", "a"*40, "artifact", "b"*64)):
            result, trace = self.run_adapter("api", arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIsNone(trace)


if __name__ == "__main__":
    unittest.main()
