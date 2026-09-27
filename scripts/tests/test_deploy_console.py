#!/usr/bin/env python3
"""Offline checks for console deployment flag isolation and the deploy gate."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy-console.sh'


class DeployConsoleTest(unittest.TestCase):
    def run_deploy(self, flags=None, fail_verify=False, hostname='pickle-app',
                   private_dist=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            console = root / 'console'
            commands = root / 'bin'
            console.mkdir()
            commands.mkdir()
            trace = root / 'trace'
            (console / 'scripts').mkdir()
            (console / 'dist').mkdir()
            (console / 'dist' / 'index.html').write_text('bundle')
            if private_dist:
                (console / 'dist' / 'index.html').chmod(0o600)
            (console / 'scripts' / 'verify.sh').write_text(
                '#!/usr/bin/env bash\n'
                'printf "verify:%s:%s:%s\\n" "${VITE_VM_NETWORK_POLICY_ENABLED-unset}" '
                '"${VITE_PUBLIC_SOURCE_POLICY_ENABLED-unset}" "$PICKLE_TEST_MAX_WORKERS" >> "$TRACE"\n'
                + ('exit 13\n' if fail_verify else 'exit 0\n'))
            (console / 'scripts' / 'verify.sh').chmod(0o755)
            (commands / 'npm').write_text(
                '#!/usr/bin/env bash\n'
                'printf "npm:%s:%s:%s\\n" "$*" '
                '"${VITE_VM_NETWORK_POLICY_ENABLED-unset}" '
                '"${VITE_PUBLIC_SOURCE_POLICY_ENABLED-unset}" >> "$TRACE"\n')
            (commands / 'pct').write_text(
                '#!/usr/bin/env bash\n'
                'printf "pct:%s\\n" "$*" >> "$TRACE"\n'
                'if [ "$1" = config ]; then printf "hostname: %s\\n" "$MOCK_CT_HOSTNAME"; fi\n')
            for name in ('tar', 'rm'):
                (commands / name).write_text(
                    '#!/usr/bin/env bash\n'
                    f'printf "{name}:%s\\n" "$*" >> "$TRACE"\n')
            for name in ('npm', 'pct', 'tar', 'rm'):
                (commands / name).chmod(0o755)
            env = os.environ.copy()
            for key in ('VITE_VM_NETWORK_POLICY_ENABLED', 'VITE_PUBLIC_SOURCE_POLICY_ENABLED',
                        'EXPECTED_CT_HOSTNAME'):
                env.pop(key, None)
            env.update({'PATH': f'{commands}:{env["PATH"]}', 'CONSOLE_DIR': str(console),
                        'TRACE': str(trace), 'CTID': '201',
                        'MOCK_CT_HOSTNAME': hostname})
            env.update(flags or {})
            result = subprocess.run(['bash', str(DEPLOY)], env=env, text=True,
                                    capture_output=True, check=False)
            lines = trace.read_text().splitlines() if trace.exists() else []
            if private_dist:
                mode = stat.S_IMODE((console / 'dist' / 'index.html').stat().st_mode)
                lines.append(f'dist-index-mode:{mode:04o}')
            return result, lines

    def test_candidate_build_follows_default_verification(self):
        result, lines = self.run_deploy({
            'VITE_VM_NETWORK_POLICY_ENABLED': '1',
            'VITE_PUBLIC_SOURCE_POLICY_ENABLED': '1',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verify:unset:unset:', lines)
        self.assertIn('npm:run --silent build:1:1', lines)
        self.assertLess(lines.index('verify:unset:unset:'),
                        lines.index('npm:run --silent build:1:1'))
        self.assertTrue(any(line.startswith('pct:push ') for line in lines))

    def test_default_deploy_uses_verified_bundle_without_rebuild(self):
        result, lines = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verify:unset:unset:', lines)
        self.assertFalse(any(line.startswith('npm:run --silent build') for line in lines))
        self.assertTrue(any(line.startswith('pct:push ') for line in lines))

    def test_static_bundle_is_readable_with_restrictive_input_mode(self):
        result, lines = self.run_deploy({
            'VITE_VM_NETWORK_POLICY_ENABLED': '0',
            'VITE_PUBLIC_SOURCE_POLICY_ENABLED': '0',
        }, private_dist=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('dist-index-mode:0644', lines)
        self.assertTrue(any(line.startswith('pct:push ') for line in lines))

    def test_failed_verify_never_builds_or_deploys(self):
        result, lines = self.run_deploy({'VITE_VM_NETWORK_POLICY_ENABLED': '1'},
                                        fail_verify=True)
        self.assertEqual(result.returncode, 13)
        self.assertIn('verify:unset:unset:', lines)
        self.assertFalse(any(line.startswith('npm:run --silent build') for line in lines))
        self.assertFalse(any(line.startswith('tar:') for line in lines))
        self.assertFalse(any(line.startswith('pct:push ') or line.startswith('pct:exec ')
                             for line in lines))

    def test_unsupported_flag_fails_before_install(self):
        result, lines = self.run_deploy({'VITE_PUBLIC_SOURCE_POLICY_ENABLED': 'true'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected 0 or 1', result.stderr)
        self.assertEqual(lines, ['pct:config 201'])

    def test_explicit_candidate_hostname_allows_deploy(self):
        result, lines = self.run_deploy(
            {'EXPECTED_CT_HOSTNAME': 'pickle-app-example'}, hostname='pickle-app-example')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(line.startswith('pct:push ') for line in lines))

    def test_hostname_mismatch_fails_before_install(self):
        result, lines = self.run_deploy({'EXPECTED_CT_HOSTNAME': 'pickle-app-example'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("container 201 is 'pickle-app'", result.stderr)
        self.assertEqual(lines, ['pct:config 201'])

    def test_empty_expected_hostname_fails_before_target_lookup(self):
        result, lines = self.run_deploy({'EXPECTED_CT_HOSTNAME': ''})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('single lowercase hostname', result.stderr)
        self.assertEqual(lines, [])


if __name__ == '__main__':
    unittest.main()
