#!/usr/bin/env bash
# Bind the witness to its mesh address; transfer only public certificates.
set -euo pipefail
umask 077
usage() {
  cat <<'EOF'
Usage: configure-qnetd.sh --expected-host NAME --mesh-ip IPv4 [--apply]
       configure-qnetd.sh --expected-host NAME --sign-request FILE --sha256 HASH [--apply]
The signing mode signs only a CSR for example-prod. It never enables root SSH.
Public CA/certificate outputs are under /var/lib/pickle/qnetd-public/.
EOF
}
fail() { echo "configure-qnetd: $*" >&2; exit 1; }
expected_host='' mesh_ip='' csr='' expected_hash='' apply=0
while (($#)); do
  case "$1" in
    --expected-host) expected_host=${2:?}; shift 2 ;;
    --mesh-ip) mesh_ip=${2:?}; shift 2 ;;
    --sign-request) csr=${2:?}; shift 2 ;;
    --sha256) expected_hash=${2:?}; shift 2 ;;
    --apply) apply=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ $EUID == 0 ]] || fail 'root로 실행해야 합니다.'
[[ -n $expected_host && $(hostname -s) == "$expected_host" ]] || fail '호스트 이름이 일치하지 않습니다.'
[[ -f /etc/corosync/qnetd/nssdb/qnetd-cacert.crt ]] || fail 'qnetd CA가 없습니다. 기존 NSS를 초기화하지 마세요.'
public_dir=/var/lib/pickle/qnetd-public
if [[ -n $csr ]]; then
  [[ -z $mesh_ip && $csr == /* && -f $csr && ! -L $csr ]] || fail 'CSR 경로를 확인하세요.'
  [[ $expected_hash =~ ^[[:xdigit:]]{64}$ ]] || fail '검토한 CSR SHA256이 필요합니다.'
  # On apply, verify the same protected bytes that the signer will consume.
  checked_csr=$csr
  if ((apply)); then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    install -o root -g root -m 600 "$csr" "$tmp/request.der"
    checked_csr=$tmp/request.der
  fi
  [[ $(sha256sum "$checked_csr" | awk '{print $1}') == "$expected_hash" ]] || fail 'CSR SHA256이 다릅니다.'
  subject=$(openssl req -inform DER -in "$checked_csr" -noout -subject -verify -nameopt RFC2253)
  [[ $subject == 'subject=CN=example-prod' ]] || fail 'CSR subject가 example-prod가 아닙니다.'
  [[ ! -e /etc/corosync/qnetd/nssdb/cluster-example-prod.crt ]] || fail '서명된 cluster 인증서가 이미 있습니다.'
  echo '검증된 example-prod CSR을 현재 qnetd CA로 서명합니다.'
  ((apply)) || exit 0
  corosync-qnetd-certutil -s -c "$checked_csr" -n example-prod
  install -d -o root -g root -m 755 "$public_dir"
  install -o root -g root -m 644 /etc/corosync/qnetd/nssdb/cluster-example-prod.crt "$public_dir/"
else
  [[ -n $mesh_ip ]] || fail 'mesh IPv4가 필요합니다.'
  if systemctl is-active --quiet corosync-qnetd.service; then
    fail '실행 중인 witness는 재설정하지 않습니다. quorum을 확인하는 별도 변경 절차가 필요합니다.'
  fi
  python3 -I - "$mesh_ip" <<'PY'
import ipaddress, json, subprocess, sys
address = ipaddress.IPv4Address(sys.argv[1])
assert address in ipaddress.IPv4Network('100.64.0.0/10'), 'mesh address must be in shared space'
links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'address', 'show', 'dev', 'wt0']))
assert any(a['local'] == str(address) for link in links for a in link.get('addr_info', [])), 'address is not assigned to wt0'
PY
  # Ubuntu 3.0.1 accepts 'req' here even though its manual spells out 'required'.
  python3 -I - <<'PY'
import subprocess
probe = subprocess.run(['corosync-qnetd', '-s', 'req', '-c', 'on', '-h'],
                       capture_output=True, text=True, timeout=10)
if probe.returncode not in (0, 1) or probe.stderr.strip() or not probe.stdout.startswith('usage: corosync-qnetd '):
    raise SystemExit('Installed qnetd rejected mandatory TLS/client-certificate arguments')
PY
  systemctl is-active --quiet netbird || fail 'NetBird가 실행 중이 아닙니다.'
  echo "qnetd를 $mesh_ip:5403/TCP에 TLS 필수로 바인딩합니다."
  ((apply)) || exit 0
  install -d -o root -g root -m 755 /etc/systemd/system/corosync-qnetd.service.d "$public_dir"
  printf 'COROSYNC_QNETD_OPTIONS="-4 -l %s -p 5403 -s req -c on"\n' "$mesh_ip" > /etc/default/corosync-qnetd
  chmod 644 /etc/default/corosync-qnetd
  cat > /etc/systemd/system/corosync-qnetd.service.d/netbird.conf <<'UNIT'
[Unit]
Wants=netbird.service
After=netbird.service
StartLimitIntervalSec=0

[Service]
Restart=on-failure
RestartSec=5s
StandardError=journal
UNIT
  chmod 644 /etc/systemd/system/corosync-qnetd.service.d/netbird.conf
  install -o root -g root -m 644 /etc/corosync/qnetd/nssdb/qnetd-cacert.crt "$public_dir/"
  systemctl daemon-reload
  systemctl unmask corosync-qnetd.service
  if ! systemctl enable --now corosync-qnetd.service; then
    systemctl stop corosync-qnetd.service
    fail 'qnetd 시작에 실패했습니다. journalctl -u corosync-qnetd로 원인을 확인하세요.'
  fi
  systemctl is-active --quiet corosync-qnetd.service || fail 'qnetd 기동을 확인하지 못했습니다.'
  python3 -I - "$mesh_ip" <<'PY'
import subprocess, sys
rows = subprocess.check_output(['ss', '-H', '-ltn', 'sport', '=', ':5403'], text=True).splitlines()
assert {row.split()[3] for row in rows} == {sys.argv[1] + ':5403'}, 'qnetd listener does not match the mesh address'
PY
fi
sha256sum "$public_dir/"*.crt
