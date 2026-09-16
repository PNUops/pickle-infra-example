#!/usr/bin/env bash
# Prepare an Ubuntu libvirt host without changing its uplink or SSH policy.
set -euo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: bootstrap-backup-host.sh --expected-host NAME --backup-dir PATH [--apply]
       bootstrap-backup-host.sh --expected-host NAME --check
Installs NetBird CLI and a masked qnetd, and prepares PBS disk directories.
Without --apply, only validates prerequisites and prints the intended changes.
Run locally as root on the existing Ubuntu host. No disk is formatted here.
EOF
}
fail() { echo "bootstrap-backup-host: $*" >&2; exit 1; }
expected_host='' backup_dir='' apply=0 check=0
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --backup-dir) backup_dir=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --check) check=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 ]] || fail 'root로 실행해야 합니다.'
[[ -n $expected_host && $(hostname -s) == "$expected_host" ]] || fail '호스트 이름이 일치하지 않습니다.'
if ((check)); then
  ((apply == 0)) || fail '--check와 --apply는 함께 사용할 수 없습니다.'
  script_dir=$(cd "$(dirname "$0")" && pwd)
  exec python3 "$script_dir/check-backup-host.py" --expected-host "$expected_host"
fi
[[ $backup_dir == /* && -d $backup_dir && ! -L $backup_dir ]] || fail '기존 절대 경로 backup 디렉터리가 필요합니다.'
[[ $(stat -c '%u:%a' "$backup_dir") == '0:700' ]] || fail 'backup 디렉터리는 root 소유 0700이어야 합니다.'
# shellcheck disable=SC1091
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 22.04 ]] || fail '이 절차는 Ubuntu 22.04용입니다.'
[[ $(dpkg --print-architecture) == amd64 ]] || fail 'amd64가 필요합니다.'
for binary in virsh qemu-img curl gpg python3 findmnt iptables-save ip6tables-save; do command -v "$binary" >/dev/null || fail "$binary 누락"; done
[[ $(findmnt -n -o TARGET -T /home) == /home ]] || fail '/home이 별도 mount가 아닙니다.'
[[ $(findmnt -n -o FSTYPE -T /home) == ext4 ]] || fail '/home ext4를 확인하지 못했습니다.'
[[ -z $(virsh -c qemu:///system list --all --name) ]] || fail '기존 VM이 있어 별도 검토가 필요합니다.'
getent passwd libvirt-qemu >/dev/null || fail 'libvirt-qemu 계정이 없습니다.'
if [[ -e /etc/corosync/qnetd/nssdb/cert9.db ]]; then
  fail 'qnetd가 이미 초기화돼 있습니다. 기존 NSS를 보존하고 별도로 확인하세요.'
fi
for target in /home/libvirt/backup-vm /var/lib/libvirt/images/backup-vm; do
  [[ ! -e $target ]] || fail "대상 경로가 이미 있습니다: $target"
done
echo '대상: NetBird 0.78.2, corosync-qnetd 3.0.1-1, cloud-image-utils, PBS 전용 디렉터리'
((apply)) || exit 0

[[ ! -e $backup_dir/host-before.txt ]] || fail '이 backup 디렉터리에 이전 실행 기록이 있습니다.'
{
  date -Is
  uname -a
  ip -br address
  ip route
  virsh -c qemu:///system net-list --all
  dpkg-query -W -f='${binary:Package}\t${Version}\n'
} > "$backup_dir/host-before.txt"
cp -a /etc/apt/sources.list /etc/apt/sources.list.d "$backup_dir/"
virsh -c qemu:///system net-dumpxml default > "$backup_dir/libvirt-default-before.xml"
iptables-save > "$backup_dir/iptables-before.txt"
ip6tables-save > "$backup_dir/ip6tables-before.txt"

# Block the package postinst from starting its wildcard listener.
systemctl mask corosync-qnetd.service
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSLo "$tmp/netbird.key" https://pkgs.netbird.io/debian/public.key
fingerprint=$(gpg --batch --with-colons --show-keys "$tmp/netbird.key" | awk -F: '$1=="fpr" {print $10; exit}')
[[ $fingerprint == EFE37DF047DF7CCDF1FC54FA83F79AD029778355 ]] || fail 'NetBird 서명 키 지문이 다릅니다.'
gpg --batch --dearmor --output "$tmp/netbird.gpg" "$tmp/netbird.key"
install -o root -g root -m 644 "$tmp/netbird.gpg" /usr/share/keyrings/netbird-archive-keyring.gpg
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/netbird-archive-keyring.gpg] https://pkgs.netbird.io/debian stable main' > /etc/apt/sources.list.d/netbird.list
chmod 644 /etc/apt/sources.list.d/netbird.list
install -d -o root -g root -m 755 /etc/systemd/system/netbird.service.d
printf '[Service]\nEnvironment=NB_DISABLE_SSH_CONFIG=true\n' > /etc/systemd/system/netbird.service.d/10-host-only.conf
chmod 644 /etc/systemd/system/netbird.service.d/10-host-only.conf
systemctl daemon-reload
apt-get update
NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  netbird=0.78.2 corosync-qnetd=3.0.1-1 cloud-image-utils
[[ $(systemctl is-enabled corosync-qnetd.service) == masked ]] || fail 'qnetd가 mask 상태가 아닙니다.'
if systemctl is-active --quiet corosync-qnetd.service; then fail 'qnetd가 예상과 달리 시작됐습니다.'; fi
if [[ ! -e /home/libvirt ]]; then install -d -o root -g root -m 755 /home/libvirt; fi
[[ -d /home/libvirt && ! -L /home/libvirt && $(stat -c %u /home/libvirt) == 0 ]] || fail '/home/libvirt 소유권을 확인하세요.'
install -d -o root -g libvirt-qemu -m 750 /home/libvirt/backup-vm /var/lib/libvirt/images/backup-vm
systemctl enable --now netbird.service
systemctl show netbird.service -p Environment --value | grep -Fq NB_DISABLE_SSH_CONFIG=true || fail 'NetBird SSH config 보호 설정이 없습니다.'
echo '호스트 준비 완료. NetBird 등록과 qnetd 바인딩을 확인한 뒤 configure-qnetd.sh를 실행합니다.'
