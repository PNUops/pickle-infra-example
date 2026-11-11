#!/usr/bin/env python3
"""Platform check tests: containers, database, certificates, host, backup pin, links.

No real command runs. A fake runner answers each argv from a rule table, and
every result goes through the agent's run_check so it is validated exactly as
the receiver would validate it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opswatch import catalog as catalog_mod  # noqa: E402
from opswatch.agent import Context, run_check  # noqa: E402
from opswatch.checks import REGISTRY, reasons  # noqa: E402
from opswatch.checks import backup_pin, cert_files  # noqa: E402
from opswatch.result import IPV4  # noqa: E402

T0 = 1794402000.0  # 2026-11-11 22:00 KST
CATALOG = Path(__file__).resolve().parents[1] / 'catalog'


class Runner:
    """Answer argv by the first rule whose every fragment appears in the joined command."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def __call__(self, argv, **_kwargs):
        line = ' '.join(argv)
        self.calls.append(line)
        for fragments, answer in self.rules:
            if all(fragment in line for fragment in fragments):
                if isinstance(answer, BaseException):
                    raise answer
                code, out = answer if isinstance(answer, tuple) else (0, answer)
                return subprocess.CompletedProcess(argv, code, out, '')
        return subprocess.CompletedProcess(argv, 1, '', 'unexpected command')


def check(kind, args, rules, now=T0):
    module = REGISTRY[kind]
    module.validate(args)
    runner = Runner(rules)
    result = run_check({'id': 'item', 'check': kind, 'args': args}, Context(now, {}, runner), timeout=10)
    return result, runner


class Assertions(unittest.TestCase):
    def assertResult(self, result, status, reason, **detail):
        self.assertEqual((result['status'], result['reason']), (status, reason), result)
        for key, value in detail.items():
            self.assertEqual(result['detail'].get(key), value, result)


class ContainerTests(Assertions):
    ARGS = {'ct': 202, 'units': ['sshpiperd.service', 'sshgw-terminal-bridge.service']}

    def test_running_with_active_units(self):
        result, runner = check('container', self.ARGS, [
            (['pct status 202'], 'status: running'), (['ActiveState'], 'active')])
        self.assertResult(result, 'OK', 'HEALTHY')
        self.assertIn('pct exec 202 -- systemctl show --property=ActiveState --value sshpiperd.service', runner.calls)

    def test_stopped_container(self):
        result, _ = check('container', self.ARGS, [(['pct status 202'], 'status: stopped')])
        self.assertResult(result, 'FAIL', 'CONTAINER_STOPPED')

    def test_inactive_and_restarting_units_are_named(self):
        result, _ = check('container', self.ARGS, [
            (['pct status'], 'status: running'), (['sshpiperd'], 'failed'), (['terminal-bridge'], 'activating')])
        self.assertResult(result, 'FAIL', 'UNIT_INACTIVE',
                          failed_units=['sshpiperd.service', 'sshgw-terminal-bridge.service'])

    def test_reloading_counts_as_up(self):
        result, _ = check('container', self.ARGS, [(['pct status'], 'status: running'), (['ActiveState'], 'reloading')])
        self.assertResult(result, 'OK', 'HEALTHY')

    def test_unreadable_unit_state_is_an_observation_error(self):
        result, _ = check('container', self.ARGS, [(['pct status'], 'status: running'), (['ActiveState'], (255, ''))])
        self.assertResult(result, 'ERROR', 'COMMAND_FAILED')

    def test_shared_reason_text_is_the_agreed_one(self):
        self.assertEqual(reasons()['UNIT_INACTIVE'], ('다음 서비스가 멈춰 있어요: {failed_units}', '서비스 중지'))

    def test_validate_refuses_shell_text_in_units(self):
        for units in (['nginx.service; reboot'], ['nginx'], 'nginx.service'):
            with self.assertRaises(ValueError):
                REGISTRY['container'].validate({'ct': 200, 'units': units})
        with self.assertRaises(ValueError):
            REGISTRY['container'].validate({'ct': '200', 'units': []})


class DiskAndMtuTests(Assertions):
    def test_fullest_container_decides(self):
        result, _ = check('container_disk', {'cts': [200, 201]}, [
            (['pct exec 200'], '41'), (['pct exec 201'], '84')])
        self.assertResult(result, 'WARN', 'CT_DISK_HIGH', worst_pct=84, ct_label='CT201')

    def test_disk_fail_threshold(self):
        result, _ = check('container_disk', {'cts': [204], 'warn_pct': 80, 'fail_pct': 90}, [(['pct exec 204'], '90')])
        self.assertResult(result, 'FAIL', 'CT_DISK_HIGH', worst_pct=90)

    def test_unparsable_disk_output_is_an_error(self):
        result, _ = check('container_disk', {'cts': [204]}, [(['pct exec 204'], 'Traceback')])
        self.assertResult(result, 'ERROR', 'CHECK_CRASHED')

    MTU_OK = [(['pct config 201'], 'arch: amd64\nnet0: name=eth0,bridge=pinfra,mtu=1450,type=veth'),
              (['cat /sys/class/net/veth201i0/mtu'], '1450'), (['pct exec 201'], '1450')]

    def test_matching_mtu(self):
        result, _ = check('container_mtu', {'cts': [201]}, self.MTU_OK)
        self.assertResult(result, 'OK', 'HEALTHY')

    def test_guest_fallen_back_to_1500(self):
        rules = [(['pct exec 201'], '1500')] + self.MTU_OK
        result, _ = check('container_mtu', {'cts': [201]}, rules)
        self.assertResult(result, 'FAIL', 'MTU_MISMATCH', mismatched=['CT201'])

    def test_net0_without_mtu_inherits_the_bridge(self):
        rules = [(['pct config 201'], 'net0: name=eth0,bridge=pinfra,type=veth')] + self.MTU_OK
        result, _ = check('container_mtu', {'cts': [201]}, rules)
        self.assertResult(result, 'OK', 'HEALTHY')

    def test_configured_mtu_differs_from_veth(self):
        rules = [(['pct config 201'], 'net0: name=eth0,mtu=1400,type=veth')] + self.MTU_OK
        result, _ = check('container_mtu', {'cts': [201]}, rules)
        self.assertResult(result, 'FAIL', 'MTU_MISMATCH', mismatched=['CT201'])


DB = {'ct': 204, 'database': 'pickle_prod'}


class SqlTests(Assertions):
    def sql(self, query, answer, **extra):
        return check('sql', dict(DB, query=query, **extra), [(['psql'], answer)])

    def test_query_goes_through_psql_as_postgres(self):
        result, runner = self.sql('read', '1')
        self.assertResult(result, 'OK', 'HEALTHY')
        self.assertTrue(runner.calls[0].startswith(
            'pct exec 204 -- runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -tA -d pickle_prod -c select 1'))

    def test_unreachable_database_is_a_failure_not_an_error(self):
        result, _ = self.sql('read', (2, ''))
        self.assertResult(result, 'FAIL', 'DB_UNREACHABLE')

    def test_other_queries_report_psql_failure_as_error(self):
        result, _ = self.sql('migrations', (2, ''))
        self.assertResult(result, 'ERROR', 'COMMAND_FAILED')

    def test_failed_migration(self):
        self.assertResult(self.sql('migrations', '0')[0], 'OK', 'HEALTHY')
        self.assertResult(self.sql('migrations', '2')[0], 'FAIL', 'MIGRATION_FAILED', count=2)

    def test_jobrunr_heartbeat(self):
        self.assertResult(self.sql('jobrunr_heartbeat', '0')[0], 'FAIL', 'JOBRUNR_DOWN')
        self.assertResult(self.sql('jobrunr_heartbeat', '1')[0], 'OK', 'HEALTHY', workers=1)
        self.assertResult(self.sql('jobrunr_heartbeat', '2')[0], 'WARN', 'JOBRUNR_EXTRA', workers=2)

    def test_recurring_jobs_against_three_cadences_with_a_floor(self):
        cadence = {'vm-status-poller': 30, 'route-reconcile': 120, 'vm-expiry': 3600, 'domain-renewal-sweeper': 86400}
        # vm-status-poller at 250 s is within the 300 s floor; route-reconcile at
        # 400 s is past 3 x 120; domain-renewal-sweeper has no SUCCEEDED row.
        rows = 'vm-status-poller|250\nroute-reconcile|400\nvm-expiry|7000\nunlisted-job|99999'
        result, _ = self.sql('recurring_jobs', rows, cadence=cadence)
        self.assertResult(result, 'FAIL', 'JOB_STALE', stale=['domain-renewal-sweeper', 'route-reconcile'], count=2)
        fresh = 'vm-status-poller|20\nroute-reconcile|100\nvm-expiry|3000\ndomain-renewal-sweeper|90000'
        self.assertResult(self.sql('recurring_jobs', fresh, cadence=cadence)[0], 'OK', 'HEALTHY')

    def test_long_stale_list_is_cut_to_the_detail_limit(self):
        cadence = {f'job-{n:02d}': 60 for n in range(20)}
        result, _ = self.sql('recurring_jobs', '', cadence=cadence)
        self.assertEqual(result['detail']['count'], 20)
        self.assertEqual(len(result['detail']['stale']), 15)
        self.assertEqual(result['detail']['stale'][-1], '외 6건')

    def test_recurring_output_shape_is_checked(self):
        result, _ = self.sql('recurring_jobs', 'garbage', cadence={'vm-expiry': 3600})
        self.assertResult(result, 'ERROR', 'CHECK_CRASHED')

    def test_failed_jobs_warn(self):
        self.assertResult(self.sql('failed_jobs', '3')[0], 'WARN', 'JOB_FAILED_RECENT', count=3)
        self.assertResult(self.sql('failed_jobs', '0')[0], 'OK', 'HEALTHY')

    def test_llm_failure_rate(self):
        args = {'fail_ratio': 0.2, 'min_requests': 10}
        self.assertResult(self.sql('llm_failure_rate', '9|9', **args)[0], 'OK', 'HEALTHY', requests=9)
        self.assertResult(self.sql('llm_failure_rate', '40|8', **args)[0], 'OK', 'HEALTHY', ratio_pct=20)
        self.assertResult(self.sql('llm_failure_rate', '40|9', **args)[0], 'FAIL', 'LLM_FAILURE_RATE',
                          requests=40, failed=9, ratio_pct=22)
        self.assertResult(self.sql('llm_failure_rate', '40|5', warn_ratio=0.1, **args)[0], 'WARN', 'LLM_FAILURE_RATE')

    def test_llm_failure_rate_counts_only_served_statuses(self):
        _, runner = self.sql('llm_failure_rate', '0|0')
        self.assertIn("status in ('OK', 'UPSTREAM_ERROR', 'TIMEOUT')", runner.calls[0])
        self.assertIn("filter (where status in ('UPSTREAM_ERROR', 'TIMEOUT'))", runner.calls[0])

    def test_contacts(self):
        self.assertResult(self.sql('contacts', '')[0], 'OK', 'HEALTHY')
        result, _ = self.sql('contacts', 'gateway||\nrelay|3|cse-relay\nrelay|4|\nroutes||')
        self.assertResult(result, 'FAIL', 'CONTACT_LOST',
                          stale=['LLM 게이트웨이', '중계 서버 cse-relay', '중계 서버 relay-4', '프록시 에이전트'])

    def test_mail_backlog(self):
        self.assertResult(self.sql('mail_backlog', '0|0')[0], 'OK', 'HEALTHY', count=0)
        self.assertResult(self.sql('mail_backlog', '4|20')[0], 'WARN', 'MAIL_BACKLOG', count=4, oldest_min=20)
        self.assertResult(self.sql('mail_backlog', '4|61')[0], 'FAIL', 'MAIL_BACKLOG')

    def test_openrouter_balance_and_freshness(self):
        self.assertResult(self.sql('openrouter', '0||')[0], 'OK', 'HEALTHY')
        self.assertResult(self.sql('openrouter', '1|153.250000|4')[0], 'OK', 'HEALTHY',
                          balance_usd=153.25, snapshot_age_min=4)
        self.assertResult(self.sql('openrouter', '1|12.500000|4')[0], 'WARN', 'OPENROUTER_BALANCE_LOW')
        self.assertResult(self.sql('openrouter', '1|153.25|31')[0], 'WARN', 'OPENROUTER_SNAPSHOT_STALE')
        # Never polled yet: the balance is unknown and only the age is judged.
        self.assertResult(self.sql('openrouter', '1||3')[0], 'OK', 'HEALTHY', snapshot_age_min=3)

    def test_openrouter_watched_key_usage(self):
        key = '0b9a6d1e-1c2f-4a8b-9e3d-5f6a7b8c9d0e'
        rules = [(['llm_api_keys'], '100'), (['openrouter_accounts'], '1|153.25|4')]
        result, runner = check('sql', dict(DB, query='openrouter', key_ids=[key]), rules)
        self.assertResult(result, 'FAIL', 'OPENROUTER_KEY_EXHAUSTED', usage_pct=100)
        self.assertIn(f"public_id in ('{key}')", runner.calls[1])
        rules[0] = (['llm_api_keys'], '85')
        result, _ = check('sql', dict(DB, query='openrouter', key_ids=[key]), rules)
        self.assertResult(result, 'WARN', 'OPENROUTER_KEY_HIGH', usage_pct=85)

    def test_openrouter_reason_renders_the_balance(self):
        from opswatch import render
        self.assertEqual(render.sentence('OPENROUTER_BALANCE_LOW', {'balance_usd': 12.5}),
                         'OpenRouter 계정 잔액이 12.50달러 남았어요.')

    def test_validate_takes_a_query_name_never_sql(self):
        validate = REGISTRY['sql'].validate
        for bad_args in (dict(DB, query='select 1'), dict(DB, query='read', database='x; drop'),
                         dict(DB, query='recurring_jobs', cadence={"x' or 1=1": 60}),
                         dict(DB, query='openrouter', key_ids=["') or true --"]),
                         dict(DB, query='failed_jobs', window_min='15 minutes; select')):
            with self.assertRaises((ValueError, KeyError)):
                validate(bad_args)


class NginxAndCertTests(Assertions):
    def test_nginx_exit_codes(self):
        for code, status, reason in (('0', 'OK', 'HEALTHY'), ('1', 'FAIL', 'NGINX_CONFIG_INVALID'),
                                     ('127', 'ERROR', 'COMMAND_FAILED')):
            result, runner = check('nginx_config', {'ct': 200}, [(['nginx -t'], code)])
            self.assertResult(result, status, reason)
        self.assertTrue(runner.calls[0].startswith('pct exec 200 -- sh -c'))

    ARGS = {'ct': 200, 'paths': ['/etc/letsencrypt/live/pickle.pusan.ac.kr/fullchain.pem',
                                 '/etc/letsencrypt/live/pusan.dev/fullchain.pem']}

    def certs(self, first, second):
        return check('cert_files', self.ARGS, [(['pickle.pusan.ac.kr'], f'notAfter={first} GMT'),
                                               (['pusan.dev'], f'notAfter={second} GMT')])[0]

    def test_soonest_lineage_is_named_by_directory(self):
        result = self.certs('Dec 25 04:18:56 2026', 'Dec  1 12:00:00 2026')
        self.assertResult(result, 'WARN', 'CERT_FILE_EXPIRING', days_left=19, cert_label='pusan.dev')

    def test_certificate_thresholds(self):
        self.assertResult(self.certs('Feb  1 00:00:00 2027', 'Mar  1 00:00:00 2027'), 'OK', 'HEALTHY',
                          cert_label='pickle.pusan.ac.kr')
        self.assertResult(self.certs('Feb  1 00:00:00 2027', 'Nov 17 00:00:00 2026'), 'FAIL', 'CERT_FILE_EXPIRING',
                          days_left=5)
        self.assertResult(self.certs('Feb  1 00:00:00 2027', 'Nov 10 00:00:00 2026'), 'FAIL', 'CERT_FILE_EXPIRED')

    def test_unparsable_end_date_is_an_error(self):
        result, _ = check('cert_files', self.ARGS, [(['openssl'], 'unable to load certificate')])
        self.assertResult(result, 'ERROR', 'CHECK_CRASHED')

    def test_label_for_other_files_is_the_basename(self):
        self.assertEqual(cert_files.label('/etc/nginx/mtls/server.crt'), 'server.crt')
        self.assertEqual(cert_files.label('/etc/letsencrypt/live/a/b/fullchain.pem'), 'fullchain.pem')

    def test_validate_paths(self):
        for paths in (['relative.pem'], ['/etc/../root/key'], ['/etc/x y.pem'], []):
            with self.assertRaises(ValueError):
                REGISTRY['cert_files'].validate({'ct': 200, 'paths': paths})


LVS = '  12.50|8.10'
DF = 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/mapper/pve-root 98559220 30000000 63500000 33% /'
QUORATE = 'Votequorum information\nExpected votes:   3\nQuorate:          Yes'
CHRONY = ('Reference ID    : C0A80001 (ntp)\nSystem time     : 0.000120345 seconds slow of NTP time\n'
          'Leap status     : Normal')


def host_rules(lvs=LVS, df=DF, pvecm=QUORATE, chrony=CHRONY):
    return [(['lvs'], lvs), (['df -P /'], df), (['pvecm status'], pvecm), (['chronyc tracking'], chrony)]


class HostTests(Assertions):
    ARGS = {'thin_pools': ['pve/data'], 'cluster': True}

    def test_healthy_host(self):
        result, runner = check('host', self.ARGS, host_rules())
        self.assertResult(result, 'OK', 'HEALTHY', worst_pct=33, offset_ms=0)
        self.assertIn('lvs --noheadings --nosuffix --separator | -o data_percent,metadata_percent pve/data',
                      runner.calls)

    def test_thin_pool_metadata_counts(self):
        result, _ = check('host', self.ARGS, host_rules(lvs='  40.00|71.30'))
        self.assertResult(result, 'WARN', 'THIN_POOL_HIGH', worst_pct=71)
        result, _ = check('host', self.ARGS, host_rules(lvs='  85,00|10,00'))
        self.assertResult(result, 'FAIL', 'THIN_POOL_HIGH', worst_pct=85)

    def test_thin_pool_name_matching_several_volumes_is_an_error(self):
        result, _ = check('host', self.ARGS, host_rules(lvs='  1.00|1.00\n  2.00|2.00'))
        self.assertResult(result, 'ERROR', 'CHECK_CRASHED')

    def test_root_disk(self):
        result, _ = check('host', self.ARGS, host_rules(df=DF.replace('33%', '91%')))
        self.assertResult(result, 'FAIL', 'HOST_DISK_HIGH', worst_pct=91)

    def test_quorum(self):
        result, _ = check('host', self.ARGS, host_rules(pvecm=QUORATE.replace('Yes', 'No')))
        self.assertResult(result, 'FAIL', 'QUORUM_LOST', problem=['클러스터 정족수'])
        result, _ = check('host', self.ARGS, host_rules(pvecm=(2, '')))
        self.assertResult(result, 'FAIL', 'QUORUM_LOST')
        result, runner = check('host', {'cluster': False}, host_rules(pvecm=(2, '')))
        self.assertResult(result, 'OK', 'HEALTHY')
        self.assertFalse(any('pvecm' in call for call in runner.calls))

    def test_clock(self):
        drift = CHRONY.replace('0.000120345 seconds slow', '1.500000000 seconds fast')
        self.assertResult(check('host', self.ARGS, host_rules(chrony=drift))[0], 'WARN', 'CLOCK_DRIFT', offset_ms=1500)
        far = CHRONY.replace('0.000120345', '6.000000000')
        self.assertResult(check('host', self.ARGS, host_rules(chrony=far))[0], 'FAIL', 'CLOCK_DRIFT')
        unsynced = CHRONY.replace('Normal', 'Not synchronised')
        self.assertResult(check('host', self.ARGS, host_rules(chrony=unsynced))[0], 'WARN', 'CLOCK_UNSYNCED')
        self.assertResult(check('host', self.ARGS, host_rules(chrony='506 Cannot talk to daemon'))[0],
                          'ERROR', 'CHECK_CRASHED')

    def test_several_problems_take_the_worst_status(self):
        drift = CHRONY.replace('0.000120345', '2.000000000')
        result, _ = check('host', self.ARGS, host_rules(lvs='  75.00|1.00', pvecm='Quorate: No', chrony=drift))
        self.assertResult(result, 'FAIL', 'HOST_PROBLEMS', problem=['thin pool 사용률', '클러스터 정족수', '시계'])
        from opswatch import render
        self.assertEqual(render.sentence(result['reason'], result['detail']),
                         '서버에 여러 문제가 있어요: thin pool 사용률, 클러스터 정족수, 시계')


HOOK = '''#!/usr/bin/env bash
for vmid in 200 201 203; do
  case "$vmid" in
    200) expected={h200} ;;
    201) expected={h201} ;;
    203) expected={h203} ;;
  esac
done
'''
CONFIGS = {200: b'arch: amd64\nhostname: edge\nmemory: 2048\n',
           201: b'arch: amd64\nhostname: api\nmemory: 8192\n',
           203: b'arch: amd64\nhostname: gw\nmemory: 16384\n'}


class BackupPinTests(Assertions):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'lxc').mkdir()
        hashes = {f'h{vmid}': hashlib.sha256(data).hexdigest() for vmid, data in CONFIGS.items()}
        self.hook = self.root / 'selected-hook'
        self.hook.write_text(HOOK.format(**hashes))
        for vmid, data in CONFIGS.items():
            (self.root / 'lxc' / f'{vmid}.conf').write_bytes(data)
        self.write_jobs(f'script {self.hook}')
        self.args = {'job': 'example-core-daily', 'jobs_file': str(self.root / 'jobs.cfg'),
                     'lxc_dir': str(self.root / 'lxc')}

    def write_jobs(self, *lines):
        body = ''.join(f'\t{line}\n' for line in lines)
        (self.root / 'jobs.cfg').write_text(
            'vzdump: other-job\n\tscript /bin/false\n\tenabled 1\n\n'
            f'vzdump: example-core-daily\n\tschedule 03:17\n{body}\tstorage pbs\n')

    def run_pin(self, running='status: running'):
        return check('backup_pin', self.args, [(['pct status'], running)])[0]

    def test_unchanged_configs_pass(self):
        self.assertResult(self.run_pin(), 'OK', 'HEALTHY')

    def test_changed_config_would_be_refused(self):
        (self.root / 'lxc' / '203.conf').write_bytes(CONFIGS[203].replace(b'16384', b'8192'))
        self.assertResult(self.run_pin(), 'FAIL', 'BACKUP_PIN_MISMATCH', mismatched=['CT203'])

    def test_one_backup_lock_is_a_run_in_progress(self):
        (self.root / 'lxc' / '201.conf').write_bytes(b'arch: amd64\nlock: backup\nhostname: api\nmemory: 8192\n')
        self.assertResult(self.run_pin(), 'OK', 'HEALTHY')
        (self.root / 'lxc' / '201.conf').write_bytes(b'lock: backup\n' + CONFIGS[201] + b'lock: backup\n')
        self.assertResult(self.run_pin(), 'FAIL', 'BACKUP_PIN_MISMATCH', mismatched=['CT201'])

    def test_missing_config_is_a_mismatch(self):
        (self.root / 'lxc' / '200.conf').unlink()
        self.assertResult(self.run_pin(), 'FAIL', 'BACKUP_PIN_MISMATCH', mismatched=['CT200'])

    def test_stopped_container_would_be_refused(self):
        self.assertResult(self.run_pin('status: stopped'), 'FAIL', 'BACKUP_CT_STOPPED',
                          stopped=['CT200', 'CT201', 'CT203'])

    def test_disabled_or_missing_job(self):
        self.write_jobs(f'script {self.hook}', 'enabled 0')
        self.assertResult(self.run_pin(), 'FAIL', 'BACKUP_JOB_MISSING')
        self.args['job'] = 'absent-job'
        self.assertResult(self.run_pin(), 'FAIL', 'BACKUP_JOB_MISSING')

    def test_hook_without_readable_pins(self):
        self.hook.write_text(HOOK.replace('203) expected', '203) other'))
        self.assertResult(self.run_pin(), 'ERROR', 'BACKUP_HOOK_UNPARSED')
        self.write_jobs('mode snapshot')
        self.assertResult(self.run_pin(), 'ERROR', 'BACKUP_HOOK_UNPARSED')

    def test_follows_the_hook_the_job_selects(self):
        other = self.root / 'repinned-hook'
        hashes = {f'h{vmid}': hashlib.sha256(data).hexdigest() for vmid, data in CONFIGS.items()}
        hashes['h203'] = '0' * 64
        other.write_text(HOOK.format(**hashes))
        self.write_jobs(f'script {other}')
        self.assertResult(self.run_pin(), 'FAIL', 'BACKUP_PIN_MISMATCH', mismatched=['CT203'])

    def test_parses_the_repository_hook(self):
        hook = Path(__file__).resolve().parents[2] / 'scripts' / 'candidate-core-vzdump-hook.sh'
        pinned = backup_pin.pins(hook.read_text())
        self.assertEqual(sorted(pinned), [1200, 1201, 1202, 1204])
        self.assertTrue(all(re.fullmatch(r'[0-9a-f]{64}', value) for value in pinned.values()))

    def test_unreadable_jobs_file_is_an_error(self):
        with mock.patch.object(backup_pin, 'read_bytes', side_effect=PermissionError):
            self.assertResult(self.run_pin(), 'ERROR', 'COMMAND_FAILED')


class InternalLinkTests(Assertions):
    ARGS = {'ct': 100, 'address': '198.18.122.168', 'port': 24443}

    def test_link_up_and_down(self):
        result, runner = check('internal_link', self.ARGS, [(['pct exec 100 -- bash -c'], 'up 3')])
        self.assertResult(result, 'OK', 'HEALTHY', latency_ms=3)
        self.assertTrue(runner.calls[0].endswith('198.18.122.168 24443 5'))
        result, _ = check('internal_link', self.ARGS, [(['pct exec 100'], 'down')])
        self.assertResult(result, 'FAIL', 'LINK_DOWN')
        self.assertNotIn('10.125', json.dumps(result))

    def test_unexpected_output_and_broken_wrapper(self):
        self.assertResult(check('internal_link', self.ARGS, [(['pct exec'], 'weird')])[0], 'ERROR', 'CHECK_CRASHED')
        self.assertResult(check('internal_link', self.ARGS, [(['pct exec'], (255, ''))])[0], 'ERROR', 'COMMAND_FAILED')

    def test_validate_address(self):
        for address in ('192.0.2.1; reboot', '', 'a b'):
            with self.assertRaises(ValueError):
                REGISTRY['internal_link'].validate(dict(self.ARGS, address=address))
        with self.assertRaises(ValueError):
            REGISTRY['internal_link'].validate(dict(self.ARGS, port=70000))


class ReasonTableTests(unittest.TestCase):
    OWN = ('container', 'container_disk', 'container_mtu', 'sql', 'nginx_config', 'cert_files', 'host',
           'backup_pin', 'internal_link')

    def test_reason_text_is_korean_and_address_free(self):
        table = reasons()
        for kind in self.OWN:
            for code, (template, category) in REGISTRY[kind].REASONS.items():
                self.assertEqual(table[code], (template, category))
                self.assertTrue(template.endswith('요.') or template.endswith('}'), code)
                self.assertFalse(IPV4.search(template), code)
                self.assertTrue(category and len(category) <= 20, code)
                for field in re.findall(r'{(\w+)', template):
                    self.assertIn(field, REGISTRY[kind].DETAIL, code)


class CatalogItemTests(unittest.TestCase):
    """Load this lane's item files on their own; another lane brings the wireguard check."""

    PENDING_KINDS = {'wireguard'}

    def load(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'items').mkdir()
        shutil.copy(CATALOG / 'hosts.json', root / 'hosts.json')
        skipped = []
        for name in ('pve-node.json', 'pve-node-2.json'):
            items = json.loads((CATALOG / 'items' / name).read_text(encoding='utf-8'))
            kept = [item for item in items if item['check'] in REGISTRY]
            skipped += [item['check'] for item in items if item['check'] not in REGISTRY]
            (root / 'items' / name).write_text(json.dumps(kept, ensure_ascii=False), encoding='utf-8')
        return catalog_mod.load(root), skipped

    def test_items_load_and_only_the_pending_kind_is_skipped(self):
        catalog, skipped = self.load()
        self.assertTrue(set(skipped) <= self.PENDING_KINDS, skipped)
        hosts = {item['host'] for item in catalog['items'].values()}
        self.assertEqual(hosts, {'pve-node', 'pve-node-2'})
        self.assertGreaterEqual(len(catalog['items']), 22)

    def test_page_labels_name_no_container_host_or_address(self):
        catalog, _ = self.load()
        for item in catalog['items'].values():
            label = item['page_label']
            self.assertFalse(re.search(r'CT ?\d|pve\d|dgx|\d{1,3}\.\d{1,3}\.', label, re.I), label)

    def test_every_recurring_job_of_the_api_is_listed(self):
        catalog, _ = self.load()
        cadence = catalog['items']['prod-recurring-jobs']['args']['cadence']
        self.assertEqual(len(cadence), 28)
        # JobRunr keeps SUCCEEDED rows 36 hours; a weekly job cannot be judged from them.
        self.assertNotIn('auth-token-retention-sweeper', cadence)
        self.assertTrue(all(seconds <= 86400 for seconds in cadence.values()))


if __name__ == '__main__':
    unittest.main(verbosity=1)
