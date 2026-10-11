#!/usr/bin/env bash
# Enroll a new management peer without changing DNS, routes or SSH ownership.
set -euo pipefail
usage() { echo 'Usage: enroll-management-peer.sh --expected-host NAME --peer-name NAME --setup-key-file /run/FILE --journal /root/NEW [--apply]'; }
fail() { echo "enroll-management-peer: $*" >&2; exit 1; }
expected_host='' peer_name='' key_file='' journal='' apply=0
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --peer-name) peer_name=${2:?}; shift 2 ;;
    --setup-key-file) key_file=${2:?}; shift 2 ;;
    --journal) journal=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 && -n $expected_host && $(hostname -s) == "$expected_host" ]] || fail '실제 호스트가 일치하지 않습니다.'
[[ $peer_name =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || fail 'peer 이름이 올바르지 않습니다.'
[[ $key_file == /run/* && $(dirname "$key_file") == /run && -f $key_file && ! -L $key_file ]] || fail '/run 바로 아래의 새 key 파일이 필요합니다.'
[[ $(stat -c '%u:%a:%h' "$key_file") == 0:600:1 ]] || fail 'key 파일은 root:0600 single-link여야 합니다.'
[[ $(stat -c %s "$key_file") -gt 0 && $(stat -c %s "$key_file") -lt 1024 ]] || fail 'key 파일 크기가 올바르지 않습니다.'
key_sha=$(sha256sum "$key_file" | awk '{print $1}')
[[ $journal =~ ^/root/[A-Za-z0-9_-]+$ && ! -e $journal && ! -L $journal ]] || fail '새 /root journal 경로가 필요합니다.'
version=$(netbird version)
[[ $version =~ (^|[[:space:]])v?0\.80\.0([[:space:]]|$) ]] || fail '검토한 NetBird 0.80.0이 필요합니다.'
systemctl show netbird.service -p Environment --value | grep -Fq NB_DISABLE_SSH_CONFIG=true || fail 'SSH config 보호가 로드되지 않았습니다.'
before=$(netbird status --json)
python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(1 if d.get("management",{}).get("connected") is True else 0)' <<<"$before" || fail '기존 연결 peer를 재등록하지 않습니다.'
echo '새 management peer를 UDP51821, DNS/route/server-SSH 비활성으로 등록합니다.'
((apply)) || exit 0
mkdir -m 0700 -- "$journal"
umask 077
printf '%s\n' "$before" > "$journal/status-before.json"
if [[ -d /etc/netbird ]]; then cp -a -- /etc/netbird "$journal/netbird-before"; fi
if ! timeout 120 netbird up --setup-key-file "$key_file" --hostname "$peer_name" \
  --wireguard-port 51821 --disable-dns --disable-client-routes --disable-server-routes \
  --allow-server-ssh=false > "$journal/up.stdout" 2> "$journal/up.stderr"; then
  fail '등록 결과가 불명확합니다. 보호 journal과 cloud peer를 조회하고 재실행하지 마세요.'
fi
netbird status --json > "$journal/status-after.json"
python3 - "$journal/status-after.json" <<'PY'
import ipaddress,json,sys
with open(sys.argv[1]) as stream:
    value=json.load(stream)
address=value.get('netbirdIp','').split('/')[0]
if (ipaddress.ip_address(address) not in ipaddress.ip_network('100.64.0.0/10')
        or value.get('management',{}).get('connected') is not True):
    raise SystemExit('Management connection is not confirmed; preserve the attempt')
management=value.get('management',{})
print(json.dumps({**{key:value.get(key) for key in ('netbirdIp','fqdn','daemonVersion')},
                  'management':{key:management.get(key) for key in ('url','connected')}}))
PY
[[ $(stat -c '%u:%a:%h' "$key_file") == 0:600:1 ]] || fail 'key custody가 변경됐습니다.'
[[ $(sha256sum "$key_file" | awk '{print $1}') == "$key_sha" ]] || fail 'key bytes가 변경됐습니다.'
rm -- "$key_file"
echo 'Cloud의 정확한 peer/group을 확인하고 이 일회용 setup key만 revoke하세요.'
