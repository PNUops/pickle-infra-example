"""Host agent: run due checks, push the newest result of every check to the judge.

The agent keeps no backlog. A failed push leaves only the newest results on
disk, so a long outage cannot release a flood of stale samples when the link
comes back.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

from . import catalog as catalog_mod
from .checks import REGISTRY, CheckError
from .result import ResultError, clean
from .util import OpsError, atomic_json, read_json

PUSH_TIMEOUT = 10
DUE_SLACK = 5
# Wall-clock limit for one round of checks. With the push timeout this stays
# well inside the unit's TimeoutStartSec, so a round always ends in a push.
CHECK_BUDGET = 30
# Consecutive failed pushes before the fallback path speaks, and the judge age
# that counts as stopped. Three minutes matches the judge's own host timeout.
FALLBACK_PUSH_FAILURES = 3
FALLBACK_JUDGE_AGE = 300
FALLBACK_UNDELIVERED = 3


class Context:
    """What a check may use: the time, its own persistent state, and commands."""

    def __init__(self, now, state, runner=subprocess.run):
        self.now = now
        self.state = state
        self._runner = runner

    def run(self, argv, timeout=12, input_text=None):
        try:
            result = self._runner(argv, capture_output=True, text=True, timeout=timeout,
                                  input=input_text, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        except subprocess.TimeoutExpired:
            raise CheckError('CHECK_TIMEOUT') from None
        except OSError:
            raise CheckError('COMMAND_FAILED') from None
        if result.returncode:
            raise CheckError('COMMAND_FAILED')
        return result.stdout.strip()

    def pct(self, ct, argv, timeout=12):
        return self.run(['pct', 'exec', str(ct), '--', *argv], timeout=timeout)


_SLOTS = threading.BoundedSemaphore(8)  # bounds concurrent pct/psql processes on a busy host


def _start(item, ctx):
    """Start one check in a daemon thread; a hung socket cannot stall the agent."""
    module = REGISTRY[item['check']]
    box = {}

    def target():
        try:
            with _SLOTS:
                box['value'] = module.run(item['args'], ctx)
        except CheckError as error:
            box['value'] = {'status': 'ERROR', 'reason': error.reason, 'detail': {}}
        except Exception:  # noqa: BLE001 - any crash is an observation failure, never a service verdict
            box['value'] = {'status': 'ERROR', 'reason': 'CHECK_CRASHED', 'detail': {}}

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _finish(item, ctx, box):
    value = box.get('value') or {'status': 'ERROR', 'reason': 'CHECK_TIMEOUT', 'detail': {}}
    try:
        return clean(value, item['check'], item_id=item['id'], sample_at=ctx.now)
    except ResultError:
        # A check that returns an undeclared field is a bug in the check, and the
        # receiver would refuse the whole bundle for it. Report the bug instead.
        return clean({'status': 'ERROR', 'reason': 'CHECK_CRASHED', 'detail': {}},
                     item['check'], item_id=item['id'], sample_at=ctx.now)


def run_check(item, ctx, timeout):
    thread, box = _start(item, ctx)
    thread.join(timeout)
    return _finish(item, ctx, box)


def collect(items, state, now, *, runner=subprocess.run, clock=time.time, budget=CHECK_BUDGET):
    """Run every due check concurrently and return the newest result for each item.

    Checks run side by side so the whole round ends within one check's limit:
    when campus is cut off every external check times out at once, and run one
    after another they would outlast the service and never push the outage.
    """
    running = []
    started = time.monotonic()
    for item in items:
        entry = state.setdefault(item['id'], {})
        due = entry.get('last_run', 0) + item['every'] - DUE_SLACK <= now or 'result' not in entry
        if due:
            ctx = Context(clock(), entry.setdefault('check_state', {}), runner)
            thread, box = _start(item, ctx)
            running.append((item, entry, ctx, thread, box, min(budget, item['every'] - 5)))
    for item, entry, ctx, thread, box, limit in running:
        thread.join(max(0.0, limit - (time.monotonic() - started)))
        entry['result'] = _finish(item, ctx, box)
        entry['last_run'] = now
    results = [state[item['id']]['result'] for item in items]
    for stale in set(state) - {item['id'] for item in items}:
        del state[stale]
    return results


def push(config, bundle, *, runner=subprocess.run):
    """Deliver a bundle. Returns the judge's response dict, or None on failure."""
    data = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    target = config['push']
    if target['mode'] == 'command':
        # On the judge host itself: hand the bundle to the receiver as the
        # judge's account (for example via runuser), so the inbox keeps one owner.
        argv = list(target['argv'])
    else:
        argv = ['ssh', '-T', '-i', target['key_file'], '-p', str(target['port']),
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + target['known_hosts'],
                '-o', 'ServerAliveInterval=3', '-o', 'ServerAliveCountMax=1', target['destination']]
    try:
        result = runner(argv, input=data, capture_output=True, text=True, timeout=PUSH_TIMEOUT,
                        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode:
        return None
    try:
        response = json.loads(result.stdout)
    except ValueError:
        return None
    return response if isinstance(response, dict) else None


def fallback(config, state, response, now, *, sender=None):
    """pve-node-3 only: speak directly when the judge cannot be reached or has stopped.

    A failed Discord send is not retried as an alert: if Discord is unreachable
    too, campus outbound is down and there is nobody to tell from here.
    """
    settings = config.get('fallback')
    if not settings:
        return None
    memo = state.setdefault('_fallback', {'failures': 0, 'alerted': False})
    if response is None:
        memo['failures'] += 1
    else:
        memo['failures'] = 0
    judge_age = response.get('judge_age') if isinstance(response, dict) else None
    problem = None
    if memo['failures'] >= FALLBACK_PUSH_FAILURES:
        problem = 'unreachable'
    elif type(judge_age) in (int, float) and judge_age > FALLBACK_JUDGE_AGE:
        problem = 'stopped'
    elif isinstance(response, dict) and response.get('undelivered', 0) >= FALLBACK_UNDELIVERED:
        # One unconfirmed delivery is normal Discord noise; several in the
        # judge's half-hour window mean alerts are not getting through.
        problem = 'undelivered'
    if problem and not memo['alerted']:
        memo.setdefault('since', now)
        if _send_fallback(settings, problem, memo['since'], recovered=False, sender=sender):
            memo['alerted'] = True
            memo['kind'] = problem
    elif not problem:
        if memo['alerted']:
            if _send_fallback(settings, memo.get('kind', 'unreachable'), memo.get('since', now), recovered=True, sender=sender):
                memo.update(alerted=False)
                memo.pop('since', None)
        else:
            memo.pop('since', None)
    return problem


def _send_fallback(settings, problem, since, *, recovered, sender):
    from . import notify, render
    payload = render.fallback_payload(problem, since, recovered=recovered)
    try:
        webhook = notify.load_webhook(settings['webhook_file'])
        outcome = (sender or notify.send)(webhook, payload)
    except (OpsError, OSError):
        return False
    return outcome.get('delivery') == 'SENT'


def main(config_path):
    config = read_json(config_path, private=True)
    catalog = catalog_mod.load(config['catalog'])
    host = config['host']
    if host not in catalog['hosts']:
        raise OpsError('this host is not in the catalog')
    if socket.gethostname().split('.')[0] != catalog['hosts'][host]['hostname']:
        raise OpsError('the agent configuration belongs to a different host')
    state_dir = Path(config['state_dir'])
    state_dir.mkdir(mode=0o700, exist_ok=True)
    lock = os.open(state_dir / 'agent.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The previous run is still going; skipping keeps runs from piling up.
            return {'mode': 'SKIPPED'}
        path = state_dir / 'agent-state.json'
        state = read_json(path, private=True) if path.exists() else {}
        items_state = state.setdefault('items', {})
        now = time.time()
        results = collect(catalog_mod.for_host(catalog, host), items_state, now)
        atomic_json(path, state)
        response = push(config, {'host': host, 'results': results, 'version': 1})
        problem = fallback(config, state, response, now)
        atomic_json(path, state)
        return {'mode': 'RUN', 'results': len(results), 'pushed': response is not None, 'fallback': problem}
    finally:
        os.close(lock)
