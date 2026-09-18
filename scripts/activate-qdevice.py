#!/usr/bin/env python3
"""Activate an already enrolled two-node qdevice without SSH to the witness."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import time

# The same lock and atomic writer are used by the installed pvecm implementation.
# The digest, membership and vote checks run again inside that lock.
CONFIG_PROGRAM = r'''
use strict; use warnings;
use Digest::SHA qw(sha256_hex);
use JSON::PP qw(encode_json);
use PVE::Cluster; use PVE::Corosync; use PVE::SSHInfo;
my ($cluster, $names, $host, $digest, $apply) = @ARGV;
my @expected = sort split(/,/, $names);
sub read_state {
    PVE::Cluster::cfs_update(1);
    PVE::Cluster::check_cfs_quorum();
    my $conf = PVE::Cluster::cfs_read_file('corosync.conf');
    die "Unexpected cluster\n" unless $conf->{main}->{totem}->{cluster_name} eq $cluster;
    my $nodes = PVE::Corosync::nodelist($conf);
    my $members = PVE::Cluster::get_members();
    die "Unexpected configured nodes\n" unless join(',', sort keys %$nodes) eq join(',', @expected);
    die "Unexpected online members\n" unless join(',', sort keys %$members) eq join(',', @expected);
    for my $name (@expected) {
        die "Offline node\n" unless $members->{$name}->{online};
        die "Node votes must be one\n" unless ($nodes->{$name}->{quorum_votes} // 1) == 1;
    }
    my $quorum = $conf->{main}->{quorum};
    die "Unexpected quorum provider\n" unless $quorum->{provider} eq 'corosync_votequorum';
    die "Existing qdevice or quorum override\n" unless join(',', sort keys %$quorum) eq 'provider';
    open(my $fh, '<', '/etc/pve/corosync.conf') or die "Cannot read cluster config\n";
    local $/; my $bytes = <$fh>; close($fh);
    return ($conf, $members, sha256_hex($bytes));
}
my ($conf, $members, $before) = read_state();
my %ssh;
for my $name (@expected) {
    die "Missing cluster address\n" unless $members->{$name}->{ip};
    $ssh{$name} = PVE::SSHInfo::ssh_info_to_command(
        {ip => $members->{$name}->{ip}, name => $name},
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
        '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2');
}
if ($apply eq '1') {
    my $code = sub {
        my ($fresh, undef, $actual) = read_state();
        die "Cluster configuration changed\n" unless $actual eq $digest;
        $fresh->{main}->{quorum}->{device} = {
            model => 'net', votes => 1,
            net => {tls => 'required', host => $host, algorithm => 'ffsplit'},
        };
        PVE::Corosync::atomic_write_conf($fresh);
    };
    PVE::Cluster::cfs_lock_file('corosync.conf', 10, $code);
    die $@ if $@;
}
print encode_json({sha256 => $before, ssh => \%ssh,
    config_version => $conf->{main}->{totem}->{config_version}, applied => $apply eq '1' ? JSON::PP::true : JSON::PP::false});
'''

PROBE_PROGRAM = r'''
import hashlib,json,os,pathlib,re,socket,stat,subprocess,sys
node,cluster,witness,ca_hash,cert_hash=sys.argv[1:]
def run(args):
    p=subprocess.run(args,capture_output=True,timeout=15)
    if p.returncode: raise RuntimeError('prerequisite command failed: '+args[0])
    return p.stdout
assert socket.gethostname().split('.')[0]==node, 'unexpected node identity'
base=pathlib.Path('/etc/corosync/qdevice/net/nssdb')
assert base.is_dir() and not base.is_symlink(), 'NSS directory missing'
for name in ('key4.db','pwdfile.txt'):
    p=base/name; s=p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_mode&0o077==0, 'private NSS permissions'
db='sql:'+str(base); password=str(base/'pwdfile.txt')
ca=run(['certutil','-L','-d',db,'-n','QNet CA','-r'])
cert=run(['certutil','-L','-d',db,'-n','Cluster Cert','-r'])
assert hashlib.sha256(ca).hexdigest()==ca_hash, 'CA fingerprint mismatch'
assert hashlib.sha256(cert).hexdigest()==cert_hash, 'cluster certificate mismatch'
subject=subprocess.run(['openssl','x509','-inform','DER','-noout','-subject','-nameopt','RFC2253'],input=cert,capture_output=True,timeout=10)
assert subject.returncode==0 and subject.stdout.decode().strip()=='subject=CN='+cluster, 'cluster certificate subject mismatch'
run(['certutil','-V','-d',db,'-n','Cluster Cert','-u','C','-f',password])
keys=run(['certutil','-K','-d',db,'-f',password]).decode()
assert re.search(r'^<\s*\d+>\s+\S+\s+[0-9a-fA-F]+\s+(?:NSS Certificate DB:)?Cluster Cert\s*$', keys, re.M), 'cluster private key absent'
unit=dict(line.split('=',1) for line in run(['systemctl','show','corosync-qdevice.service','-p','LoadState','-p','User','-p','Group','-p','DynamicUser']).decode().splitlines())
assert unit.get('LoadState')=='loaded', 'qdevice unit missing'
assert unit.get('User') in ('','root','0') and unit.get('Group') in ('','root','0') and unit.get('DynamicUser')=='no', 'unexpected effective qdevice service identity'
active=subprocess.run(['systemctl','is-active','--quiet','corosync-qdevice.service'],timeout=10)
assert active.returncode==3, 'qdevice service must be inactive before enrollment'
with socket.create_connection((witness,5403),timeout=5): pass
print(json.dumps({'node':node,'ca_matches':True,'client_certificate_matches':True,'private_key_present':True,'client_certificate_valid':True,'witness_tcp_reachable':True,'quorum':run(['pvecm','status']).decode()}))
'''

POST_PROGRAM = r'''
import json,subprocess
result={}
for key,args in [('quorum',['pvecm','status']),('qdevice',['corosync-qdevice-tool','-s','-v']),('enabled',['systemctl','is-enabled','corosync-qdevice.service']),('active',['systemctl','is-active','corosync-qdevice.service'])]:
    p=subprocess.run(args,capture_output=True,text=True,timeout=15)
    if p.returncode: raise RuntimeError('qdevice verification command failed')
    result[key]=p.stdout
print(json.dumps(result))
'''


def run(args, timeout=40):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'Command failed ({Path(args[0]).name}); inspect the node locally')
    return result.stdout


def validate_inputs(cluster, nodes, witness, ca_hash, cert_hash):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,14}', cluster):
        raise ValueError('Invalid cluster name')
    if len(nodes) != 2 or len(set(nodes)) != 2 or any(not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', n) for n in nodes):
        raise ValueError('Exactly two distinct node names are required')
    address = ipaddress.IPv4Address(witness)
    if str(address) != witness or address not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Witness must be the confirmed NetBird IPv4 address')
    for digest in (ca_hash, cert_hash):
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Expected a lowercase SHA-256 of certificate DER bytes')


def quorum_ready(text, expected_votes):
    values = {k.strip(): v.strip() for k, v in re.findall(r'^([^:\n]+):\s*([^\n]*)$', text, re.M)}
    expected = {'Nodes': '2', 'Expected votes': str(expected_votes), 'Total votes': str(expected_votes), 'Quorum': '2', 'Quorate': 'Yes'}
    if any(values.get(k) != v for k, v in expected.items()):
        return False
    return expected_votes != 3 or 'Qdevice' in values.get('Flags', '').split()


def service_ready(result):
    return (quorum_ready(result.get('quorum', ''), 3)
            and result.get('enabled', '').strip() == 'enabled'
            and result.get('active', '').strip() == 'active'
            and bool(re.search(r'^\s*State:\s+Connected\s*$', result.get('qdevice', ''), re.M))
            and bool(re.search(r'^\s*TLS:\s+Required\s*$', result.get('qdevice', ''), re.M))
            and bool(re.search(r'^\s*TLS active:\s+Yes\s+\(client certificate sent\)\s*$', result.get('qdevice', ''), re.M)))


def remote(ssh, program, args=()):
    command = 'python3 -I -c ' + shlex.quote(program)
    command += ''.join(' ' + shlex.quote(str(a)) for a in args)
    return json.loads(run([*ssh, '--', command], timeout=90))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-host', required=True)
    parser.add_argument('--cluster', required=True)
    parser.add_argument('--nodes', required=True, nargs=2)
    parser.add_argument('--witness-ip', required=True)
    parser.add_argument('--ca-sha256', required=True)
    parser.add_argument('--certificate-sha256', required=True)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    validate_inputs(args.cluster, args.nodes, args.witness_ip, args.ca_sha256, args.certificate_sha256)
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != args.expected_host or args.expected_host not in args.nodes:
        raise ValueError('Run as root on the explicitly named PVE cluster member')
    os.umask(0o077)
    parameters = [args.cluster, ','.join(sorted(args.nodes)), args.witness_ip]
    before = json.loads(run(['perl', '-e', CONFIG_PROGRAM, *parameters, '', '0']))
    proofs = {}
    for node in sorted(args.nodes):
        proof = remote(before['ssh'][node], PROBE_PROGRAM,
                       [node, args.cluster, args.witness_ip, args.ca_sha256, args.certificate_sha256])
        if not quorum_ready(proof['quorum'], 2):
            raise ValueError('Both nodes must have normal two-vote quorum before activation')
        proofs[node] = proof
    receipt = {'cluster': args.cluster, 'nodes': sorted(args.nodes), 'witness_ip': args.witness_ip,
               'configuration_before_sha256': before['sha256'], 'preflight': proofs, 'applied': False}
    if not args.apply:
        print(json.dumps(receipt, indent=2))
        return
    backup = args.backup_dir
    if backup is None or not backup.is_absolute() or backup.is_symlink() or not backup.is_dir():
        raise ValueError('Apply requires an existing absolute backup directory')
    info = backup.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700 or any(backup.iterdir()):
        raise ValueError('Backup directory must be empty, root-owned and mode 0700')
    current = Path('/etc/pve/corosync.conf').read_bytes()
    if hashlib.sha256(current).hexdigest() != before['sha256']:
        raise ValueError('Configuration changed since preflight')
    with (backup / 'corosync-before.conf').open('xb') as stream:
        stream.write(current)
    receipt['stage'] = 'activation_attempted'
    (backup / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    json.loads(run(['perl', '-e', CONFIG_PROGRAM, *parameters, before['sha256'], '1']))
    receipt['applied'] = True
    receipt['stage'] = 'configuration_written'
    (backup / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    # No automatic reset or certificate removal follows a partial failure.
    for node in sorted(args.nodes):
        run([*before['ssh'][node], '--', 'systemctl enable --now corosync-qdevice.service'])
    run(['corosync-cfgtool', '-R'])
    deadline = time.monotonic() + 120
    last = {}
    while time.monotonic() < deadline:
        for node in sorted(args.nodes):
            try:
                last[node] = remote(before['ssh'][node], POST_PROGRAM)
            except (RuntimeError, subprocess.TimeoutExpired):
                last[node] = {'error': 'runtime verification failed'}
        if time.monotonic() <= deadline and all(service_ready(last[n]) for n in args.nodes):
            receipt['verified'] = True
            receipt['stage'] = 'verified'
            receipt['postflight'] = last
            (backup / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps(receipt, indent=2))
            return
        time.sleep(2)
    receipt['verified'] = False
    receipt['stage'] = 'verification_failed'
    receipt['postflight'] = last
    (backup / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    raise RuntimeError('Configuration applied but quorum/TLS verification failed; keep both PVE nodes online and use the recovery runbook')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print('qdevice 활성화 실패: ' + str(error), file=sys.stderr)
        sys.exit(1)
