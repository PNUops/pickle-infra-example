#!/usr/bin/env bash
# Records that a scheduled operations unit failed. Invoked by systemd through
# OnFailure=pickle-ops-failure@<unit>.service.
#
# Why this exists separately from cron-wrap.sh: the wrapper can only record a
# failure it is running inside of. When the unit fails before the wrapper gets
# control — missing interpreter, deleted script, a permission problem — there is
# nobody left to write a marker, and that is precisely how ten days of backups
# and health snapshots vanished without a trace. systemd notices that class of
# failure, so let systemd be the one to record it.
#
# The marker is per unit, and the unit clears its own on the next successful run
# (ExecStartPost). A marker that nothing retracts would turn one transient
# failure into a permanent notice, which is how a warning surface stops being
# read at all.
set -euo pipefail

STATE_DIR="${PICKLE_OPS_STATE_DIR:-/var/lib/pickle-ops}"
LOG="${PICKLE_CRON_LOG:-/var/log/pickle-cron.log}"

unit="${1:-unknown}"
# The unit name is embedded in a file path.
case "$unit" in
  *[!A-Za-z0-9_.@-]*) unit="invalid" ;;
esac

mkdir -p "$STATE_DIR"
stamp=$(date '+%Y-%m-%d %H:%M:%S %z')

{
  printf 'fail %s unit=%s\n' "$stamp" "$unit"
  echo '--- last 20 journal lines for the unit ---'
  journalctl -u "$unit" -n 20 --no-pager 2>/dev/null || echo '(journal unavailable)'
} >"$STATE_DIR/unit-failure-$unit.fail"

printf '%s [unit-failure] %s failed at the unit level\n' "$stamp" "$unit" >>"$LOG" 2>/dev/null || true
