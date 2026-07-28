#!/usr/bin/env bash
# Moves the scheduled operations jobs on pve1 from cron.d to systemd timers and
# installs the login notice that surfaces their state.
#
# Why: the cron entries invoked their scripts directly, so they depended on the
# files' execute bit. Those two files are committed non-executable, which meant
# cron fired on schedule for ten days while every run died at exec with the
# error going nowhere — no marker, no log line, and the health snapshot that was
# supposed to notice was the second casualty. The units here launch both the
# wrapper and its child through the interpreter, so the execute bit stops
# mattering, and systemd records unit-level failures that cron discarded.
#
# Idempotent: re-running syncs the unit files, re-enables the timers and
# reinstalls the notice. Leaves the cron.d files in a backup and removes them
# so the two schedules cannot both run.
set -euo pipefail

SRC_UNITS=/root/pickle/infra/hosts/pve1/systemd
SRC_MOTD=/root/pickle/infra/hosts/pve1/motd/50-pickle-ops
UNIT_DIR=/etc/systemd/system
MOTD_DIR=/etc/update-motd.d
STATE_DIR="${PICKLE_OPS_STATE_DIR:-/var/lib/pickle-ops}"

ts=$(date +%Y%m%d-%H%M%S)
BK="/root/pickle/backup/ops-timers-$ts"
mkdir -p "$BK"

echo "== backup the cron entries being replaced -> $BK"
for f in /etc/cron.d/pickle-db-backup /etc/cron.d/pickle-health-check; do
  if [ -f "$f" ]; then cp -a "$f" "$BK/"; fi
done
ls -1 "$BK" 2>/dev/null || echo "  (no cron.d files present — already migrated)"

echo "== install unit files"
for u in "$SRC_UNITS"/*; do
  install -m 0644 "$u" "$UNIT_DIR/$(basename "$u")"
  echo "  $(basename "$u")"
done
systemctl daemon-reload

echo "== install the login notice"
install -d -m 0755 "$MOTD_DIR"
install -m 0755 "$SRC_MOTD" "$MOTD_DIR/50-pickle-ops"
install -d -m 0755 "$STATE_DIR"

echo "== enable the timers"
systemctl enable --now pickle-db-backup.timer pickle-health.timer

echo "== retire the cron entries (both schedules must not run)"
rm -f /etc/cron.d/pickle-db-backup /etc/cron.d/pickle-health-check

echo "== verification"
vfail=0
for t in pickle-db-backup.timer pickle-health.timer; do
  if systemctl is-enabled "$t" >/dev/null 2>&1 && systemctl is-active "$t" >/dev/null 2>&1; then
    echo "  OK   $t enabled and active"
  else
    echo "  FAIL $t not enabled/active"; vfail=$((vfail + 1))
  fi
done
if [ -x "$MOTD_DIR/50-pickle-ops" ]; then echo "  OK   login notice installed"
else echo "  FAIL login notice missing or not executable"; vfail=$((vfail + 1)); fi
if [ -e /etc/cron.d/pickle-db-backup ] || [ -e /etc/cron.d/pickle-health-check ]; then
  echo "  FAIL a replaced cron entry is still present"; vfail=$((vfail + 1))
else
  echo "  OK   cron entries retired"
fi
# The point of the change: the jobs must run even with the execute bit cleared.
# Prove it here rather than assuming it.
if head -1 "$UNIT_DIR/pickle-health.service" >/dev/null 2>&1 \
   && grep -q 'ExecStart=/bin/bash .*cron-wrap.sh health -- /bin/bash ' "$UNIT_DIR/pickle-health.service"; then
  echo "  OK   health unit invokes wrapper and child through the interpreter"
else
  echo "  FAIL health unit does not use interpreter invocation"; vfail=$((vfail + 1))
fi

if [ "$vfail" -ne 0 ]; then
  echo "FAILED — $vfail check(s). Restore with: cp -a $BK/pickle-* /etc/cron.d/ && systemctl disable --now pickle-db-backup.timer pickle-health.timer"
  exit 1
fi
echo "OK — rollback if ever needed: cp -a $BK/pickle-* /etc/cron.d/ && systemctl disable --now pickle-db-backup.timer pickle-health.timer"
