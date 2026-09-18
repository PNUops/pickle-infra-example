#!/usr/bin/env bash
# Create only the explicitly named, new PBS disks on an existing libvirt host.
set -euo pipefail
umask 077
usage() {
  cat <<'EOF'
Usage: create-pbs-vm.sh --expected-host NAME --boot-image FILE --sha256 HASH --ssh-public-key FILE --uefi-loader FILE --uefi-vars-template FILE [--apply]
The boot image must be a checksum-verified Debian 13 amd64 generic cloud image.
Creates backup-vm (4 vCPU/8 GiB), a new 64 GiB boot disk and a new 1 TiB data disk.
It refuses an existing domain or target file. No existing disk is reformatted.
EOF
}
fail() { echo "create-pbs-vm: $*" >&2; exit 1; }
expected_host='' boot_image='' expected_hash='' public_key='' uefi_loader='' uefi_vars_template='' apply=0
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --boot-image) boot_image=${2:?}; shift 2 ;;
    --sha256) expected_hash=${2:?}; shift 2 ;;
    --ssh-public-key) public_key=${2:?}; shift 2 ;;
    --uefi-loader) uefi_loader=${2:?}; shift 2 ;;
    --uefi-vars-template) uefi_vars_template=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 ]] || fail 'root로 실행해야 합니다.'
[[ -n $expected_host && $(hostname -s) == "$expected_host" ]] || fail '호스트 이름이 일치하지 않습니다.'
[[ $boot_image == /* && -f $boot_image && ! -L $boot_image ]] || fail 'boot image 절대 경로가 필요합니다.'
[[ $expected_hash =~ ^[[:xdigit:]]{64}$ ]] || fail '신뢰한 checksum 목록의 SHA256이 필요합니다.'
[[ $(sha256sum "$boot_image" | awk '{print $1}') == "$expected_hash" ]] || fail 'boot image SHA256이 다릅니다.'
python3 -I - "$boot_image" <<'PY'
import json, subprocess, sys
info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', sys.argv[1]]))
assert info['format'] == 'qcow2' and not info.get('backing-filename'), 'standalone qcow2 image required'
assert info['virtual-size'] <= 64 * 1024**3, 'boot image exceeds the new disk size'
PY
[[ $public_key == /* && -f $public_key && ! -L $public_key ]] || fail 'SSH 공개키 파일이 필요합니다.'
[[ $(wc -l < "$public_key") -eq 1 ]] || fail 'SSH 공개키는 한 줄이어야 합니다.'
ssh-keygen -l -f "$public_key" >/dev/null || fail 'SSH 공개키를 읽지 못했습니다.'
grep -Eq '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[^ ]+) ' "$public_key" || fail '공개키 형식이 아닙니다.'
[[ $uefi_loader == /* && -f $uefi_loader ]] || fail 'UEFI loader 절대 경로가 필요합니다.'
[[ $uefi_vars_template == /* && -f $uefi_vars_template ]] || fail 'UEFI vars template 절대 경로가 필요합니다.'
for binary in virsh virt-install qemu-img cloud-localds python3 setpriv; do command -v "$binary" >/dev/null || fail "$binary 누락"; done
if virsh -c qemu:///system dominfo backup-vm >/dev/null 2>&1; then fail 'backup-vm domain이 이미 있습니다.'; fi
boot_dir=/var/lib/libvirt/images/backup-vm
data_dir=/home/libvirt/backup-vm
[[ -d $boot_dir && ! -L $boot_dir && -d $data_dir && ! -L $data_dir ]] || fail '전용 디렉터리를 먼저 준비하세요.'
[[ $(findmnt -n -o TARGET -T "$data_dir") == /home ]] || fail 'data 경로가 /home에 없습니다.'
[[ $(stat -c %u "$boot_dir") == 0 && $(stat -c %u "$data_dir") == 0 ]] || fail '디렉터리는 root 소유여야 합니다.'
[[ -z $(find "$boot_dir" "$data_dir" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail '전용 디렉터리가 비어 있지 않습니다.'
(( $(df -B1 --output=avail "$boot_dir" | tail -n 1) > 103079215104 )) || fail 'boot 디스크와 여유 공간이 부족합니다.'
(( $(df -B1 --output=avail "$data_dir" | tail -n 1) > 1206885810176 )) || fail 'data 1 TiB와 여유 공간이 부족합니다.'
script_dir=$(cd "$(dirname "$0")" && pwd)
guest_script=$script_dir/install-pbs-guest.sh
[[ -f $guest_script ]] || fail '같은 scripts 디렉터리에 guest 설치 스크립트가 필요합니다.'
python3 -I - <<'PY'
import subprocess, xml.etree.ElementTree as E
root = E.fromstring(subprocess.check_output(['virsh', '-c', 'qemu:///system', 'net-dumpxml', 'default']))
assert root.find('forward').get('mode') == 'nat', 'default network must use NAT'
assert root.find('ip').get('address') == '198.19.122.1', 'unexpected libvirt gateway'
for host in root.findall('./ip/dhcp/host'):
    assert host.get('ip') != '198.19.122.10' and host.get('mac') != '52:54:00:9e:01:10', 'DHCP reservation already exists'
leases = subprocess.check_output(['virsh', '-c', 'qemu:///system', 'net-dhcp-leases', 'default'], text=True)
assert '198.19.122.10/' not in leases, 'PBS address already has a DHCP lease'
PY
qemu_identity=$(python3 -I - <<'PY'
import pwd, re, subprocess, xml.etree.ElementTree as E
root = E.fromstring(subprocess.check_output(['virsh', '-c', 'qemu:///system', 'capabilities']))
labels = [s.findtext("baselabel[@type='kvm']") for s in root.findall('./host/secmodel') if s.findtext('model') == 'dac']
assert len(labels) == 1 and labels[0], 'libvirt KVM DAC identity is unknown'
match = re.fullmatch(r'\+(\d+):\+(\d+)', labels[0])
assert match and int(match.group(1)) == pwd.getpwnam('libvirt-qemu').pw_uid, 'unexpected QEMU user'
print(match.group(1), match.group(2))
PY
)
read -r qemu_uid qemu_gid <<< "$qemu_identity"
[[ $qemu_uid =~ ^[0-9]+$ && $qemu_gid =~ ^[0-9]+$ ]] || fail 'QEMU DAC identity가 올바르지 않습니다.'
firmware_check=$script_dir/lib/pbs_firmware.py
[[ -f $firmware_check ]] || fail '같은 scripts/lib 디렉터리에 firmware 검사기가 필요합니다.'
firmware_caps=$(mktemp)
firmware_xml=$(mktemp)
firmware_paths=$(mktemp)
cleanup() { rm -f -- "$firmware_caps" "$firmware_xml" "$firmware_paths"; }
trap cleanup EXIT
virsh -c qemu:///system domcapabilities --virttype kvm --arch x86_64 > "$firmware_caps" ||
  fail 'libvirt domain capabilities를 읽지 못했습니다.'
python3 -I "$firmware_check" preflight "$firmware_caps" "$uefi_loader" "$uefi_vars_template" > "$firmware_paths" ||
  fail 'UEFI firmware/vars 지원 검사가 실패했습니다.'
mapfile -t uefi_files < "$firmware_paths"
[[ ${#uefi_files[@]} -eq 2 ]] || fail 'UEFI loader와 vars template을 모두 확인해야 합니다.'
for firmware in "${uefi_files[@]}"; do
  setpriv --reuid "$qemu_uid" --regid "$qemu_gid" --clear-groups -- test -r "$firmware" ||
    fail "QEMU가 UEFI firmware 파일을 읽을 수 없습니다: $firmware"
done
boot_spec="loader=$uefi_loader,loader.readonly=yes,loader.type=pflash,nvram.template=$uefi_vars_template"
virt-install --connect qemu:///system --name backup-vm --memory 8192 --vcpus 4 --cpu host-model \
  --import --os-variant generic --graphics none --noautoconsole --network none \
  --disk "path=$boot_image,format=qcow2,readonly=on" --boot "$boot_spec" --tpm none \
  --dry-run --print-xml > "$firmware_xml" || fail '사용 가능한 UEFI firmware를 선택하지 못했습니다.'
python3 -I "$firmware_check" domain "$firmware_xml" "$uefi_loader" "$uefi_vars_template" ||
  fail 'virt-install UEFI XML 검사가 실패했습니다.'
echo 'backup-vm: 4 vCPU/8 GiB, boot 64 GiB, data 1 TiB, NAT 198.19.122.10을 구성합니다.'
((apply)) || exit 0
boot_disk=$boot_dir/boot.qcow2
data_disk=$data_dir/datastore.raw
chown "root:$qemu_gid" "$boot_dir" "$data_dir"
chmod 750 "$boot_dir" "$data_dir"
qemu-img convert -f qcow2 -O qcow2 "$boot_image" "$boot_disk"
qemu-img resize "$boot_disk" 64G
qemu-img create -f raw -o preallocation=falloc "$data_disk" 1T
python3 -I - "$boot_dir" "$public_key" "$guest_script" <<'PY'
from pathlib import Path
import base64, json, sys, uuid
root = Path(sys.argv[1])
user = {'hostname': 'backup-vm', 'manage_etc_hosts': True, 'disable_root': False, 'ssh_pwauth': False,
        'users': [{'name': 'root', 'lock_passwd': True, 'ssh_authorized_keys': [Path(sys.argv[2]).read_text().strip()]}],
        'write_files': [{'path': '/root/install-pbs-guest.sh', 'owner': 'root:root', 'permissions': '0700',
                         'encoding': 'b64', 'content': base64.b64encode(Path(sys.argv[3]).read_bytes()).decode()}],
        'runcmd': [['/bin/bash', '/root/install-pbs-guest.sh', '--expected-host', 'backup-vm', '--apply']]}
(root / 'user-data').write_text('#cloud-config\n' + json.dumps(user, indent=2) + '\n')
(root / 'meta-data').write_text(json.dumps({'instance-id': 'backup-vm-' + str(uuid.uuid4()), 'local-hostname': 'backup-vm'}))
network = {'version': 2, 'ethernets': {'pbsnet': {'match': {'macaddress': '52:54:00:9e:01:10'},
           'set-name': 'eth0', 'dhcp4': False, 'addresses': ['198.19.122.10/24'],
           'routes': [{'to': '0.0.0.0/0', 'via': '198.19.122.1'}],
           'nameservers': {'addresses': ['198.19.122.1']}}}}
(root / 'network-config').write_text(json.dumps(network, indent=2))
PY
cloud-localds --network-config="$boot_dir/network-config" "$boot_dir/seed.iso" "$boot_dir/user-data" "$boot_dir/meta-data"
chown "$qemu_uid:$qemu_gid" "$boot_disk" "$data_disk" "$boot_dir/seed.iso"
chmod 600 "$boot_disk" "$data_disk" "$boot_dir/seed.iso"
for disk in "$boot_disk" "$data_disk"; do
  setpriv --reuid "$qemu_uid" --regid "$qemu_gid" --clear-groups -- test -r "$disk"
  setpriv --reuid "$qemu_uid" --regid "$qemu_gid" --clear-groups -- test -w "$disk"
done
setpriv --reuid "$qemu_uid" --regid "$qemu_gid" --clear-groups -- test -r "$boot_dir/seed.iso"
virsh -c qemu:///system net-update default add ip-dhcp-host \
  "<host mac='52:54:00:9e:01:10' name='backup-vm' ip='198.19.122.10'/>" --live --config
virt-install --connect qemu:///system --name backup-vm --memory 8192 --vcpus 4 --cpu host-model \
  --import --os-variant generic --graphics none --noautoconsole --boot "$boot_spec" --tpm none \
  --network network=default,model=virtio,mac=52:54:00:9e:01:10 \
  --disk "path=$boot_disk,format=qcow2,bus=virtio,cache=none,serial=PBS_BOOT" \
  --disk "path=$data_disk,format=raw,bus=virtio,cache=none,io=native,serial=PBS_DATA" \
  --disk "path=$boot_dir/seed.iso,format=raw,bus=virtio,readonly=on,serial=PBS_SEED" \
  --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 \
  --console pty,target.type=serial
virsh -c qemu:///system autostart backup-vm
virsh -c qemu:///system dumpxml backup-vm > "$boot_dir/domain.xml"
echo 'backup-vm 생성 완료. guest cloud-init 완료와 datastore mount, SSH, PBS 8007을 확인해야 합니다.'
