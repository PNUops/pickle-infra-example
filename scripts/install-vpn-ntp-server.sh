#!/usr/bin/env bash
# Extract a reviewed distro package; do not install a competing time daemon.
set -euo pipefail
usage() { echo 'Usage: install-vpn-ntp-server.sh --expected-host NAME --package FILE.deb --package-sha256 SHA --bind ADDRESS --upstream ADDRESS --client ADDRESS [--client ADDRESS ...] [--apply]'; }
fail() { echo "vpn-ntp-server: $*" >&2; exit 1; }
expected_host='' package='' package_sha='' bind_address='' upstream='' apply=0
clients=()
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --package) package=${2:?}; shift 2 ;;
    --package-sha256) package_sha=${2:?}; shift 2 ;;
    --bind) bind_address=${2:?}; shift 2 ;;
    --upstream) upstream=${2:?}; shift 2 ;;
    --client) clients+=("${2:?}"); shift 2 ;;
    --apply) apply=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 && -n $expected_host && $(hostname -s) == "$expected_host" ]] || fail '실제 서버 호스트가 일치하지 않습니다.'
[[ -f $package && ! -L $package && $package_sha =~ ^[0-9a-f]{64}$ ]] || fail '검토한 정규 deb 파일과 SHA가 필요합니다.'
[[ $(sha256sum "$package" | awk '{print $1}') == "$package_sha" ]] || fail 'deb SHA가 다릅니다.'
[[ $(dpkg-deb -f "$package" Package) == chrony ]] || fail 'chrony 패키지가 아닙니다.'
[[ $(dpkg-deb -f "$package" Architecture) == "$(dpkg --print-architecture)" ]] || fail 'distro architecture가 다릅니다.'
version=$(dpkg-deb -f "$package" Version)
[[ $version =~ ^[A-Za-z0-9.+:~_-]+$ ]] || fail '패키지 version이 올바르지 않습니다.'
python3 - "$bind_address" "$upstream" "${clients[@]}" <<'PY'
import ipaddress,sys
overlay=ipaddress.ip_network('100.64.0.0/10')
addresses=[ipaddress.ip_address(value) for value in sys.argv[1:]]
if len(addresses)<3 or any(value.version!=4 for value in addresses):
    raise SystemExit('An IPv4 server, upstream and explicit clients are required')
bind,upstream,*clients=addresses
if bind not in overlay or any(value not in overlay for value in clients):
    raise SystemExit('Bind and clients must be exact overlay addresses')
if bind in clients or len(clients)!=len(set(clients)) or upstream.is_unspecified or upstream.is_multicast:
    raise SystemExit('Duplicate, server-as-client or invalid upstream address')
PY
release="/opt/pickle-chrony-server/$version"
config=/etc/pickle-vpn-ntp-server.conf
service=/etc/systemd/system/pickle-vpn-ntp-server.service
for path in "$release" "$config" "$service"; do
  [[ ! -e $path && ! -L $path ]] || fail "기존 경로를 덮어쓰지 않습니다: $path"
done
systemctl is-active --quiet systemd-timesyncd.service || fail '기존 time client가 active여야 합니다.'
[[ $(timedatectl show -p NTPSynchronized --value) == yes ]] || fail '기존 시스템 시각 동기화를 먼저 확인해야 합니다.'
ip -j address show dev wt0 | python3 -c 'import json,sys; d=json.load(sys.stdin); wanted=sys.argv[1]; sys.exit(0 if any(a.get("local")==wanted for i in d for a in i.get("addr_info",[])) else 1)' "$bind_address" || fail '실제 wt0 주소가 다릅니다.'
echo "chrony $version deb를 추출하고 IPv4 VPN-only server를 -x로 실행합니다."
((apply)) || exit 0
umask 077
if [[ -e /opt/pickle-chrony-server || -L /opt/pickle-chrony-server ]]; then
  [[ -d /opt/pickle-chrony-server && ! -L /opt/pickle-chrony-server \
    && $(stat -c '%u:%a' /opt/pickle-chrony-server) == 0:755 ]] || fail '기존 release parent custody가 다릅니다.'
else
  mkdir -m 0755 /opt/pickle-chrony-server
fi
mkdir -m 0755 -- "$release"
dpkg-deb -x "$package" "$release"
binary="$release/usr/sbin/chronyd"
[[ -x $binary ]] || fail '패키지에 실행 가능한 chronyd가 없습니다. Partial release를 보존하세요.'
dependencies=$(ldd "$binary") || fail 'ABI loader 검사가 실패했습니다. Partial release를 보존하세요.'
if grep -Fq 'not found' <<<"$dependencies"; then fail 'ABI dependency가 없습니다. Partial release를 보존하고 별도 검토하세요.'; fi
(
  set -C
  {
    printf 'server %s iburst\nbindaddress %s\nport 123\ncmdport 0\n' "$upstream" "$bind_address"
    printf 'bindcmdaddress /run/pickle-vpn-ntp-server/chronyd.sock\npidfile /run/pickle-vpn-ntp-server/chronyd.pid\n'
    for address in "${clients[@]}"; do printf 'allow %s/32\n' "$address"; done
  } > "$config"
  cat > "$service" <<EOF
[Unit]
Description=VPN-only NTP server without system clock control
Requires=netbird.service
After=network-online.target netbird.service
[Service]
Type=simple
User=root
ExecStart=$binary -4 -n -x -u root -f $config
Restart=on-failure
RestartSec=5
RuntimeDirectory=pickle-vpn-ntp-server
RuntimeDirectoryMode=0750
NoNewPrivileges=true
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
ProtectClock=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
)
chmod 0600 "$config"
chmod 0644 "$service"
"$binary" -p -f "$config" > "$release/config-parse.txt"
systemd-analyze verify "$service"
systemctl daemon-reload
systemctl enable --now pickle-vpn-ntp-server.service
systemctl is-active --quiet systemd-timesyncd.service || fail '기존 time client를 보존하지 못했습니다.'
echo '서버 설치를 완료했습니다. NTP reference/allow/socket과 client offset을 별도로 확인하세요.'
