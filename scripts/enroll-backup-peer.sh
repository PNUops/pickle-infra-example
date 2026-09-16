#!/usr/bin/env bash
# Consume a root-only one-off key file without exposing the key in argv.
set -euo pipefail
usage() {
  echo 'Usage: enroll-backup-peer.sh --expected-host NAME --peer-name dept-node|backup-vm --setup-key-file /run/FILE [--apply]'
}
fail() { echo "enroll-backup-peer: $*" >&2; exit 1; }
expected_host='' peer_name='' key_file='' apply=0
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --peer-name) peer_name=${2:?}; shift 2 ;;
    --setup-key-file) key_file=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 ]] || fail 'root로 실행해야 합니다.'
[[ -n $expected_host && $(hostname -s) == "$expected_host" ]] || fail '호스트 이름이 일치하지 않습니다.'
[[ $peer_name == dept-node || $peer_name == backup-vm ]] || fail 'dept-node 또는 backup-vm peer만 등록합니다.'
[[ $key_file == /run/* && -f $key_file && ! -L $key_file ]] || fail '/run의 임시 key 파일이 필요합니다.'
[[ $(dirname "$key_file") == /run ]] || fail 'key 파일은 /run 바로 아래에 둡니다.'
[[ $(stat -c '%u:%a' "$key_file") == 0:600 ]] || fail 'setup key 파일은 root 소유 0600이어야 합니다.'
[[ $(stat -c %s "$key_file") -gt 0 && $(stat -c %s "$key_file") -lt 1024 ]] || fail 'setup key 파일 크기가 올바르지 않습니다.'
systemctl show netbird.service -p Environment --value | grep -Fq NB_DISABLE_SSH_CONFIG=true || fail 'NetBird SSH config 보호 설정이 없습니다.'
echo "$peer_name peer를 DNS와 route 변경 없이 등록합니다."
((apply)) || exit 0
args=(up --setup-key-file "$key_file" --hostname "$peer_name" --disable-dns \
  --disable-client-routes --disable-server-routes --allow-server-ssh=false)
if [[ -e /sys/class/net/idrac ]]; then args+=(--extra-iface-blacklist idrac); fi
netbird "${args[@]}"
rm -f -- "$key_file"
netbird status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in ("netbirdIp","fqdn","daemonVersion","management")},indent=2))'
echo '등록 뒤 관리 API에서 이 일회용 setup key를 revoke하고 peer 정책을 확인합니다.'
