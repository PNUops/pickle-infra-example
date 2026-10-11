#!/usr/bin/env python3
"""Read-only service checks for a split-container production deployment."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
from urllib.parse import urlsplit


def run(argv, timeout=12):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('probe returned a nonzero exit')
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    path = Path(args.config)
    metadata = path.lstat()
    if (os.geteuid() != 0 or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0 or metadata.st_mode & 0o022):
        parser.error('a root-owned config without group/other write access is required')
    config = json.loads(path.read_text())
    if config['expected_host'] != socket.gethostname().split('.')[0]:
        parser.error('the actual host differs from the selected deployment')
    containers = config['containers']
    if not containers or any(not str(ct).isdigit() or int(ct) < 100 for ct in containers):
        parser.error('explicit container IDs are required')
    for units in containers.values():
        if not units or any(not re.fullmatch(r'[A-Za-z0-9@_.-]+\.service', u) for u in units):
            parser.error('explicit systemd service names are required')
    for field in ('app_ctid', 'gateway_ctid', 'db_ctid', 'proxy_ctid'):
        if str(config[field]) not in containers:
            parser.error('probe containers must belong to the selected deployment')
    if not re.fullmatch(r'[a-z][a-z0-9_]*', config['database']):
        parser.error('invalid database name')
    gateway_url = urlsplit(config['gateway_health_url'])
    if (gateway_url.scheme != 'http' or gateway_url.path != '/healthz'
            or gateway_url.port != 8081 or gateway_url.username or gateway_url.password
            or gateway_url.query or gateway_url.fragment):
        parser.error('an explicit internal gateway health endpoint is required')
    checks = []

    def check(name, probe):
        try:
            detail = probe()
            checks.append({'name': name, 'ok': True, 'detail': detail})
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            checks.append({'name': name, 'ok': False, 'detail': 'probe failed; inspect the selected service journal'})

    def require(value, message):
        if not value:
            raise RuntimeError(message)
        return message

    def pct(ct, argv):
        return run(['pct', 'exec', str(ct), '--', *argv])

    for ct, units in containers.items():
        check('container:' + ct, lambda ct=ct: require(run(['pct', 'status', ct]) == 'status: running', 'running'))
        for unit in units:
            check(ct + ':' + unit, lambda ct=ct, unit=unit: require(pct(ct, ['systemctl', 'is-active', unit]) == 'active', 'active'))
        check(ct + ':disk', lambda ct=ct: require(int(pct(ct, ['python3', '-c',
            'import os;s=os.statvfs("/");print(round(100*(1-s.f_bavail/s.f_blocks)))'])) < 90, 'root filesystem below 90%'))

    app = config['app_ctid']
    check('api:readiness', lambda: require(json.loads(pct(app, ['curl', '-fsS', '--max-time', '8',
        'http://127.0.0.1:8080/actuator/health/readiness']))['status'] == 'UP', 'UP'))
    check('console:static', lambda: require(pct(app, ['curl', '-sS', '--max-time', '8', '-o', '/dev/null',
        '-w', '%{http_code}', 'http://127.0.0.1/']) == '200', 'HTTP200'))
    gateway_probe = ('import json,sys,urllib.request;'
        'o=urllib.request.build_opener(urllib.request.ProxyHandler({}));'
        'r=o.open(sys.argv[1],timeout=8);'
        'd=json.load(r);assert d["status"]=="ok";print("status=ok")')
    check('gateway:snapshot-health', lambda: pct(config['proxy_ctid'], ['python3', '-I', '-B', '-c', gateway_probe, config['gateway_health_url']]))
    db = config['database']

    def sql(query):
        return pct(config['db_ctid'], ['runuser', '-u', 'postgres', '--', 'psql', '-X', '-q',
            '-v', 'ON_ERROR_STOP=1', '-tA', '-d', db, '-c', query])

    check('database:read', lambda: require(sql('select 1') == '1', 'SELECT1'))
    check('database:migrations', lambda: require(sql('select count(*) from flyway_schema_history where not success') == '0', 'failed0'))
    check('jobrunr:worker-heartbeat', lambda: require(sql("select count(*) from jobrunr_backgroundjobservers where lastheartbeat > now() at time zone 'utc' - interval '90 seconds'") == '1', 'one current worker'))
    cadence = {'vm-status-poller': 300, 'domain-verification': 300, 'notification-dispatcher': 300,
        'route-reconcile': 600, 'deletion-sweeper': 1200, 'drift-reconciler': 2400,
        'stale-task-recovery': 2400, 'vm-expiry': 7200}

    def recurring():
        rows = sql("select recurringjobid||'|'||floor(extract(epoch from (now() at time zone 'utc' - max(updatedat))))::bigint from jobrunr_jobs where state='SUCCEEDED' and recurringjobid is not null group by recurringjobid")
        ages = dict(line.split('|', 1) for line in rows.splitlines())
        return require(all(name in ages and 0 <= int(ages[name]) <= limit for name, limit in cadence.items()), 'eight recurring jobs current')

    check('jobrunr:recurring-freshness', recurring)
    check('nginx:configuration', lambda: pct(config['proxy_ctid'], ['nginx', '-t']))
    for cert in config['certificates']:
        if not cert.startswith('/') or '\n' in cert:
            parser.error('absolute certificate paths are required')
        check('certificate:' + cert, lambda cert=cert: pct(config['proxy_ctid'],
            ['openssl', 'x509', '-noout', '-checkend', str(14 * 86400), '-in', cert]))
    print(json.dumps({'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'node': socket.gethostname(), 'healthy': all(c['ok'] for c in checks), 'checks': checks}, sort_keys=True))
    return 0 if all(c['ok'] for c in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
