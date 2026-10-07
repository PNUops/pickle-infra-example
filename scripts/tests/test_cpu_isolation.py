#!/usr/bin/env python3
"""Meaningful offline refusal tests for whole-SMT boundaries and start gates."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import cpu_isolation as c


def policy(cores=12):
    return {'schema_version': 1, 'node': 'node-example', 'physical_cores': cores,
            'host_core_ids': [0, 1], 'platform_core_ids': [2, 3, 4, 5],
            'platform_cts': [{'vmid': 200, 'cores': 2, 'core_ids': [2]},
                             {'vmid': 201, 'cores': 4, 'core_ids': [3, 4]}],
            'parked_ct_ids': [], 'committed_vcpu': 5 if cores == 12 else 3,
            'expected_kernel': '7.0.14-15-pve', 'expected_pve_release': '9.2.11',
            'expected_systemd_major': 257, 'required_code_sha256': {p: 'a' * 64 for p in c.CODE_PATHS},
            'clone_inheritance_review_ref': 'fixture code-only review; no native approval'}


def topology(cores=12):
    return [{'cpu': cpu, 'core': cpu % cores, 'socket': 0, 'online': True,
             'siblings': f'{cpu % cores},{cpu % cores + cores}'} for cpu in range(cores * 2)]


def observation(config=None):
    config = config or policy()
    masks = c.derive(config, topology(config['physical_cores']))
    group = lambda x: {'cpuset.cpus': x, 'cpuset.cpus.effective': x,
                        'cpuset.cpus.exclusive': x, 'cpuset.cpus.exclusive.effective': x,
                        'cpuset.cpus.partition': 'member', 'direct_process_count': 0,
                        'cgroup.subtree_control': 'cpuset', 'thread_cpu_sets': []}
    groups = {name: group(masks['host']) for name in c.CONTROL_NAMES}
    groups['lxc'] = group(masks['platform']); groups['lxc']['cpuset.cpus.partition'] = 'root'
    groups['qemu.slice'] = group(masks['student'])
    guests = []
    for ct in config['platform_cts']:
        mask = masks['platform_cts'][str(ct['vmid'])]
        guests.append({'kind': 'lxc', 'vmid': ct['vmid'], 'cores': ct['cores'], 'status': 'running',
                       'onboot': 0, 'template': False, 'cpuset': mask, 'hookscript': None})
        for name in ('lxc/' + str(ct['vmid']), 'lxc/' + str(ct['vmid']) + '/ns'):
            groups[name] = group(mask)
            groups[name]['thread_cpu_sets'] = [mask]
    guests.append({'kind': 'qemu', 'vmid': 100001, 'status': 'running', 'template': False,
                   'affinity': None, 'hookscript': c.HOOK_VOLUME})
    return {'node': config['node'], 'kernel': config['expected_kernel'], 'pve_release': '9.2.11',
            'systemd_major': 257, 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'boot_id': '935dd91e-dca3-4e23-80fc-c28597b9a8c9', 'topology': topology(config['physical_cores']),
            'masks': masks, 'required_code_sha256': config['required_code_sha256'],
            'guests': guests, 'cgroups': groups,
            'installed_files': {'program_sha256': 'b' * 64, 'hook_sha256': c.sha(c.hook_bytes('b' * 64))},
            'host_or_guest_changed': False,
            'inventory': {'scope_vmids': [100001], 'qemu_process_vmids': [100001],
                          'vhost_scan_complete': True, 'thread_scan_complete': True,
                          'threads': [{'vmid': 100001, 'tid': 17, 'start_ticks': 8192,
                                       'kind': 'qemu', 'cgroup': '/qemu.slice/100001.scope',
                                       'allowed_cpus': masks['student']}]}}


class CpuIsolationTests(unittest.TestCase):
    def test_file_verify_excludes_synthesized_init_scope_without_removing_runtime_scope_controls(self):
        def native_verify_model(argv):
            if 'init.scope' in argv:
                raise c.IsolationError('Unit init.scope not found.')
            return ''
        with patch.object(c, 'command', side_effect=native_verify_model) as run:
            c.verify_control_units()
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ['systemd-analyze', 'verify'])
        self.assertIn('qemu.slice', args)
        self.assertIn('pve-container@200.service', args)
        _, files = c.render(policy(), topology(), b'closed CPU program model')
        self.assertIn(b'[Scope]\nAllowedCPUs=', files['/etc/systemd/system/init.scope.d/90-pickle-cpu-isolation.conf'])
        self.assertIn('init.scope', c.CONTROL_NAMES)
        bad = observation()
        bad['cgroups']['init.scope']['cpuset.cpus.effective'] = '0-23'
        with self.assertRaises(c.IsolationError):
            c.verify(policy(), bad, authority_sha256='d' * 64)

    def test_storage_capability_order_is_canonical_without_hiding_real_config_drift(self):
        before = {'type': 'dir', 'path': '/var/lib/vz', 'content': 'import,iso,vztmpl,backup',
                  'digest': 'a' * 40, 'disable': 0}
        reordered = before | {'content': 'backup,vztmpl,iso,import'}
        self.assertEqual(c.storage_projection(before), c.storage_projection(reordered))
        self.assertEqual(before['content'], 'import,iso,vztmpl,backup')
        self.assertEqual(c.storage_projection(before)['content'], 'backup,import,iso,vztmpl')
        with_snippets = before | {'content': 'snippets,backup,import,iso,vztmpl'}
        self.assertEqual(c.storage_projection(with_snippets)['content'], 'backup,import,iso,snippets,vztmpl')
        for key, value in (('digest', 'b' * 40), ('path', '/foreign'), ('disable', 1)):
            self.assertNotEqual(c.storage_projection(before), c.storage_projection(reordered | {key: value}))

    def test_storage_capabilities_reject_duplicate_missing_extra_and_untyped_values(self):
        base = {'type': 'dir', 'path': '/var/lib/vz', 'digest': 'a' * 40}
        for value in ('backup,import,iso', 'backup,import,iso,vztmpl,images',
                      'backup,backup,import,iso,vztmpl', 'backup,import,iso,vztmpl,',
                      'backup, import,iso,vztmpl', None, ['backup', 'import', 'iso', 'vztmpl'], True):
            with self.subTest(value=value), self.assertRaises(c.IsolationError):
                c.storage_projection(base | {'content': value})

    def existing_dropins(self):
        controls = {group: {} for group in c.DROPIN_GROUPS}
        metadata = {group: {} for group in c.DROPIN_GROUPS}
        name = 'example-production-network.conf'
        controls['pve-guests.service'][name] = c.EXISTING_DROPINS['pve-guests.service'][name]
        metadata['pve-guests.service'][name] = {'uid': 0, 'gid': 0, 'mode': 0o600, 'nlink': 1,
            'dev': 1, 'ino': 22, 'mtime_ns': 33, 'ctime_ns': 44}
        return {'systemd_dropins': controls, 'systemd_dropin_metadata': metadata}

    def test_only_exact_existing_network_override_with_original_custody_is_admitted(self):
        before = self.existing_dropins()
        c.permitted_existing_dropins(before)
        for edit in (lambda r: r['systemd_dropins']['pve-guests.service'].update({'foreign.conf': 'a' * 64}),
                     lambda r: r['systemd_dropins']['pve-guests.service'].update({'example-production-network.conf': 'a' * 64}),
                     lambda r: r['systemd_dropin_metadata']['pve-guests.service']['example-production-network.conf'].update(mode=0o644),
                     lambda r: r['systemd_dropin_metadata']['pve-guests.service']['example-production-network.conf'].update(nlink=2),
                     lambda r: r['systemd_dropin_metadata']['pve-guests.service']['example-production-network.conf'].update(uid=False)):
            bad = copy.deepcopy(before); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.permitted_existing_dropins(bad)

    def test_cpu_install_preserves_network_override_and_rejects_deleted_replaced_or_extra_files(self):
        before = self.existing_dropins()
        _, files = c.render(policy(), topology(), b'closed CPU program model')
        after = copy.deepcopy(before)
        for group in c.DROPIN_GROUPS:
            path = '/etc/systemd/system/' + group + '.d/' + c.CPU_DROPIN
            after['systemd_dropins'][group][c.CPU_DROPIN] = c.sha(files[path])
            after['systemd_dropin_metadata'][group][c.CPU_DROPIN] = {'uid': 0, 'mode': 0o644}
        c.preserved_dropins(before, after, files)
        for edit in (lambda r: r['systemd_dropins']['pve-guests.service'].pop('example-production-network.conf'),
                     lambda r: r['systemd_dropins']['pve-guests.service'].update({'example-production-network.conf': 'b' * 64}),
                     lambda r: r['systemd_dropin_metadata']['pve-guests.service']['example-production-network.conf'].update(ino=23),
                     lambda r: r['systemd_dropins']['qemu.slice'].update({'foreign.conf': 'a' * 64}),
                     lambda r: r['systemd_dropins']['qemu.slice'].update({c.CPU_DROPIN: 'a' * 64})):
            bad = copy.deepcopy(after); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.preserved_dropins(before, bad, files)

    def test_budget_protects_whole_host_and_platform_pairs_before_sharing(self):
        for physical, expected in ((12, 19), (16, 37)):
            result = c.derive(policy(physical), topology(physical))
            self.assertEqual(result['allocatable_vcpu'], expected)
            self.assertEqual(len(c.cpus(result['host'])), 4)
            self.assertEqual(len(c.cpus(result['platform'])), 8)
            self.assertFalse(c.cpus(result['platform']) & c.cpus(result['student']))

    def test_mixed_missing_offline_duplicate_and_multisocket_smt_are_refused(self):
        for edit in (lambda x: x[2].update(siblings='2,15'), lambda x: x.pop(),
                     lambda x: x[2].update(online=False), lambda x: x[2].update(socket=1),
                     lambda x: x[2].update(cpu=1), lambda x: x[2].update(core=3)):
            bad = topology(); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.derive(policy(), bad)

    def test_overlap_nonwhole_cores_unknown_keys_and_bool_inputs_are_refused(self):
        for edit in (lambda x:x.update(platform_core_ids=[1, 2, 3, 4]),
                     lambda x:x['platform_cts'][0].update(core_ids=[0]),
                     lambda x:x['platform_cts'][0].update(cores=4),
                     lambda x:x.update(committed_vcpu=True), lambda x:x.update(unknown=1),
                     lambda x:x.update(expected_systemd_major=True),
                     lambda x:x.update(parked_ct_ids=[200]), lambda x:x.update(expected_kernel='6.8.0')):
            bad = policy(); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.validate_config(bad)

    def test_native_summary_is_bound_to_full_observation_and_has_no_io_claim(self):
        report = observation()
        result = c.verify(policy(), report, authority_sha256='d'*64)
        self.assertEqual(result['observation_sha256'], c.sha(c.canonical(report)))
        self.assertFalse(result['irq_and_kernel_workers_verified'])
        self.assertFalse(result['global_hook_for_arbitrary_unenrolled_qemu'])
        self.assertFalse(result['performance_or_full_protection_claim'])

    def test_stale_runtime_pins_topology_digest_and_foreign_guest_class_fail(self):
        for edit in (lambda x:x.update(observed_at_utc=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()),
                     lambda x:x.update(kernel='7.1.0'), lambda x:x['required_code_sha256'].update({c.CODE_PATHS[0]:'c'*64}),
                     lambda x:x['masks'].update(host='0,1,2,3'),
                     lambda x:x['guests'][0].update(vmid=299), lambda x:x['guests'][0].update(cores=4)):
            bad = copy.deepcopy(observation()); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.verify(policy(), bad, authority_sha256='d'*64)

    def test_invalid_degraded_exclusive_effective_and_thread_escape_fail(self):
        for edit in (lambda x:x['cgroups']['lxc'].update({'cpuset.cpus.partition':'root invalid (hotplug)'}),
                     lambda x:x['cgroups']['lxc'].update({'cpuset.cpus.partition':'isolated'}),
                     lambda x:x['cgroups']['lxc'].update({'cpuset.cpus.exclusive.effective':'2,3'}),
                     lambda x:x['cgroups']['qemu.slice'].update({'cpuset.cpus.effective':'0,1'}),
                     lambda x:x['cgroups']['system.slice'].update({'cpuset.cpus':'0,1'}),
                     lambda x:x['cgroups']['lxc/200/ns'].update({'cpuset.cpus.effective':'0,1'}),
                     lambda x:x['cgroups']['lxc/200'].update(thread_cpu_sets=[]),
                     lambda x:x['inventory']['threads'][0].update(kind='vhost'),
                     lambda x:x['inventory']['threads'][0].update(allowed_cpus='2,14')):
            bad=observation(); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.verify(policy(),bad, authority_sha256='d'*64)

    def test_missing_foreign_hooks_and_duplicate_incomplete_tids_fail(self):
        for edit in (lambda x:x['guests'][-1].update(hookscript=None),
                     lambda x:x['installed_files'].update(hook_sha256='c'*64),
                     lambda x:x['inventory'].update(vhost_scan_complete=False),
                     lambda x:x['inventory'].update(scope_vmids=[]),
                     lambda x:x['inventory'].update(threads=[]),
                     lambda x:x['inventory']['threads'].append(copy.deepcopy(x['inventory']['threads'][0]))):
            bad=observation(); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(c.IsolationError): c.verify(policy(),bad, authority_sha256='d'*64)

    def test_boot_and_per_start_hooks_are_distinct_and_original_guards_are_closed(self):
        manifest, files=c.render(policy(),topology(),b'guarded program fixture')
        self.assertIn(b'AllowedCPUs=',files['/etc/systemd/system/qemu.slice.d/90-pickle-cpu-isolation.conf'])
        self.assertIn(b'lxc-pre-start --vmid %i',files['/etc/systemd/system/pve-container@.service.d/90-pickle-cpu-isolation.conf'])
        self.assertIn(b"sys.argv[2]=='pre-start'",files[str(c.HOOK_PATH)])
        self.assertIn(c.sha(b'guarded program fixture').encode(),files[str(c.HOOK_PATH)])
        self.assertFalse(c.INSTALL_REVIEWED);self.assertFalse(c.APPLY_REVIEWED)
        with self.assertRaises(c.IsolationError): c.apply_parents(policy())
        with self.assertRaises(c.IsolationError): c.install(policy(),{},b'',b'',b'')

    def test_exclusive_candidate_preserves_existing_and_never_replays(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'candidate'
            c.exclusive(p,b'original')
            with self.assertRaises(FileExistsError): c.exclusive(p,b'replacement')
            self.assertEqual(p.read_bytes(),b'original')

    def test_mutator_has_native_lock_file_cas_and_no_source_start(self):
        g={'vmid':200,'config_sha256':'c'*64,'cores':2}
        with patch.object(c,'INSTALL_REVIEWED',True),patch.object(c,'command',return_value='') as run:
            c.mutate_guest('node-example',g,cpuset='2,14',deadline_epoch=9999999999)
        argv=run.call_args[0][0]
        self.assertIn('lock_config',argv[2]);self.assertIn('sha256_hex',argv[2])
        self.assertIn('foreign cpuset',argv[2]);self.assertNotIn('start',argv[2])
        self.assertIn('Time::HiRes::time() >= $deadline',argv[2])

    def test_window_is_rechecked_before_each_parent_effect(self):
        calls=[]
        def authorization():
            calls.append('check')
            if len(calls)==9: raise c.IsolationError('expired fixture window')
        with patch.object(c,'APPLY_REVIEWED',True), patch.object(c,'native_topology',return_value=topology()), \
             patch.object(c,'CG') as cg, patch.object(c,'cgroup_state',return_value={'direct_process_count':0}), \
             patch.object(c,'write_cgroup') as write:
            with self.assertRaises(c.IsolationError): c.apply_parents(policy(),authorization_check=authorization)
        self.assertEqual(write.call_count,1)
        self.assertEqual(write.call_args[0][:2],('system.slice','cpuset.cpus'))

    def test_storage_effect_checks_cas_and_deadline_inside_shared_lock(self):
        before={'storage_config_sha256':'a'*64,'storage':{'digest':'b'*40}}
        with patch.object(c,'INSTALL_REVIEWED',True),patch.object(c,'command',return_value='') as run:
            c.add_snippets(before,9999999999)
        source=run.call_args[0][0][2]
        self.assertIn('lock_storage_config',source)
        self.assertIn('Time::HiRes::time() >= $deadline',source)
        self.assertIn("$local->{content}->{snippets}=1",source)
        self.assertNotIn('delete',source)

    def test_complete_census_includes_kernel_helpers_without_exe_and_refuses_disappearance(self):
        real_path=Path
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cg=root/'cgroup';proc=root/'proc'
            (cg/'qemu.slice/100001.scope').mkdir(parents=True)
            (cg/'qemu.slice/100001.scope/cgroup.threads').write_text('17\n')
            for pid,comm,group in ((17,'kvm','/qemu.slice/100001.scope'),(23,'vhost-17','/system.slice')):
                base=proc/str(pid);task=base/'task'/str(pid);task.mkdir(parents=True)
                (base/'stat').write_text(str(pid)+' (fixture) '+' '.join(['S']+['0']*18+['8192']))
                (task/'comm').write_text(comm+'\n');(task/'cgroup').write_text('0::'+group+'\n')
            def path(value):
                text=str(value)
                return proc if text=='/proc' else proc/text[6:] if text.startswith('/proc/') else real_path(value)
            def executable(value):
                if '/17/exe' in str(value): return '/usr/bin/kvm'
                raise FileNotFoundError('kernel helper has no exe')
            with patch.object(c,'CG',cg),patch.object(c,'Path',side_effect=path), \
                 patch.object(c.os,'readlink',side_effect=executable), \
                 patch.object(c.os,'sched_getaffinity',return_value={6,18},create=True):
                with self.assertRaisesRegex(c.IsolationError,'vhost helper'): c.qemu_inventory()
                (proc/'23/task/23/comm').write_text('fixture-host\n')
                result=c.qemu_inventory()
                self.assertEqual(result['threads'][0]['allowed_cpus'],'6,18')
                (proc/'17/task/17/comm').unlink()
                with self.assertRaisesRegex(c.IsolationError,'disappeared'): c.qemu_inventory()

    def test_completed_profile_survives_window_expiry_but_initial_boot_does_not(self):
        config=policy();program=b'closed fixture program'
        expected,_=c.render(config,topology(),program)
        now=datetime.now(timezone.utc)
        record={'schema_version':1,'node':config['node'],'policy_sha256':expected['policy_sha256'],
            'program_sha256':c.sha(program),'authority_sha256':'d'*64,
            'user_approval_ref':'fixture only; not an operator approval','user_approval_sha256':'e'*64,
            'window_start_utc':(now-timedelta(hours=2)).isoformat(),
            'window_end_utc':(now-timedelta(hours=1)).isoformat(),
            'persistent_boot_reapply':True,'nonce':'1'*32}
        installation=c.verify(config,observation(),authority_sha256='d'*64)
        installation['program_sha256']=c.sha(program)
        installation['observed_at_utc']=(now-timedelta(hours=1,minutes=30)).isoformat()
        activation=copy.deepcopy(installation)
        self.assertFalse(c.completed_phase(config,expected,record,program,installation,activation))
        with self.assertRaises(c.IsolationError):
            c.completed_phase(config,expected,record,program,installation,None)
        with self.assertRaises(c.IsolationError):
            c.completed_phase(config,expected,record,program,installation,None,allow_initial=True)
        with self.assertRaises(c.IsolationError):
            c.completed_phase(config,expected,record,program,None,None,allow_initial=True)
        activation['producer']='manual-claim'
        with self.assertRaises(c.IsolationError):
            c.completed_phase(config,expected,record,program,installation,activation)


if __name__=='__main__': unittest.main()
