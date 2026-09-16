#!/usr/bin/env bash
# Run only inside the newly created backup VM; never on its hypervisor.
set -euo pipefail
umask 077
usage() {
  echo 'Usage: install-pbs-guest.sh --expected-host backup-vm [--apply]'
}
fail() { echo "install-pbs-guest: $*" >&2; exit 1; }
expected_host='' apply=0
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 ]] || fail 'root로 실행해야 합니다.'
[[ $expected_host == backup-vm && $(hostname -s) == backup-vm ]] || fail 'backup-vm 전용 VM이 아닙니다.'
[[ $(systemd-detect-virt) == kvm ]] || fail 'KVM guest가 아닙니다.'
# shellcheck disable=SC1091
. /etc/os-release
[[ $ID == debian && $VERSION_ID == 13 ]] || fail 'Debian 13 guest가 필요합니다.'
[[ $(dpkg --print-architecture) == amd64 ]] || fail 'amd64 guest가 필요합니다.'
data_device=/dev/disk/by-id/virtio-PBS_DATA
[[ -b $data_device ]] || fail 'PBS_DATA serial의 신규 가상 디스크가 없습니다.'
[[ $(blockdev --getsize64 "$data_device") == 1099511627776 ]] || fail 'datastore 디스크 크기가 1 TiB가 아닙니다.'
state_dir=/var/lib/pickle-pbs-bootstrap
if [[ -e $state_dir/datastore.uuid ]]; then
  [[ -s $state_dir/datastore.uuid ]] || fail '보관된 datastore UUID가 비어 있습니다.'
  saved_uuid=$(cat "$state_dir/datastore.uuid")
  if ! current_uuid=$(blkid -s UUID -o value "$data_device"); then
    fail '이전 설치의 datastore UUID가 있는데 현재 디스크의 UUID를 확인할 수 없습니다. 포맷하지 않습니다.'
  fi
  [[ $current_uuid == "$saved_uuid" ]] || fail '현재 디스크가 이전 설치의 datastore가 아닙니다. 포맷하지 않습니다.'
fi
if [[ -f $state_dir/complete ]]; then
  mountpoint -q /mnt/datastore/example-prod || fail '완료 표시는 있지만 datastore mount가 없습니다.'
  [[ -s $state_dir/datastore.uuid ]] || fail '보관된 datastore UUID가 없습니다.'
  saved_uuid=$(cat "$state_dir/datastore.uuid")
  [[ $(blkid -s UUID -o value "$data_device") == "$saved_uuid" ]] || fail 'PBS_DATA의 UUID가 보관된 값과 다릅니다.'
  [[ $(findmnt -n -o UUID /mnt/datastore/example-prod) == "$saved_uuid" ]] || fail '실제 mount의 UUID가 다릅니다.'
  runuser -u backup -- test -w /mnt/datastore/example-prod || fail 'backup 계정이 datastore에 쓸 수 없습니다.'
  proxmox-backup-manager datastore list --output-format json | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert len([r for r in rows if r.get("name")=="example-prod" and r.get("path")=="/mnt/datastore/example-prod"])==1, "datastore config does not match"'
  echo 'PBS guest bootstrap은 이미 완료됐습니다.'
  exit 0
fi
if signature=$(blkid -p -s TYPE -o value "$data_device"); then
  :
else
  probe_status=$?
  [[ $probe_status == 2 ]] || fail '디스크 signature를 검사하지 못했습니다.'
  signature=''
fi
if [[ -n $signature && ! -f $state_dir/format-authorized ]]; then
  fail '기존 파일시스템이 있습니다. 새 datastore로 포맷하지 않습니다.'
fi
echo 'PBS_DATA 신규 1 TiB 가상 디스크와 PBS 4.2.5-1을 구성합니다.'
((apply)) || exit 0
install -d -o root -g root -m 700 "$state_dir"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
apt-get update
NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg qemu-guest-agent nftables
curl -fsSLo "$tmp/proxmox.gpg" https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg
printf '%s  %s\n' 136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45 "$tmp/proxmox.gpg" | sha256sum -c -
install -o root -g root -m 644 "$tmp/proxmox.gpg" /usr/share/keyrings/proxmox-archive-keyring.gpg
cat > /etc/apt/sources.list.d/pbs.sources <<'APT'
Types: deb
URIs: http://download.proxmox.com/debian/pbs
Suites: trixie
Components: pbs-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
APT
curl -fsSLo "$tmp/netbird.key" https://pkgs.netbird.io/debian/public.key
fingerprint=$(gpg --batch --with-colons --show-keys "$tmp/netbird.key" | awk -F: '$1=="fpr" {print $10; exit}')
[[ $fingerprint == EFE37DF047DF7CCDF1FC54FA83F79AD029778355 ]] || fail 'NetBird 서명 키 지문이 다릅니다.'
gpg --batch --dearmor --output "$tmp/netbird.gpg" "$tmp/netbird.key"
install -o root -g root -m 644 "$tmp/netbird.gpg" /usr/share/keyrings/netbird-archive-keyring.gpg
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/netbird-archive-keyring.gpg] https://pkgs.netbird.io/debian stable main' > /etc/apt/sources.list.d/netbird.list
chmod 644 /etc/apt/sources.list.d/pbs.sources /etc/apt/sources.list.d/netbird.list
install -d -o root -g root -m 755 /etc/systemd/system/netbird.service.d
printf '[Service]\nEnvironment=NB_DISABLE_SSH_CONFIG=true\n' > /etc/systemd/system/netbird.service.d/10-host-only.conf
chmod 644 /etc/systemd/system/netbird.service.d/10-host-only.conf
systemctl daemon-reload
apt-get update
systemctl mask proxmox-backup.service proxmox-backup-proxy.service
NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  proxmox-backup-server=4.2.5-1 netbird=0.78.2

if [[ -z $signature ]]; then
  : > "$state_dir/format-authorized"
  mkfs.ext4 -m 1 -L pickle-pbs-data "$data_device"
fi
[[ $(blkid -s TYPE -o value "$data_device") == ext4 ]] || fail 'datastore가 ext4가 아닙니다.'
[[ $(blkid -s LABEL -o value "$data_device") == pickle-pbs-data ]] || fail 'datastore label이 다릅니다.'
data_uuid=$(blkid -s UUID -o value "$data_device")
[[ $data_uuid =~ ^[a-fA-F0-9-]+$ ]] || fail 'datastore UUID가 올바르지 않습니다.'
if [[ -e $state_dir/datastore.uuid ]]; then
  [[ $(cat "$state_dir/datastore.uuid") == "$data_uuid" ]] || fail '부분 설치에 보관된 datastore UUID가 다릅니다.'
else
  printf '%s\n' "$data_uuid" > "$state_dir/datastore.uuid"
fi
[[ ! -L /mnt/datastore/example-prod ]] || fail 'datastore mountpoint가 symlink입니다.'
if [[ ! -d /mnt/datastore/example-prod ]]; then
  install -d -o root -g root -m 755 /mnt/datastore/example-prod
fi
fstab_line="UUID=$data_uuid /mnt/datastore/example-prod ext4 defaults 0 2"
grep -Fqx "$fstab_line" /etc/fstab || printf '%s\n' "$fstab_line" >> /etc/fstab
if mountpoint -q /mnt/datastore/example-prod; then
  [[ $(findmnt -n -o UUID /mnt/datastore/example-prod) == "$data_uuid" ]] || fail '다른 datastore가 mount돼 있습니다.'
else
  mount /mnt/datastore/example-prod
fi
for unit in proxmox-backup.service proxmox-backup-proxy.service; do
  install -d -m 755 "/etc/systemd/system/$unit.d"
  printf '[Unit]\nRequiresMountsFor=/mnt/datastore/example-prod\n' > "/etc/systemd/system/$unit.d/datastore.conf"
  chmod 644 "/etc/systemd/system/$unit.d/datastore.conf"
done
# This is a new guest. Keep host rules in a separate table from NetBird.
install -d -m 755 /etc/nftables.d
cat > /etc/nftables.d/pickle-pbs.nft <<'NFT'
table inet pickle_pbs {
  chain input {
    type filter hook input priority filter; policy drop;
    iifname "lo" accept
    ct state established,related accept
    ip saddr 198.19.122.1 tcp dport 22 accept
    iifname "wt0" tcp dport { 22, 8007 } accept
    iifname "wt0" meta l4proto { icmp, ipv6-icmp } accept
    udp dport 51820 accept
    ip protocol icmp icmp type { destination-unreachable, time-exceeded, parameter-problem } accept
  }
}
NFT
chmod 644 /etc/nftables.d/pickle-pbs.nft
cat > /usr/local/sbin/pickle-pbs-firewall <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
/usr/sbin/nft list table inet pickle_pbs >/dev/null 2>&1 || /usr/sbin/nft add table inet pickle_pbs
{
  printf '%s\n' 'flush table inet pickle_pbs'
  cat /etc/nftables.d/pickle-pbs.nft
} | /usr/sbin/nft -f -
SCRIPT
chmod 700 /usr/local/sbin/pickle-pbs-firewall
cat > /etc/systemd/system/pickle-pbs-firewall.service <<'UNIT'
[Unit]
Description=Backup guest management ingress
Before=netbird.service proxmox-backup-proxy.service
After=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash /usr/local/sbin/pickle-pbs-firewall

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 /etc/systemd/system/pickle-pbs-firewall.service
cat > /etc/systemd/system/proxmox-backup-proxy.service.d/firewall.conf <<'UNIT'
[Unit]
Requires=pickle-pbs-firewall.service
After=pickle-pbs-firewall.service
UNIT
chmod 644 /etc/systemd/system/proxmox-backup-proxy.service.d/firewall.conf
systemctl daemon-reload
systemctl enable --now pickle-pbs-firewall.service qemu-guest-agent.service netbird.service
systemctl unmask proxmox-backup.service proxmox-backup-proxy.service
systemctl enable --now proxmox-backup.service proxmox-backup-proxy.service
datastore_state=$(proxmox-backup-manager datastore list --output-format json)
datastore_path=$(python3 -c 'import json,sys; rows=json.load(sys.stdin); found=[r for r in rows if r.get("name")=="example-prod"]; assert len(found)<=1; print(found[0]["path"] if found else "")' <<< "$datastore_state")
if [[ -n $datastore_path ]]; then
  [[ $datastore_path == /mnt/datastore/example-prod ]] || fail '다른 경로의 example-prod datastore가 있습니다.'
elif [[ -d /mnt/datastore/example-prod/.chunks ]]; then
  # A prior create can leave only part of its chunk directory structure behind.
  fail '설정에 없는 .chunks가 있습니다. 부분 생성과 기존 backup을 구분해 복구한 뒤 재개하세요.'
else
  proxmox-backup-manager datastore create example-prod /mnt/datastore/example-prod
fi
proxmox-backup-manager datastore list --output-format json | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert len([r for r in rows if r.get("name")=="example-prod" and r.get("path")=="/mnt/datastore/example-prod"])==1, "datastore config does not match"'
mountpoint -q /mnt/datastore/example-prod
runuser -u backup -- test -w /mnt/datastore/example-prod || fail 'backup 계정이 datastore에 쓸 수 없습니다.'
runuser -u backup -- test -w /mnt/datastore/example-prod/.chunks || fail 'backup 계정이 chunk 디렉터리에 쓸 수 없습니다.'
date -Is > "$state_dir/complete"
echo 'PBS 설치 완료. 별도 NetBird 등록과 최소 권한 backup token, 복구 키 보관을 이어서 확인합니다.'
