#!/usr/bin/env bash
# Compatibility pathname for explicit environment and prebuilt artifact deployment.
set -euo pipefail

if [ "$#" -lt 5 ]; then
  echo 'usage: deploy-console.sh <check|apply> <dev|prod> <commit> <artifact> <sha256> [options]' >&2
  exit 2
fi
case "$1:$2" in
  check:dev|check:prod|apply:dev|apply:prod) ;;
  *) echo 'an explicit action and environment are required' >&2; exit 2 ;;
esac
for name in CTID EXPECTED_CT_HOSTNAME API_DIR CONSOLE_DIR WEB_ROOT RELEASES_DIR HEALTH_TICKS LLMGW_DIR RP GO REMOTE_BIN REMOTE_KEYGEN REMOTE_UNIT KEEP \
            VITE_VM_NETWORK_POLICY_ENABLED VITE_PUBLIC_SOURCE_POLICY_ENABLED; do
  if [ "${!name+x}" = x ]; then
    echo 'legacy target and checkout overrides are not accepted; select an environment and artifact' >&2
    exit 2
  fi
done
script_dir="$(cd "$(dirname "$0")" && pwd)"
action="$1"; environment="$2"
shift 2
exec python3 "$script_dir/deploy-environment.py" "$action" "$environment" console "$@"
