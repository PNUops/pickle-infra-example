"""Offline DNS health fixtures: failures must affect the snapshot exit status."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


LIBRARY = Path(__file__).resolve().parents[1] / 'lib/platform-dns-health.sh'
HEALTH_SCRIPT = Path(__file__).resolve().parents[1] / 'health-check.sh'
NS = ' '.join(f'ns{i}.example.test' for i in range(1, 5))
HARNESS = r'''
set -uo pipefail
FAILS=0
rec() {
  printf '%s|%s|%s\n' "$1" "$2" "${3:-}"
  if [ "$2" = FAIL ]; then FAILS=$((FAILS+1)); fi
}
psqv() {
  [ "$DB_FIXTURE_FAIL" = 0 ] || return 1
  printf '%s\n' "$DB_FIXTURE_ROWS"
}
. "$1"
check_platform_dns
[ "$FAILS" -eq 0 ]
'''
DIG = r'''#!/usr/bin/env python3
import json
import os
import sys
fixture = json.loads(os.environ['DNS_FIXTURE'])
args = sys.argv[1:]
server = next((a[1:] for a in args if a.startswith('@')), '')
owner, kind = [a for a in args if not a.startswith(('+', '@'))]
if fixture.get('timeout_server') == server and server:
    sys.exit(9)
status, answers = 'NOERROR', []
aa = fixture.get('authoritative', True)
if kind == 'NS':
    names = fixture.get('ns', os.environ['PLATFORM_DNS_EXPECTED_NS'])
    answers = [f'{owner}. 21600 IN NS {ns}.' for ns in names.split()]
elif owner == os.environ['PLATFORM_DNS_PROBE_FQDN']:
    status = fixture.get('unregistered_status', 'NXDOMAIN')
    if fixture.get('wildcard'):
        status = 'NOERROR'
        answers = [f'{owner}. 300 IN A 203.0.113.10']
else:
    address = '203.0.113.10'
    if owner == fixture.get('wrong_a_name'):
        address = '203.0.113.20'
    if owner != fixture.get('missing_a_name'):
        answer_owner = fixture.get('answer_owner', owner)
        answers = [f'{answer_owner}. 300 IN A {address}']
    if owner == fixture.get('cname_name'):
        answers.insert(0, f'{owner}. 300 IN CNAME elsewhere.example.test.')
flags = 'qr aa' if aa else 'qr'
print(f';; ->>HEADER<<- opcode: QUERY, status: {status}, id: 1')
print(f';; flags: {flags}; QUERY: 1, ANSWER: {len(answers)}, AUTHORITY: 1, ADDITIONAL: 0')
print('\n'.join(answers))
'''


class PlatformDnsHealthTest(unittest.TestCase):
    def run_health(self, fixture=None, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'dig'
            binary.write_text(DIG)
            binary.chmod(0o755)
            env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}',
                       PLATFORM_ROOT_DOMAIN='example.test', PLATFORM_DNS_MODE='explicit',
                       PLATFORM_DNS_EXPECTED_NS=NS, PLATFORM_DNS_MANUAL_FQDNS='',
                       PLATFORM_DNS_PROBE_FQDN='probe.example.test',
                       MAIN_DOMAIN_PUBLIC_IP='203.0.113.10', DB_FIXTURE_FAIL='0',
                       DB_FIXTURE_ROWS='FAILED|0', DNS_FIXTURE=json.dumps(fixture or {}))
            env.update(overrides)
            return subprocess.run(['bash', '-c', HARNESS, 'health-fixture', str(LIBRARY)],
                                  env=env, text=True, capture_output=True, timeout=15)

    def assert_failure(self, result, label):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(label, result.stdout)
        self.assertIn('|FAIL|', result.stdout)

    def test_snapshot_fails_when_helper_cannot_be_loaded(self):
        health = HEALTH_SCRIPT.read_text()
        bootstrap = health[health.index('# ---- 14. platform root:'):health.index('# ---- output')]
        for helper in (None, 'if then\n', '# No DNS check function\n'):
            with self.subTest(helper=helper), tempfile.TemporaryDirectory() as tmp:
                scripts = Path(tmp) / 'scripts'
                (scripts / 'lib').mkdir(parents=True)
                if helper is not None:
                    (scripts / 'lib/platform-dns-health.sh').write_text(helper)
                runner = scripts / 'health-check.sh'
                runner.write_text(HARNESS.split('. "$1"')[0] + bootstrap + '[ "$FAILS" -eq 0 ]\n')
                result = subprocess.run(['bash', str(runner)], text=True,
                                        capture_output=True, timeout=5)
                self.assert_failure(result, 'dns:platform|FAIL|')

    def test_empty_serving_set_skips_only_serving_names(self):
        result = self.run_health()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('dns:serving|SKIP|', result.stdout)
        self.assertEqual(result.stdout.count('dns:apex:'), 4)
        self.assertEqual(result.stdout.count('dns:unregistered:'), 4)
        self.assertIn('dns:ns|OK|', result.stdout)

    def test_serving_and_manual_names_are_checked_on_every_authority(self):
        result = self.run_health(DB_FIXTURE_ROWS='FAILED|0\nSERVING|site.example.test',
                                 PLATFORM_DNS_MANUAL_FQDNS='staging.example.test')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('dns:serving:site.example.test:'), 4)
        self.assertEqual(result.stdout.count('dns:manual:staging.example.test:'), 4)

    def test_wrong_or_missing_serving_and_manual_a_fail(self):
        for owner in ('site.example.test', 'staging.example.test', 'example.test'):
            for problem in ('wrong_a_name', 'missing_a_name', 'cname_name'):
                with self.subTest(owner=owner, problem=problem):
                    result = self.run_health({problem: owner},
                        DB_FIXTURE_ROWS='FAILED|0\nSERVING|site.example.test',
                        PLATFORM_DNS_MANUAL_FQDNS='staging.example.test')
                    self.assert_failure(result, owner)

    def test_fallback_cannot_hide_missing_exact_record(self):
        result = self.run_health({'wildcard': True},
                                 DB_FIXTURE_ROWS='FAILED|0\nSERVING|site.example.test')
        self.assert_failure(result, 'dns:unregistered:')

    def test_nodata_servfail_and_refused_are_not_nxdomain(self):
        for status in ('NOERROR', 'SERVFAIL', 'REFUSED'):
            with self.subTest(status=status):
                self.assert_failure(self.run_health({'unregistered_status': status}),
                                    'dns:unregistered:')

    def test_timeout_and_nonauthoritative_answers_fail(self):
        self.assert_failure(self.run_health({'timeout_server': 'ns2.example.test'}),
                            'ns2.example.test')
        self.assert_failure(self.run_health({'authoritative': False}), 'dns:unregistered:')

    def test_wrong_nameserver_set_and_unconfigured_expected_set_fail(self):
        self.assert_failure(self.run_health({'ns': 'ns9.example.test'}), 'dns:ns|FAIL|')
        self.assert_failure(self.run_health(PLATFORM_DNS_EXPECTED_NS=''), 'dns:ns|FAIL|')

    def test_database_failure_is_not_an_empty_serving_set(self):
        result = self.run_health(DB_FIXTURE_FAIL='1')
        self.assert_failure(result, 'dns:domains|FAIL|')
        self.assertNotIn('dns:serving|SKIP|', result.stdout)
        self.assert_failure(self.run_health(DB_FIXTURE_ROWS=''), 'dns:domains|FAIL|')
        self.assert_failure(self.run_health(DB_FIXTURE_ROWS='FAILED|1'), 'dns:apply|FAIL|')

    def test_rollback_mode_requires_wildcard_answer(self):
        result = self.run_health({'wildcard': True}, PLATFORM_DNS_MODE='wildcard')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('dns:wildcard:'), 4)
        self.assert_failure(self.run_health(PLATFORM_DNS_MODE='wildcard'), 'dns:wildcard:')

    def test_invalid_configuration_and_foreign_database_name_fail(self):
        for env in ({'PLATFORM_DNS_MODE': 'other'},
                    {'PLATFORM_DNS_PROBE_FQDN': 'outside.example.net'},
                    {'PLATFORM_DNS_MANUAL_FQDNS': 'outside.example.net'},
                    {'PLATFORM_DNS_PROBE_FQDN': 'probe.exampleXtest'},
                    {'PLATFORM_DNS_MANUAL_FQDNS': 'staging.exampleXtest'},
                    {'DB_FIXTURE_ROWS': 'FAILED|0\nSERVING|site.exampleXtest'},
                    {'DB_FIXTURE_ROWS': 'FAILED|0\nSERVING|outside.example.net'}):
            with self.subTest(env=env):
                self.assert_failure(self.run_health(**env), 'dns:')

    def test_unconfigured_root_is_explicitly_unarmed(self):
        result = self.run_health(PLATFORM_ROOT_DOMAIN='')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('|SKIP|'), 1)


if __name__ == '__main__':
    unittest.main()
