#!/usr/bin/env bash
# Guard the candidate-core PBS job before the job and before each CT backup.
set -euo pipefail

fail() { echo "candidate-core backup guard: $*" >&2; exit 1; }

phase=${1:-}
case "$phase" in
  job-init|backup-start) ;;
  job-start|job-end|job-abort|backup-end|backup-abort|log-end|pre-stop|pre-restart|post-restart)
    exit 0 ;;
  *) fail "unexpected hook phase: $phase" ;;
esac

[[ $(hostname -s) == pve-node-2 ]] || fail 'wrong PVE node'
[[ ${STOREID:-} == pbs-example-core-write ]] || fail 'wrong PBS storage'
[[ -z ${DUMPDIR:-} ]] || fail 'file backup target is not allowed'
quorum=$(pvecm status) || fail 'cannot read cluster quorum'
[[ $quorum =~ (^|$'\n')Quorate:[[:space:]]+Yes($|$'\n') ]] || fail 'cluster is not quorate'

if [[ $phase == job-init ]]; then
  [[ $# -eq 1 ]] || fail 'unexpected job-init arguments'
else
  [[ $# -eq 3 && $2 == snapshot && ${VMTYPE:-} == lxc ]] ||
    fail 'unexpected backup identity or mode'
  case "$3" in 1200|1201|1202|1204) ;; *) fail 'unexpected CT ID' ;; esac
fi

for vmid in 1200 1201 1202 1204; do
  case "$vmid" in
    1200) expected=1111111111111111111111111111111111111111111111111111111111111111 ;;
    1201) expected=2222222222222222222222222222222222222222222222222222222222222222 ;;
    1202) expected=3333333333333333333333333333333333333333333333333333333333333333 ;;
    1204) expected=4444444444444444444444444444444444444444444444444444444444444444 ;;
  esac
  config="/etc/pve/lxc/$vmid.conf"
  [[ -f $config ]] || fail "CT $vmid configuration missing"
  if [[ $phase == backup-start && $vmid == "$3" ]]; then
    # PVE has already locked the selected CT before invoking backup-start.
    # Accept that one exact transient line and no other config change.
    actual=$(python3 - "$config" <<'PY'
import hashlib
from pathlib import Path
import sys

lines = Path(sys.argv[1]).read_bytes().splitlines(keepends=True)
lock = b"lock: backup\n"
if lines.count(lock) != 1:
    sys.exit("expected exactly one backup lock")
lines.remove(lock)
print(hashlib.sha256(b"".join(lines)).hexdigest())
PY
    ) || fail "CT $vmid backup lock or configuration changed"
  else
    checksum=$(sha256sum -- "$config") || fail "CT $vmid configuration unreadable"
    actual=${checksum%% *}
  fi
  [[ $actual == "$expected" ]] || fail "CT $vmid configuration changed"
  [[ $(pct status "$vmid") == 'status: running' ]] || fail "CT $vmid is not running locally"
done
