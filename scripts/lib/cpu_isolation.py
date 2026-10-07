"""Whole-SMT CPU policy, exclusive LXC partition and bounded start checks.

The original installer and writer guards stay closed. Offline candidates are
not deployment receipts. IRQ, kernel worker and peripheral isolation is outside
this program's claim.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys

INSTALL_REVIEWED = False
APPLY_REVIEWED = False
CG = Path('/sys/fs/cgroup')
POLICY_PATH = Path('/etc/pickle/cpu-isolation/policy.json')
PROGRAM_PATH = Path('/usr/local/libexec/pickle-cpu-isolation.py')
HOOK_PATH = Path('/var/lib/vz/snippets/pickle-cpu-isolation-hook.py')
HOOK_VOLUME = 'local:snippets/pickle-cpu-isolation-hook.py'
CONTROL_NAMES = ('system.slice', 'user.slice', 'init.scope', 'lxc.monitor', 'lxc.pivot')
DROPIN_GROUPS = ('system.slice', 'user.slice', 'init.scope', 'qemu.slice',
                 'pve-container@.service', 'pve-guests.service')
CPU_DROPIN = '90-pickle-cpu-isolation.conf'
EXISTING_DROPINS = {'pve-guests.service': {
    'example-production-network.conf':
        '4bd160888cfc1743de5f7fd89b59b9dbba37ddd10080506fd03b95a0868e587e'}}
CODE_PATHS = ('/usr/share/perl5/PVE/QemuServer.pm', '/usr/share/perl5/PVE/API2/Qemu.pm',
              '/usr/share/perl5/PVE/GuestHelpers.pm', '/usr/share/perl5/PVE/LXC.pm',
              '/usr/share/perl5/PVE/LXC/Config.pm', '/usr/share/perl5/PVE/Service/pvestatd.pm')
CF = ('cgroup.controllers', 'cgroup.subtree_control', 'cgroup.procs',
      'cpuset.cpus', 'cpuset.cpus.effective', 'cpuset.cpus.exclusive',
      'cpuset.cpus.exclusive.effective', 'cpuset.cpus.partition')


class IsolationError(ValueError):
    """A policy, native state or custody precondition is unmet."""


def need(condition, reason):
    if not condition:
        raise IsolationError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def storage_projection(value):
    """Canonicalize only capability order; retain every other native field."""
    need(type(value) is dict and type(value.get('content')) is str,
         'Native storage content is not a typed CSV')
    members = value['content'].split(',')
    base = {'backup', 'vztmpl', 'import', 'iso'}
    need(all(re.fullmatch('[a-z]+', item) for item in members) and
         len(members) == len(set(members)) and set(members) in (base, base | {'snippets'}),
         'Storage capabilities are missing, repeated or foreign')
    return value | {'content': ','.join(sorted(members))}


def whole(value, minimum=0):
    need(type(value) is int and minimum <= value <= 2147483647, 'Invalid integer')
    return value


def pin(value):
    need(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'Invalid SHA-256 pin')
    return value


def cpus(text):
    need(type(text) is str and re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', text),
         'Invalid nonempty CPU set')
    values = []
    for part in text.split(','):
        pair = [int(x) for x in part.split('-')]
        start, end = pair[0], pair[-1]
        need(0 <= start <= end <= 4095, 'Invalid CPU range')
        values.extend(range(start, end + 1))
    need(len(values) == len(set(values)), 'Repeated CPU in CPU set')
    return set(values)


def cpu_text(values):
    values = sorted(values)
    need(values and len(values) == len(set(values)), 'Empty or repeated CPU set')
    return ','.join(str(v) for v in values)


def load_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'Repeated JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def validate_config(c):
    need(type(c) is dict and set(c) == {'schema_version', 'node', 'physical_cores',
         'host_core_ids', 'platform_core_ids', 'platform_cts', 'parked_ct_ids',
         'committed_vcpu', 'expected_kernel', 'expected_pve_release',
         'expected_systemd_major', 'required_code_sha256',
         'clone_inheritance_review_ref'}, 'Unknown or missing policy field')
    need(type(c['schema_version']) is int and c['schema_version'] == 1, 'Unknown policy schema')
    need(type(c['node']) is str and re.fullmatch('[a-z][a-z0-9-]{0,62}', c['node']), 'Invalid node')
    need(whole(c['physical_cores'], 7) in (12, 16), 'Unsupported physical core count')
    need(c['host_core_ids'] == [0, 1] and c['platform_core_ids'] == [2, 3, 4, 5]
         and all(type(x) is int for x in c['host_core_ids'] + c['platform_core_ids']),
         'Host/platform physical core policy differs')
    whole(c['committed_vcpu'])
    need(c['committed_vcpu'] < (c['physical_cores'] * 2 - 12) * 2, 'No student budget')
    need(c['expected_kernel'] == '7.0.14-15-pve' and c['expected_pve_release'] == '9.2.11'
         and type(c['expected_systemd_major']) is int and c['expected_systemd_major'] == 257,
         'Unsupported kernel/PVE/systemd contract')
    need(type(c['required_code_sha256']) is dict and set(c['required_code_sha256']) == set(CODE_PATHS),
         'Installed PVE code pins are incomplete')
    for value in c['required_code_sha256'].values():
        pin(value)
    need(type(c['clone_inheritance_review_ref']) is str and
         1 <= len(c['clone_inheritance_review_ref']) <= 512,
         'Exact installed clone inheritance review is required')
    need(type(c['platform_cts']) is list and type(c['parked_ct_ids']) is list, 'Invalid guest classes')
    ids = []
    for ct in c['platform_cts']:
        need(type(ct) is dict and set(ct) == {'vmid', 'cores', 'core_ids'}, 'Invalid platform CT shape')
        ids.append(whole(ct['vmid'], 100))
        need(type(ct['core_ids']) is list and ct['core_ids'] and
             all(type(x) is int and x in c['platform_core_ids'] for x in ct['core_ids']) and
             len(ct['core_ids']) == len(set(ct['core_ids'])), 'Invalid platform CT core subset')
        need(whole(ct['cores'], 2) in (2, 4) and ct['cores'] == 2 * len(ct['core_ids']),
             'Existing CT cores must equal the whole-SMT explicit subset')
    ids.extend(whole(x, 100) for x in c['parked_ct_ids'])
    need(len(ids) == len(set(ids)), 'Guest classifications overlap')
    return c


def derive(c, topology):
    validate_config(c)
    need(type(topology) is list and len(topology) == 2 * c['physical_cores'], 'Incomplete topology')
    groups, seen = {}, set()
    for row in topology:
        need(type(row) is dict and set(row) == {'cpu', 'core', 'socket', 'online', 'siblings'},
             'Unexpected topology shape')
        cpu, core = whole(row['cpu']), whole(row['core'])
        need(row['online'] is True and type(row['socket']) is int and row['socket'] == 0,
             'Offline CPU or unsupported multi-socket topology')
        need(cpu not in seen and 0 <= core < c['physical_cores'], 'Duplicate or unknown CPU/core')
        seen.add(cpu)
        groups.setdefault(core, set()).add(cpu)
    need(set(groups) == set(range(c['physical_cores'])) and all(len(x) == 2 for x in groups.values()),
         'Incomplete SMT pair')
    for row in topology:
        need(cpus(row['siblings']) == groups[row['core']], 'Mixed or asymmetric SMT siblings')
    select = lambda core_ids: set().union(*(groups[x] for x in core_ids))
    h, o = select(c['host_core_ids']), select(c['platform_core_ids'])
    s = seen - h - o
    need(len(h) == 4 and len(o) == 8 and len(s) == len(seen) - 12 and
         not (h & o or h & s or o & s), 'Partition overlap or wrong reservation')
    return {'host': cpu_text(h), 'platform': cpu_text(o), 'student': cpu_text(s),
            'platform_cts': {str(ct['vmid']): cpu_text(select(ct['core_ids'])) for ct in c['platform_cts']},
            'topology_sha256': sha(canonical(sorted(topology, key=lambda x: x['cpu']))),
            'reserved_cpu_threads': 12, 'allocation_ratio': 2,
            'committed_vcpu': c['committed_vcpu'], 'allocatable_vcpu': len(s) * 2 - c['committed_vcpu']}


def read(path, limit=1048576, protected=False):
    path = Path(path)
    if protected:
        for parent in (path.parent, *path.parents):
            st = parent.lstat()
            need(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022,
                 'Untrusted parent directory')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode), 'Input is not a regular file')
        if protected:
            need(before.st_uid == 0 and before.st_nlink == 1 and not before.st_mode & 0o022,
                 'Input ownership or write permissions differ')
        raw = bytearray()
        while len(raw) <= limit:
            block = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        need(len(raw) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'Input changed or is too large')
        return bytes(raw)
    finally:
        os.close(fd)


def command(argv):
    result = subprocess.run(argv, capture_output=True, timeout=30,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    need(result.returncode == 0 and len(result.stdout) <= 4194304, 'Native command failed')
    return result.stdout.decode()


def cgroup_state(name):
    root = CG / name
    result = {}
    for f in CF:
        path = root / f
        result[f] = read(path).decode().strip() if path.exists() else None
    # PIDs are used only inside the host and never leave the collector.
    result['direct_process_count'] = len((result.pop('cgroup.procs') or '').split())
    return result


def native_topology():
    root = Path('/sys/devices/system/cpu')
    online = cpus(read(root / 'online').decode().strip())
    return [{'cpu': cpu, 'core': int(read(root / f'cpu{cpu}/topology/core_id').decode()),
             'socket': int(read(root / f'cpu{cpu}/topology/physical_package_id').decode()),
             'online': cpu in online,
             'siblings': read(root / f'cpu{cpu}/topology/thread_siblings_list').decode().strip()}
            for cpu in sorted(cpus(read(root / 'present').decode().strip()))]


def guest_document(node, kind, row):
    vmid = whole(row['vmid'], 100)
    api = '/nodes/' + node + '/' + kind + '/' + str(vmid) + '/config'
    doc = load_json(command(['pvesh', 'get', api, '--output-format', 'json']))
    config_path = Path('/etc/pve/nodes') / node / ('lxc' if kind == 'lxc' else 'qemu-server') / f'{vmid}.conf'
    raw = read(config_path)
    explicit = [line.split(':', 1)[1].strip() for line in raw.decode().splitlines()
                if line.startswith('lxc.cgroup2.cpuset.cpus:')]
    need(len(explicit) <= 1, 'Duplicate CT CPU configuration')
    base = {k: v for k, v in doc.items() if k not in ('digest', 'hookscript')}
    if kind == 'lxc' and 'lxc' in base:
        need(type(base['lxc']) is list, 'Unexpected native custom LXC option shape')
        base['lxc'] = [x for x in base['lxc'] if x[0] != 'lxc.cgroup2.cpuset.cpus']
        if not base['lxc']:
            del base['lxc']
    return {'vmid': vmid, 'kind': kind, 'status': row['status'], 'template': doc.get('template', 0) == 1,
            'cores': doc.get('cores'), 'onboot': doc.get('onboot', 0),
            'cpuset': explicit[0] if explicit else None, 'hookscript': doc.get('hookscript'),
            'affinity': doc.get('affinity'), 'config_sha256': sha(raw), 'digest': doc.get('digest'),
            'document_sha256': sha(canonical({k: v for k, v in doc.items() if k != 'digest'})),
            'base_document_sha256': sha(canonical(base))}


def thread_cpu_sets(group):
    result = set()
    for path in (CG / group).rglob('cgroup.threads'):
        for text in read(path).decode().split():
            tid = int(text)
            try:
                result.add(cpu_text(os.sched_getaffinity(tid)))
            except ProcessLookupError:
                pass
    return sorted(result)


def proc_start(tid):
    raw = read('/proc/' + str(tid) + '/stat', 8192).decode()
    # The parenthesized command can contain spaces or parentheses.
    fields = raw[raw.rfind(')') + 2:].split()
    return int(fields[19])


def qemu_inventory():
    scopes = sorted(int(x.name[:-6]) for x in (CG / 'qemu.slice').glob('*.scope')
                    if re.fullmatch(r'[1-9][0-9]*\.scope', x.name))
    initial_tids = set()
    for vmid in scopes:
        for path in (CG / 'qemu.slice' / (str(vmid) + '.scope')).rglob('cgroup.threads'):
            initial_tids.update(int(x) for x in read(path).decode().split())
    threads = {}
    processes = set()
    scanned = 0
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            exe = Path(os.readlink(proc / 'exe')).name
            if exe.startswith('qemu-system-') or exe == 'kvm':
                group = read(proc / 'cgroup', 4096).decode().strip()
                match = re.fullmatch(r'0::/qemu\.slice/([1-9][0-9]*)\.scope', group)
                need(match is not None, 'QEMU process is outside the bounded parent')
                processes.add(int(match.group(1)))
        except (FileNotFoundError, ProcessLookupError):
            # Kernel helpers can have no executable symlink. They still belong
            # in the vhost census; an absent exe must not skip their tasks.
            pass
        try:
            tasks = list((proc / 'task').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            raise IsolationError('A process disappeared during the complete thread census')
        for task in tasks:
            try:
                tid = int(task.name)
                scanned += 1
                need(scanned <= 65536, 'Thread census exceeds the bounded host scope')
                start = proc_start(tid)
                comm = read(task / 'comm', 256).decode().strip()
                group = read(task / 'cgroup', 4096).decode().strip()
                match = re.fullmatch(r'0::/qemu\.slice/([1-9][0-9]*)\.scope', group)
                is_vhost = comm.startswith('vhost')
                need(not is_vhost or match is not None, 'vhost helper is outside the bounded parent')
                if match:
                    allowed = cpu_text(os.sched_getaffinity(tid))
                    need(proc_start(tid) == start, 'Thread identity changed during collection')
                    threads[tid] = {'vmid': int(match.group(1)), 'tid': tid, 'start_ticks': start,
                                    'kind': 'vhost' if is_vhost else 'qemu',
                                    'cgroup': group[3:], 'allowed_cpus': allowed}
            except (FileNotFoundError, ProcessLookupError):
                raise IsolationError('A thread disappeared during the complete thread census')
    # The scope prefix is fixed; this second census detects a missed native
    # thread or a process which entered/left during observation.
    native_tids = set()
    for vmid in scopes:
        for path in (CG / 'qemu.slice' / (str(vmid) + '.scope')).rglob('cgroup.threads'):
            native_tids.update(int(x) for x in read(path).decode().split())
    need(initial_tids == native_tids == set(threads), 'Native scope/proc thread census changed or is incomplete')
    return {'scope_vmids': scopes, 'qemu_process_vmids': sorted(processes),
            'threads': sorted(threads.values(), key=lambda x: (x['vmid'], x['tid'])),
            'vhost_scan_complete': True, 'thread_scan_complete': True}


def permitted_existing_dropins(report):
    """Admit only the pinned network dependency; never rewrite it."""
    controls = report.get('systemd_dropins')
    metadata = report.get('systemd_dropin_metadata')
    need(type(controls) is dict and set(controls) == set(DROPIN_GROUPS) and
         type(metadata) is dict and set(metadata) == set(DROPIN_GROUPS),
         'Existing drop-in inventory is incomplete')
    for group, entries in controls.items():
        need(type(entries) is dict and type(metadata[group]) is dict and
             set(metadata[group]) == set(entries), 'Existing drop-in metadata differs')
        for name, value in entries.items():
            need(EXISTING_DROPINS.get(group, {}).get(name) == value,
                 'An existing drop-in has another owner')
            row = metadata[group][name]
            need(type(row) is dict and all(type(row.get(k)) is int and row[k] == v
                 for k, v in {'uid': 0, 'gid': 0, 'mode': 0o600, 'nlink': 1}.items()),
                 'Pinned network drop-in custody differs')


def preserved_dropins(before, after, files):
    """Reject added overrides and preserve original bytes and native identity."""
    permitted_existing_dropins(before)
    controls, metadata = after.get('systemd_dropins'), after.get('systemd_dropin_metadata')
    need(type(controls) is dict and set(controls) == set(DROPIN_GROUPS) and
         type(metadata) is dict and set(metadata) == set(DROPIN_GROUPS),
         'Installed drop-in inventory is incomplete')
    for group in DROPIN_GROUPS:
        expected = dict(before['systemd_dropins'][group])
        path = '/etc/systemd/system/' + group + '.d/' + CPU_DROPIN
        need(path in files, 'Owned CPU drop-in candidate is absent')
        expected[CPU_DROPIN] = sha(files[path])
        need(controls[group] == expected and type(metadata[group]) is dict and
             set(metadata[group]) == set(expected), 'Installed or foreign drop-in differs')
        for name, value in before['systemd_dropin_metadata'][group].items():
            need(metadata[group][name] == value, 'Existing network drop-in changed during CPU installation')


def observe(c):
    validate_config(c)
    need(os.geteuid() == 0 and socket.gethostname().split('.')[0] == c['node'], 'Wrong native host')
    topology = native_topology()
    masks = derive(c, topology)
    guests = []
    for kind in ('lxc', 'qemu'):
        rows = load_json(command(['pvesh', 'get', '/nodes/' + c['node'] + '/' + kind, '--output-format', 'json']))
        guests.extend(guest_document(c['node'], kind, row) for row in rows)
    state = {x: cgroup_state(x) for x in (*CONTROL_NAMES, 'lxc', 'qemu.slice')}
    for ct in c['platform_cts']:
        key = 'lxc/' + str(ct['vmid'])
        if (CG / key).exists():
            state[key] = cgroup_state(key)
            state[key + '/ns'] = cgroup_state(key + '/ns')
            state[key]['thread_cpu_sets'] = thread_cpu_sets(key)
    state['qemu.slice']['thread_cpu_sets'] = thread_cpu_sets('qemu.slice')
    storage = storage_projection(load_json(command(['pvesh', 'get', '/storage/local', '--output-format', 'json'])))
    controls, dropin_metadata = {}, {}
    for name in DROPIN_GROUPS:
        path = Path('/etc/systemd/system') / (name + '.d')
        controls[name] = {x.name: sha(read(x, protected=True)) for x in sorted(path.glob('*.conf'))} if path.exists() else {}
        dropin_metadata[name] = {}
        for x in sorted(path.glob('*.conf')) if path.exists() else ():
            s = x.lstat()
            dropin_metadata[name][x.name] = {'uid': s.st_uid, 'gid': s.st_gid,
                'mode': stat.S_IMODE(s.st_mode), 'nlink': s.st_nlink,
                'dev': s.st_dev, 'ino': s.st_ino, 'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns}
    installed = {}
    if PROGRAM_PATH.exists():
        installed['program_sha256'] = sha(read(PROGRAM_PATH, protected=True))
    if HOOK_PATH.exists():
        installed['hook_sha256'] = sha(read(HOOK_PATH, protected=True))
    return {'schema_version': 1, 'node': c['node'], 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'boot_id': read('/proc/sys/kernel/random/boot_id').decode().strip(),
            'kernel': command(['uname', '-r']).strip(),
            'pve_release': command(['pveversion']).split('/')[1],
            'systemd_major': int(command(['systemctl', '--version']).split()[1]),
            'topology': topology, 'masks': masks,
            'required_code_sha256': {p: sha(read(p)) for p in CODE_PATHS},
            'guests': sorted(guests, key=lambda x: (x['kind'], x['vmid'])),
            'cgroups': state, 'storage': storage, 'storage_config_sha256': sha(read('/etc/pve/storage.cfg')),
            'systemd_dropins': controls, 'systemd_dropin_metadata': dropin_metadata, 'installed_files': installed,
            'inventory': qemu_inventory(), 'host_or_guest_changed': False}


def validate_observation(c, report, *, fresh=True):
    need(report['node'] == c['node'] and report['kernel'] == c['expected_kernel'] and
         report['pve_release'] == c['expected_pve_release'] and
         report['systemd_major'] == c['expected_systemd_major'], 'Native runtime contract differs')
    need(report['required_code_sha256'] == c['required_code_sha256'], 'Installed PVE code drift')
    expected = derive(c, report['topology'])
    need(report['masks'] == expected, 'Mask or topology digest differs')
    if fresh:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(report['observed_at_utc'])).total_seconds()
        need(0 <= age <= 300, 'Native observation is stale or from the future')
    platform = {x['vmid']: x for x in c['platform_cts']}
    parked = set(c['parked_ct_ids'])
    seen = set()
    for g in report['guests']:
        key = (g['kind'], g['vmid'])
        need(key not in seen, 'Duplicate native guest')
        need(g['status'] in ('running','stopped') and type(g['template']) is bool,
             'Unknown native guest state/template flag')
        seen.add(key)
        if g['kind'] == 'lxc':
            need(g['vmid'] in platform or g['vmid'] in parked, 'Unknown LXC guest class')
            if g['vmid'] in platform:
                need(g['cores'] == platform[g['vmid']]['cores'], 'Existing CT core count differs')
            else:
                need(g['status'] == 'stopped' and type(g['onboot']) is int and g['onboot'] == 0,
                     'Parked recovery CT is running or onboot')
        elif g['kind'] == 'qemu':
            need(not g['template'] or g['status'] == 'stopped', 'A template cannot be running')
            need(g['affinity'] is None or cpus(g['affinity']) <= cpus(expected['student']),
                 'QEMU affinity escapes student CPUs')
        else:
            raise IsolationError('Unknown native guest class')
    need(set(platform) <= {g['vmid'] for g in report['guests'] if g['kind'] == 'lxc'},
         'Expected platform CT is absent')
    return expected


def verify(c, report, *, fresh=True, authority_sha256):
    pin(authority_sha256)
    masks = validate_observation(c, report, fresh=fresh)
    groups = report['cgroups']
    program_sha = pin(report['installed_files']['program_sha256'])
    need(report['installed_files'].get('hook_sha256') == sha(hook_bytes(program_sha)),
         'Enrolled executable hook bytes differ')
    for name in CONTROL_NAMES:
        need(groups[name]['cpuset.cpus'] is not None and
             cpus(groups[name]['cpuset.cpus']) == cpus(masks['host']) and
             cpus(groups[name]['cpuset.cpus.effective'] or '') == cpus(masks['host']),
             'Host cgroup mask is not effective')
    for name, which in (('lxc', 'platform'), ('qemu.slice', 'student')):
        g = groups[name]
        need(cpus(g['cpuset.cpus'] or '') == cpus(masks[which]) and
             cpus(g['cpuset.cpus.effective'] or '') == cpus(masks[which]) and
             'cpuset' in (g['cgroup.subtree_control'] or '').split(), 'Parent CPU inheritance is not effective')
    lxc = groups['lxc']
    need(lxc['cpuset.cpus.partition'] == 'root' and lxc['direct_process_count'] == 0 and
         cpus(lxc['cpuset.cpus.exclusive'] or '') == cpus(masks['platform']) and
         cpus(lxc['cpuset.cpus.exclusive.effective'] or '') == cpus(masks['platform']),
         'Platform partition is invalid, degraded or nonexclusive')
    for g in report['guests']:
        if g['kind'] == 'qemu':
            need(g['hookscript'] == HOOK_VOLUME, 'QEMU/template is not enrolled in the start gate')
        elif g['vmid'] not in c['parked_ct_ids']:
            expected = masks['platform_cts'][str(g['vmid'])]
            need(g['cpuset'] is not None and cpus(g['cpuset']) == cpus(expected), 'CT explicit mask differs')
            if g['status'] == 'running':
                for name in ('lxc/' + str(g['vmid']), 'lxc/' + str(g['vmid']) + '/ns'):
                    need(name in groups and cpus(groups[name]['cpuset.cpus.effective'] or '') == cpus(expected),
                         'Running CT payload mask differs')
                need(groups['lxc/' + str(g['vmid'])]['thread_cpu_sets'] and
                     all(cpus(x) <= cpus(expected) for x in groups['lxc/' + str(g['vmid'])]['thread_cpu_sets']),
                     'CT thread affinity escapes its assigned mask')
    need(all(cpus(x) <= cpus(masks['student']) for x in groups['qemu.slice']['thread_cpu_sets']),
         'QEMU thread affinity escapes the student mask')
    inventory = report['inventory']
    running = sorted(g['vmid'] for g in report['guests'] if g['kind'] == 'qemu' and g['status'] == 'running')
    need(inventory['scope_vmids'] == running == inventory['qemu_process_vmids'] and
         inventory['vhost_scan_complete'] is True and inventory['thread_scan_complete'] is True,
         'QEMU scope/process/native guest census differs')
    tids, represented = set(), set()
    for thread in inventory['threads']:
        need(type(thread['tid']) is int and 0 < thread['tid'] <= 2147483647 and thread['tid'] not in tids and
             type(thread['start_ticks']) is int and thread['start_ticks'] > 0 and
             thread['vmid'] in running and thread['kind'] in ('qemu', 'vhost') and
             thread['cgroup'] == '/qemu.slice/' + str(thread['vmid']) + '.scope' and
             cpus(thread['allowed_cpus']) <= cpus(masks['student']),
             'QEMU/vhost TID census is incomplete or escapes the student pool')
        tids.add(thread['tid'])
        if thread['kind']=='qemu':
            represented.add(thread['vmid'])
    need(represented == set(running), 'A running QEMU has no observed native thread')
    return {'producer': 'pve-cpu-isolation-native-v1', 'node': c['node'], 'boot_id': report['boot_id'],
            'observed_at_utc': report['observed_at_utc'], 'policy_sha256': sha(canonical(c)),
            'topology_sha256': masks['topology_sha256'], 'partition': 'root',
            'userland_masks_equal': True, 'known_qemu_and_templates_enrolled': True,
            'program_sha256': program_sha,
            'observation_sha256': sha(canonical(report)),
            'inventory_sha256': sha(canonical(inventory)),
            'authority_sha256': authority_sha256,
            'global_hook_for_arbitrary_unenrolled_qemu': False,
            'irq_and_kernel_workers_verified': False, 'performance_or_full_protection_claim': False,
            'masks': masks, 'host_or_guest_changed': False}


def hook_bytes(program_sha256):
    pin(program_sha256)
    return f'''#!/usr/bin/python3
import hashlib, os, stat, sys
P='/usr/local/libexec/pickle-cpu-isolation.py'
fd=os.open(P,os.O_RDONLY|os.O_NOFOLLOW)
s=os.fstat(fd)
if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_nlink!=1 or s.st_mode&0o022:
    raise SystemExit(1)
raw=os.read(fd,1048577); os.close(fd)
if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!='{program_sha256}':
    raise SystemExit(1)
if len(sys.argv)!=3 or not sys.argv[1].isdigit():
    raise SystemExit(1)
if sys.argv[2]=='pre-start':
    sys.argv=[P,'qemu-pre-start','--vmid',sys.argv[1]]
    exec(compile(raw,P,'exec'),{{'__name__':'__main__','__file__':P}})
elif sys.argv[2] not in ('post-start','pre-stop','post-stop'):
    raise SystemExit(1)
'''.encode()


def render(c, topology, program_raw):
    masks = derive(c, topology)
    control = {}
    for name in ('system.slice', 'user.slice', 'init.scope', 'qemu.slice'):
        section = 'Scope' if name.endswith('.scope') else 'Slice'
        value = masks['student'] if name == 'qemu.slice' else masks['host']
        dependency = '[Unit]\nRequires=pickle-cpu-isolation.service\nAfter=pickle-cpu-isolation.service\n' if name == 'qemu.slice' else ''
        control['/etc/systemd/system/' + name + '.d/90-pickle-cpu-isolation.conf'] = (
            dependency + f'[{section}]\nAllowedCPUs={value}\n').encode()
    control['/etc/systemd/system/pve-container@.service.d/90-pickle-cpu-isolation.conf'] = b'''[Unit]
Requires=pickle-cpu-isolation.service
After=pickle-cpu-isolation.service
[Service]
ExecStartPre=/usr/bin/python3 /usr/local/libexec/pickle-cpu-isolation.py lxc-pre-start --vmid %i
'''
    control['/etc/systemd/system/pve-guests.service.d/90-pickle-cpu-isolation.conf'] = b'''[Unit]
Requires=pickle-cpu-isolation.service
After=pickle-cpu-isolation.service
'''
    control['/etc/systemd/system/pickle-cpu-isolation.service'] = b'''[Unit]
Description=Pickle whole-SMT CPU parent boundaries
After=pve-cluster.service
Requires=pve-cluster.service
Before=pve-guests.service qemu.slice
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/libexec/pickle-cpu-isolation.py boot-apply
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
'''
    control[str(POLICY_PATH)] = canonical(c)
    control[str(PROGRAM_PATH)] = program_raw
    control[str(HOOK_PATH)] = hook_bytes(sha(program_raw))
    return {'schema_version': 1, 'policy_sha256': sha(canonical(c)), 'masks': masks,
            'files': {path: {'sha256': sha(raw), 'bytes': len(raw),
                             'mode': '0755' if path == str(HOOK_PATH) else '0600' if path == str(POLICY_PATH) else '0644'}
                      for path, raw in control.items()},
            'install_executed': False, 'native_verify_executed': False,
            'clone_inheritance_review_ref': c['clone_inheritance_review_ref']}, control


def exclusive(path, raw, mode=0o600):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        need(read(path) == raw, 'Exclusive file readback differs')
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        # The partial is deliberately retained. No automatic replay or deletion.
        raise


def trusted_directory(path, *, create=False, mode=0o755):
    path = Path(path)
    if not path.exists():
        need(create, 'Required parent directory is absent')
        trusted_directory(path.parent)
        path.mkdir(mode=mode)
        os.chmod(path, mode)
    st = path.lstat()
    need(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022,
         'Directory is not root-owned or is writable by others')
    for parent in path.parents:
        st = parent.lstat()
        need(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022,
             'Directory ancestor is unsafe')


def mutate_guest(node, guest, *, cpuset=None, deadline_epoch):
    """Use the native PVE config lock and compare the original full file SHA."""
    need(INSTALL_REVIEWED, 'CPU installation guard is closed')
    path = '/etc/pve/nodes/' + node + ('/lxc/' if cpuset is not None else '/qemu-server/') + str(guest['vmid']) + '.conf'
    # Native libraries preserve config options and snapshots. Raw config bytes,
    # including any sensitive options, never travel outside the native process.
    prefix = r'''
use strict; use warnings; use Digest::SHA qw(sha256_hex); use Time::HiRes;
use PVE::Tools; use PVE::LXC::Config; use PVE::QemuConfig;
my ($vmid,$path,$expected,$kind,$value,$cores,$deadline)=@ARGV;
my $class=$kind eq 'lxc' ? 'PVE::LXC::Config' : 'PVE::QemuConfig';
$class->lock_config($vmid, sub {
    die "configuration changed\n" if sha256_hex(PVE::Tools::file_get_contents($path)) ne $expected;
    my $conf=$class->load_config($vmid); $class->check_lock($conf);
    if ($kind eq 'lxc') {
        die "cores changed\n" if ($conf->{cores}//0) != $cores;
        die "foreign cpuset\n" if $class->has_lxc_entry($conf,'lxc.cgroup2.cpuset.cpus')
            || $class->has_lxc_entry($conf,'lxc.cgroup.cpuset.cpus');
        push $conf->{lxc}->@*, ['lxc.cgroup2.cpuset.cpus',$value];
    } else {
        die "foreign hook\n" if defined($conf->{hookscript});
        $conf->{hookscript}=$value;
    }
    die "execution window expired\n" if Time::HiRes::time() >= $deadline;
    $class->write_config($vmid,$conf);
});
'''
    command(['perl', '-e', prefix, str(guest['vmid']), path, guest['config_sha256'],
             'lxc' if cpuset is not None else 'qemu', cpuset or HOOK_VOLUME, str(guest['cores'] or 0),
             str(deadline_epoch)])


def add_snippets(before, deadline_epoch):
    """Add one content capability under the native shared storage lock/CAS."""
    need(INSTALL_REVIEWED, 'CPU installation guard is closed')
    source = r'''
use strict; use warnings; use PVE::Storage; use PVE::Tools;
use Digest::SHA qw(sha256_hex); use Time::HiRes;
my ($expected_sha,$digest,$deadline)=@ARGV;
PVE::Storage::lock_storage_config(sub {
    die "storage changed\n" if sha256_hex(PVE::Tools::file_get_contents('/etc/pve/storage.cfg')) ne $expected_sha;
    my $cfg=PVE::Storage::config();
    die "storage digest changed\n" if $cfg->{digest} ne $digest;
    my $local=$cfg->{ids}->{local};
    die "foreign storage\n" if $local->{type} ne 'dir' || $local->{path} ne '/var/lib/vz';
    die "foreign content\n" if join(',',sort keys $local->{content}->%*) ne 'backup,import,iso,vztmpl';
    $local->{content}->{snippets}=1;
    die "execution window expired\n" if Time::HiRes::time() >= $deadline;
    PVE::Storage::write_config($cfg);
}, 'CPU hook snippets content');
'''
    command(['perl', '-e', source, before['storage_config_sha256'], before['storage']['digest'], str(deadline_epoch)])


def install(c, before, admission_raw, program_raw, authority_raw):
    need(INSTALL_REVIEWED and APPLY_REVIEWED, 'CPU installation/application guards are closed')
    validate_observation(c, before)
    record = approval(admission_raw, c, program_raw)
    deadline = datetime.fromisoformat(record['window_end_utc']).timestamp()
    def still_approved():
        need(approval(admission_raw, c, program_raw) == record, 'Execution approval changed')
    need(sha(canonical(load_json(authority_raw))) == record['authority_sha256'], 'Approval authority link differs')
    # This caller must have inspected the exact authenticated user approval.
    # The protected record is an authorization link, never a native fact proof.
    need(not POLICY_PATH.parent.exists() and not PROGRAM_PATH.exists() and not HOOK_PATH.exists(),
         'An installed policy/program/hook already owns this path')
    permitted_existing_dropins(before)
    need(not os.path.lexists('/etc/systemd/system/multi-user.target.wants/pickle-cpu-isolation.service'),
         'An existing enablement link has another owner')
    previous_partition = before['cgroups']['lxc']
    need(previous_partition['cpuset.cpus.partition'] == 'member' and
         not previous_partition['cpuset.cpus.exclusive'], 'An existing CPU partition has another owner')
    manifest, files = render(c, before['topology'], program_raw)
    need(all(not Path(path).exists() for path in files), 'Candidate control file already exists')
    for g in before['guests']:
        need(g['hookscript'] is None if g['kind'] == 'qemu' else g['cpuset'] is None,
             'An existing hook/cpuset has another owner')
    store = storage_projection(before['storage'])
    need(store.get('type') == 'dir' and store.get('path') == '/var/lib/vz' and
         set(store['content'].split(',')) in ({'backup', 'vztmpl', 'import', 'iso'},
                                             {'backup', 'vztmpl', 'import', 'iso', 'snippets'}),
         'Local storage layout differs; do not replace it')
    need(type(store.get('digest')) is str and re.fullmatch('[0-9a-f]{40}', store['digest']),
         'Storage CAS digest is absent')
    current = observe(c)
    need(current['guests'] == before['guests'] and current['storage'] == store and
         current['storage_config_sha256'] == before['storage_config_sha256'] and
         current['systemd_dropins'] == before['systemd_dropins'] and
         current['systemd_dropin_metadata'] == before['systemd_dropin_metadata'] and
         current['boot_id'] == before['boot_id'],
         'Native custody changed after collection')
    backup = Path('/var/backups') / ('pickle-cpu-isolation-' + record['nonce'])
    trusted_directory(backup.parent)
    still_approved()
    backup.mkdir(mode=0o700); os.chmod(backup, 0o700)
    exclusive(backup / 'attempt-started.json', canonical({'node': c['node'], 'nonce': record['nonce'],
               'policy_sha256': manifest['policy_sha256'], 'program_sha256': sha(program_raw),
               'approval_sha256': sha(admission_raw), 'native_verify_complete': False}))
    exclusive(backup / 'before.json', canonical(before))
    exclusive(backup / 'storage.cfg', read('/etc/pve/storage.cfg'))
    for g in before['guests']:
        path = '/etc/pve/nodes/' + c['node'] + ('/lxc/' if g['kind'] == 'lxc' else '/qemu-server/') + str(g['vmid']) + '.conf'
        raw = read(path)
        need(sha(raw) == g['config_sha256'], 'Guest configuration changed before protected backup')
        exclusive(backup / (g['kind'] + '-' + str(g['vmid']) + '.conf'), raw)
    try:
        still_approved()
        trusted_directory(POLICY_PATH.parent, create=True, mode=0o700)
        for path, raw in files.items():
            still_approved()
            trusted_directory(Path(path).parent, create=True)
            exclusive(path, raw, int(manifest['files'][path]['mode'], 8))
        exclusive(POLICY_PATH.parent / 'approval.json', admission_raw)
        exclusive(POLICY_PATH.parent / 'authority.json', canonical(load_json(authority_raw)))
        exclusive(POLICY_PATH.parent / 'installed-manifest.json', canonical(manifest))
        if 'snippets' not in store['content'].split(','):
            still_approved()
            add_snippets(before, deadline)
        # Reject drift rather than enrolling an unexpected guest.
        platform = manifest['masks']['platform_cts']
        for g in before['guests']:
            still_approved()
            if g['kind'] == 'qemu':
                mutate_guest(c['node'], g, deadline_epoch=deadline)
            elif str(g['vmid']) in platform:
                mutate_guest(c['node'], g, cpuset=platform[str(g['vmid'])], deadline_epoch=deadline)
        after_files = observe(c)
        preserved_dropins(before, after_files, files)
        need(len(after_files['guests']) == len(before['guests']), 'Guest inventory changed during installation')
        old = {(g['kind'], g['vmid']): g for g in before['guests']}
        for g in after_files['guests']:
            previous = old[(g['kind'], g['vmid'])]
            need(g['status'] == previous['status'] and g['cores'] == previous['cores'] and
                 g['onboot'] == previous['onboot'] and g['template'] == previous['template'] and
                 g['affinity'] == previous['affinity'] and
                 g['base_document_sha256'] == previous['base_document_sha256'],
                 'Collateral guest state/configuration changed')
        actual_store = {k: v for k, v in after_files['storage'].items() if k not in ('content', 'digest')}
        need(actual_store == {k: v for k, v in store.items() if k not in ('content', 'digest')} and
             set(after_files['storage']['content'].split(',')) == set(store['content'].split(',')) | {'snippets'},
             'Collateral local storage configuration changed')
        command(['systemd-analyze', 'verify', '/etc/systemd/system/pickle-cpu-isolation.service',
                 'qemu.slice', 'system.slice', 'user.slice', 'init.scope',
                 'pve-container@200.service', 'pve-guests.service'])
        # No guest is stopped/restarted. The native parent masks are changed in
        # the approved window, then independently read back before enabling.
        still_approved()
        apply_parents(c, authorization_check=still_approved)
        verified = verify(c, observe(c), authority_sha256=record['authority_sha256'])
        still_approved()
        command(['systemctl', 'daemon-reload'])
        still_approved()
        command(['systemctl', 'enable', 'pickle-cpu-isolation.service'])
        final = observe(c)
        preserved_dropins(before, final, files)
        verified = verify(c, final, authority_sha256=record['authority_sha256'])
        still_approved()
        exclusive(backup / 'after.json', canonical(final))
        # This receipt records the verified mask/enrollment phase. A separate
        # activation receipt below records successful initial systemd startup.
        exclusive(backup / 'installation-complete.json', canonical(verified))
        still_approved()
        command(['systemctl', 'start', 'pickle-cpu-isolation.service'])
        need(command(['systemctl', 'is-active', 'pickle-cpu-isolation.service']).strip() == 'active',
             'Initial persistent unit is not active')
        final = observe(c)
        preserved_dropins(before, final, files)
        verified = verify(c, final, authority_sha256=record['authority_sha256'])
        still_approved()
        exclusive(backup / 'activation-complete.json', canonical(verified))
        return {'operation': 'install', 'host_or_guest_changed': True,
                'source_actions_performed': False, 'native_readback': verified,
                'persistent_unit_enabled': True, 'initial_unit_start_executed': True,
                'protected_attempt': str(backup)}
    except BaseException:
        exclusive(backup / 'installation-incomplete.json', canonical({'node': c['node'],
                   'native_verify_complete': False, 'partial_effects_possible': True,
                   'automatic_rollback_or_replay_permitted': False}))
        raise


def approval(raw, c, program_raw, *, boot=False):
    record = load_json(raw)
    need(type(record) is dict and set(record) == {'schema_version', 'node', 'policy_sha256',
         'program_sha256', 'user_approval_ref', 'user_approval_sha256', 'window_start_utc',
         'window_end_utc', 'persistent_boot_reapply', 'nonce', 'authority_sha256'}, 'Invalid approval record')
    need(type(record['schema_version']) is int and record['schema_version'] == 1 and
         record['node'] == c['node'] and record['policy_sha256'] == sha(canonical(c)) and
         record['program_sha256'] == sha(program_raw), 'Approval is for another node/policy/program')
    pin(record['user_approval_sha256'])
    pin(record['authority_sha256'])
    need(type(record['user_approval_ref']) is str and 1 <= len(record['user_approval_ref']) <= 512,
         'Actual operator approval reference is absent')
    need(type(record['nonce']) is str and re.fullmatch('[0-9a-f]{32}', record['nonce']), 'Invalid new attempt nonce')
    start = datetime.fromisoformat(record['window_start_utc']); end = datetime.fromisoformat(record['window_end_utc'])
    need(start.utcoffset() is not None and end.utcoffset() is not None and 0 < (end - start).total_seconds() <= 7200,
         'Invalid explicit execution window')
    need(record['persistent_boot_reapply'] is True, 'Persistent boot application is not approved')
    if not boot:
        need(start <= datetime.now(timezone.utc) < end, 'Outside the separately approved execution window')
    return record


def completed_phase(c, expected, record, program, installation, activation, *, allow_initial=False):
    """Admit a completed persistent profile; bootstrap stays inside its window."""
    for receipt in (installation,activation):
        if receipt is None:
            continue
        need(type(receipt) is dict, 'Invalid actual completion receipt')
        for key,value in {'producer':'pve-cpu-isolation-native-v1','node':c['node'],
            'policy_sha256':expected['policy_sha256'],'program_sha256':sha(program),
            'authority_sha256':record['authority_sha256'],
            'topology_sha256':expected['masks']['topology_sha256'], 'partition':'root',
            'userland_masks_equal':True,'known_qemu_and_templates_enrolled':True,
            'host_or_guest_changed':False}.items():
            need(receipt.get(key)==value and type(receipt.get(key)) is type(value),
                 'Actual completion receipt binding/scope differs')
        seen=datetime.fromisoformat(receipt['observed_at_utc'])
        need(datetime.fromisoformat(record['window_start_utc'])<=seen<
             datetime.fromisoformat(record['window_end_utc']), 'Initial completion was outside its approved window')
    need(installation is not None, 'Verified installation receipt is absent')
    if activation is not None:
        return False
    need(allow_initial, 'Initial activation is incomplete; runtime admission is closed')
    approval(canonical(record),c,program,boot=False)
    return True


def installed_custody(c, *, allow_initial=False):
    """Bind every installed candidate byte to its protected full manifest."""
    program = read(PROGRAM_PATH, protected=True)
    expected, files = render(c, native_topology(), program)
    actual = load_json(read(POLICY_PATH.parent / 'installed-manifest.json', protected=True))
    need(actual == expected, 'Installed manifest differs from actual policy/program/topology')
    for path, raw in files.items():
        need(read(path, protected=True) == raw, 'Installed candidate bytes drifted')
        st = Path(path).lstat()
        need(stat.S_IMODE(st.st_mode) == int(expected['files'][path]['mode'], 8), 'Installed mode differs')
    record = approval(read(POLICY_PATH.parent / 'approval.json', protected=True), c, program, boot=True)
    need(sha(read(POLICY_PATH.parent / 'authority.json', protected=True)) == record['authority_sha256'],
         'Protected authority bytes differ')
    attempt = Path('/var/backups') / ('pickle-cpu-isolation-' + record['nonce'])
    need((attempt / 'installation-complete.json').is_file() and
         not (attempt / 'installation-incomplete.json').exists(), 'Incomplete installation is not admissible')
    installation = load_json(read(attempt / 'installation-complete.json', protected=True))
    activation = attempt / 'activation-complete.json'
    if activation.exists():
        activated = load_json(read(activation, protected=True))
    else:
        activated=None
    return completed_phase(c,expected,record,program,installation,activated,allow_initial=allow_initial)


def write_cgroup(group, name, value):
    path = CG / group / name
    need(path.exists(), 'Required cgroup control is absent')
    with path.open('w') as stream:
        stream.write(value + '\n')


def apply_parents(c, *, authorization_check=None):
    need(APPLY_REVIEWED, 'CPU application guard is closed')
    masks = derive(c, native_topology())
    def check():
        if authorization_check is not None:
            authorization_check()
    for name in (*CONTROL_NAMES, 'lxc', 'qemu.slice'):
        check()
        (CG / name).mkdir(exist_ok=True)
    need(cgroup_state('lxc')['direct_process_count'] == 0, 'LXC parent has direct processes')
    for name in CONTROL_NAMES:
        check(); write_cgroup(name, 'cpuset.cpus', masks['host'])
    for group, field, value in [('qemu.slice', 'cpuset.cpus', masks['student']),
        ('qemu.slice','cgroup.subtree_control','+cpuset'), ('lxc','cpuset.cpus',masks['platform']),
        ('lxc','cpuset.cpus.exclusive',masks['platform']), ('lxc','cgroup.subtree_control','+cpuset'),
        ('lxc','cpuset.cpus.partition','root')]:
        check(); write_cgroup(group,field,value)
    for vmid, value in masks['platform_cts'].items():
        base = 'lxc/' + vmid
        if (CG / base).exists():
            check(); write_cgroup(base, 'cpuset.cpus', value)
            check(); write_cgroup(base, 'cgroup.subtree_control', '+cpuset')
            check(); write_cgroup(base + '/ns', 'cpuset.cpus', value)


def native_start(c, kind, vmid):
    whole(vmid, 100)
    installed_custody(c)
    need(command(['systemctl','is-active','pickle-cpu-isolation.service']).strip()=='active',
         'Persistent CPU unit is not active')
    report = observe(c)
    verify(c, report, authority_sha256=sha(read(POLICY_PATH.parent / 'authority.json', protected=True)))
    if kind == 'lxc':
        need(vmid in {x['vmid'] for x in c['platform_cts']}, 'Only classified platform CTs may start')
    else:
        need(any(g['kind'] == 'qemu' and g['vmid'] == vmid and not g['template']
                 and g['hookscript'] == HOOK_VOLUME for g in report['guests']), 'QEMU caller is not enrolled')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['collect', 'render', 'check', 'install', 'boot-apply',
                                         'qemu-pre-start', 'lxc-pre-start'])
    parser.add_argument('--policy', type=Path, default=POLICY_PATH)
    parser.add_argument('--policy-sha256')
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--inventory-sha256')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--admission', type=Path)
    parser.add_argument('--authority', type=Path)
    parser.add_argument('--vmid', type=int)
    args = parser.parse_args()
    if args.mode == 'install':
        need(INSTALL_REVIEWED and APPLY_REVIEWED, 'CPU installation/application guards are closed')
    if args.mode == 'boot-apply':
        need(APPLY_REVIEWED, 'CPU application guard is closed')
    raw = read(args.policy, protected=args.mode != 'render')
    c = validate_config(load_json(raw))
    if args.policy_sha256:
        need(sha(raw) == pin(args.policy_sha256), 'Policy file pin differs')
    if args.mode == 'install':
        need(args.inventory is not None and args.inventory_sha256 and args.admission is not None and args.authority,
             'Installation requires pinned native before-state and an approval record')
        source = read(args.inventory, protected=True)
        need(sha(source) == pin(args.inventory_sha256), 'Native before-state pin differs')
        print(json.dumps(install(c, load_json(source), read(args.admission, protected=True),
                                 read(__file__, protected=True), read(args.authority, protected=True))))
    elif args.mode == 'render':
        need(args.inventory is not None and args.inventory_sha256 and args.output, 'Render requires pinned inventory/new directory')
        source = read(args.inventory)
        need(sha(source) == pin(args.inventory_sha256), 'Inventory pin differs')
        report = load_json(source)
        validate_observation(c, report)
        manifest, files = render(c, report['topology'], read(__file__))
        args.output.mkdir(mode=0o700)
        os.chmod(args.output, 0o700)
        for path, content in files.items():
            destination = args.output / path.lstrip('/')
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            exclusive(destination, content, int(manifest['files'][path]['mode'], 8))
        exclusive(args.output / 'manifest.json', canonical(manifest))
        print(json.dumps(manifest))
    elif args.mode == 'collect':
        need(args.output is not None, 'Collector requires a new output file')
        report = observe(c)
        exclusive(args.output, canonical(report))
        print(json.dumps({'output_sha256': sha(canonical(report)), 'host_or_guest_changed': False}))
    elif args.mode == 'check':
        installed_custody(c)
        need(command(['systemctl','is-active','pickle-cpu-isolation.service']).strip()=='active',
             'Persistent CPU unit is not active')
        report = observe(c)
        result = verify(c, report, authority_sha256=sha(read(POLICY_PATH.parent / 'authority.json', protected=True)))
        if args.output:
            exclusive(args.output, canonical(report))
        print(json.dumps(result))
    elif args.mode in ('qemu-pre-start', 'lxc-pre-start'):
        native_start(c, 'qemu' if args.mode.startswith('qemu') else 'lxc', args.vmid)
    else:
        saved = read('/etc/pickle/cpu-isolation/approval.json', protected=True)
        approval(saved, c, read(__file__, protected=True), boot=True)
        initial = installed_custody(c,allow_initial=True)
        record = approval(saved, c, read(__file__, protected=True), boot=True)
        previous = Path('/var/backups') / ('pickle-cpu-isolation-' + record['nonce'])
        need((previous / 'installation-complete.json').is_file() and
             not (previous / 'installation-incomplete.json').exists(), 'Incomplete initial install is not replayable at boot')
        report = observe(c)
        validate_observation(c, report)
        attempt = Path('/var/lib') / ('pickle-cpu-isolation-boot-' + report['boot_id'])
        trusted_directory(attempt.parent)
        attempt.mkdir(mode=0o700); os.chmod(attempt, 0o700)
        exclusive(attempt / 'before.json', canonical(report))
        try:
            def initial_authorization():
                approval(saved,c,read(__file__,protected=True),boot=False)
            apply_parents(c,authorization_check=initial_authorization if initial else None)
            final = observe(c)
            result = verify(c, final, authority_sha256=record['authority_sha256'])
            if initial:
                initial_authorization()
            exclusive(attempt / 'after.json', canonical(final))
            exclusive(attempt / 'complete.json', canonical(result))
            print(json.dumps(result))
        except BaseException:
            exclusive(attempt / 'incomplete.json', canonical({'partial_effects_possible': True,
                       'automatic_replay_permitted': False, 'native_verify_complete': False}))
            raise


if __name__ == '__main__':
    try:
        main()
    except (IsolationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Neither native command stderr nor private configuration values escape.
        print('cpu-isolation: precondition failed; preserve the attempt and inspect protected host records', file=sys.stderr)
        raise SystemExit(1)
