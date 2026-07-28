#!/usr/bin/env bash
# Generic cron job wrapper: runs a command, records durable success/failure
# state that health-check.sh surfaces, and appends to a shared log.
#
#   cron-wrap.sh <name> -- <command> [args...]
#
# Why this exists: db-backup.sh (and any cron job) can exit non-zero with the
# output swallowed into a per-job log nobody reads — that is exactly how the
# nightly backup silently broke for ~5 days (cron PATH gap, 2026-07-08~13). This
# wrapper turns a silent failure into a state file the health check alerts on,
# and calls an operator notifier hook when one is configured.
#
# On success: writes <state>/<name>.ok (timestamp) and removes <name>.fail.
# On failure: writes <state>/<name>.fail (timestamp, rc, log tail), KEEPS it
#   until the next success, and invokes the optional notifier hook.
set -euo pipefail

# cron's PATH is minimal and lacks /usr/sbin (where `pct` lives) — the same gap
# that silently broke db-backup. Give wrapped children an explicit PATH.
export PATH="/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

STATE_DIR="${PICKLE_OPS_STATE_DIR:-/var/lib/pickle-ops}"
LOG="${PICKLE_CRON_LOG:-/var/log/pickle-cron.log}"
# Optional operator-provided outbound notifier (mail/webhook). Absent by design
# today: pve1 has no outbound mail path (postfix is loopback-only, no relayhost;
# msmtp not installed — see runbooks/backup-alerting notes). When an executable
# is placed here it is called as: ops-notify.sh <name> <rc> <state-file>.
NOTIFY_HOOK="${PICKLE_OPS_NOTIFY_HOOK:-/etc/pickle/ops-notify.sh}"

usage() { echo "usage: $0 <name> -- <command> [args...]" >&2; exit 2; }

name="${1:-}"; [ -n "$name" ] || usage
shift
[ "${1:-}" = "--" ] || usage
shift
[ "$#" -ge 1 ] || usage

# name is embedded in file paths — restrict to a safe slug.
case "$name" in
  *[!A-Za-z0-9_-]*) echo "cron-wrap: invalid job name '$name'" >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"
now() { date '+%Y-%m-%d %H:%M:%S %z'; }
log() { printf '%s [%s] %s\n' "$(now)" "$name" "$*" >>"$LOG" 2>/dev/null || true; }

out="$(mktemp)"
trap 'rm -f "$out"' EXIT

log "start: $*"
rc=0
"$@" >>"$out" 2>&1 || rc=$?
cat "$out" >>"$LOG" 2>/dev/null || true

if [ "$rc" -eq 0 ]; then
  printf 'ok %s rc=0\n' "$(now)" >"$STATE_DIR/$name.ok"
  rm -f "$STATE_DIR/$name.fail"
  log "success (rc=0)"
  exit 0
fi

{
  printf 'fail %s rc=%s\n' "$(now)" "$rc"
  echo '--- last 20 output lines ---'
  tail -20 "$out"
} >"$STATE_DIR/$name.fail"
log "FAILED (rc=$rc) — state recorded at $STATE_DIR/$name.fail"

if [ -x "$NOTIFY_HOOK" ]; then
  "$NOTIFY_HOOK" "$name" "$rc" "$STATE_DIR/$name.fail" >>"$LOG" 2>&1 \
    || log "notify hook exited non-zero"
else
  log "no outbound notifier ($NOTIFY_HOOK absent); health-check.sh surfaces the failure marker"
fi

exit "$rc"
