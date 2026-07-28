#!/usr/bin/env bash
# Applies the log-retention policy to pve-node and the pickle containers.
#
#   apply-log-retention.sh [--dry-run]
#
# Two gaps this closes:
#   1. journald ran with the shipped default (10% of the filesystem, capped at
#      4G), i.e. effectively unbounded here — every host now gets an explicit
#      SystemMaxUse drop-in sized to its disk and measured usage.
#   2. Three application logs had no rotation at all: the shared cron log and
#      the (legacy) DB-backup log on pve-node, and the dev mock-mail spool inside
#      the app container. The spool lives under /var/lib, so the stock /var/log
#      rules never covered it and it grew unbounded; it also holds full mail
#      bodies (verification links), so rotated copies keep the 0600 mode.
#
# Rotation itself needs no new schedule: Debian's logrotate.timer already runs
# daily and picks up anything dropped into /etc/logrotate.d.
#
# Idempotent: rewrites the same files and restarts journald only when a drop-in
# actually changed. Re-run after rebuilding a container.
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

APP_CTID="${APP_CTID:-101}"
PROXY_CTID="${PROXY_CTID:-100}"
SSHGW_CTID="${SSHGW_CTID:-102}"

# journald caps, sized ~4-6x the usage measured on 2026-07-26 and well inside
# each root filesystem: pve-node 75M/94G, 100 12M/7.8G, 101 56M/32G, 102 136M/7.8G.
# The gateway container gets far more than that ratio on purpose: its journal is
# the only record of REFUSED ssh attempts with their source IPs (the database
# audit trail only starts once a route is allowed), and the gateway is actively
# probed from the internet. A brute-force flood must not be able to roll the
# window and evict the reconnaissance that preceded it. 1G on a 7.8G rootfs with
# 6G free (measured 2026-07-26).
HOST_JOURNAL_CAP="${HOST_JOURNAL_CAP:-500M}"
PROXY_JOURNAL_CAP="${PROXY_JOURNAL_CAP:-200M}"
APP_JOURNAL_CAP="${APP_JOURNAL_CAP:-500M}"
SSHGW_JOURNAL_CAP="${SSHGW_JOURNAL_CAP:-1G}"

JOURNAL_DROPIN=/etc/systemd/journald.conf.d/pickle.conf

journald_dropin() { # $1 = cap
  cat <<EOF
# pickle log-retention policy (scripts/apply-log-retention.sh).
# Without this the journal is bounded only by the 10%-of-filesystem default.
[Journal]
SystemMaxUse=$1
EOF
}

host_logrotate() {
  cat <<'EOF'
# pickle cron logs (scripts/apply-log-retention.sh). Both are opened per write
# by cron-wrap.sh, so plain rename+create rotation is safe.
/var/log/pickle-cron.log
/var/log/pickle-db-backup.log {
    weekly
    maxsize 20M
    rotate 8
    compress
    missingok
    notifempty
    create 0640 root adm
}
EOF
}

app_logrotate() {
  cat <<'EOF'
# pickle dev mock-mail spool (scripts/apply-log-retention.sh). Outside /var/log,
# so no stock rule covers it. The sender appends with a short-lived handle and
# recreates the file when it is missing, so rename+create rotation loses nothing.
# Bodies include verification links: rotated copies keep the 0600 spool mode.
/var/lib/pickle/mock-mail.log {
    weekly
    maxsize 20M
    rotate 4
    compress
    missingok
    notifempty
    create 0600 pickle pickle
}
EOF
}

# install_file <source> <target> <mode> [ctid] → 0 unchanged, 1 written.
install_file() {
  local src="$1" target="$2" mode="$3" ctid="${4:-}" want have label
  want=$(sha256sum < "$src" | cut -d' ' -f1)
  if [ -n "$ctid" ]; then
    label="[ct $ctid]"
    have=$(pct exec "$ctid" -- sha256sum "$target" 2>/dev/null | cut -d' ' -f1 || true)
  else
    label="[pve-node]"
    have=$(sha256sum "$target" 2>/dev/null | cut -d' ' -f1 || true)
  fi
  if [ "$want" = "$have" ]; then
    echo "  $label $target: unchanged"
    return 0
  fi
  if [ "$DRY_RUN" = 0 ]; then
    if [ -n "$ctid" ]; then
      pct exec "$ctid" -- mkdir -p "$(dirname "$target")"
      pct push "$ctid" "$src" "$target" -u root -g root -p "$mode"
    else
      mkdir -p "$(dirname "$target")"
      install -m "$mode" -o root -g root "$src" "$target"
    fi
    echo "  $label $target: written"
  else
    echo "  $label $target: would be written"
  fi
  return 1
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

apply_journald() { # $1 = cap, $2 = ctid ("" for pve-node)
  local cap="$1" ctid="${2:-}" rc=0
  journald_dropin "$cap" > "$TMP/journald.conf"
  install_file "$TMP/journald.conf" "$JOURNAL_DROPIN" 644 "$ctid" || rc=$?
  if [ "$rc" -eq 1 ] && [ "$DRY_RUN" = 0 ]; then
    if [ -n "$ctid" ]; then
      pct exec "$ctid" -- systemctl restart systemd-journald
    else
      systemctl restart systemd-journald
    fi
  fi
}

echo "journald caps"
apply_journald "$HOST_JOURNAL_CAP"
apply_journald "$PROXY_JOURNAL_CAP" "$PROXY_CTID"
apply_journald "$APP_JOURNAL_CAP" "$APP_CTID"
apply_journald "$SSHGW_JOURNAL_CAP" "$SSHGW_CTID"

echo "logrotate policies"
host_logrotate > "$TMP/pickle.logrotate"
# `|| true` here absorbs install_file's rc 1, which means "written" rather than
# "failed" — a genuine failure inside it aborts the run under set -e, so nothing
# is being hidden. Unlike apply_journald, these two need no restart, so the
# written/unchanged distinction is not read back.
install_file "$TMP/pickle.logrotate" /etc/logrotate.d/pickle 644 || true
app_logrotate > "$TMP/pickle-mock-mail.logrotate"
install_file "$TMP/pickle-mock-mail.logrotate" /etc/logrotate.d/pickle-mock-mail 644 "$APP_CTID" || true

if [ "$DRY_RUN" = 0 ]; then
  echo "validation (logrotate --debug)"
  # A rejected policy used to abort here through set -e with nothing printed —
  # say which file logrotate refused, since that is the whole point of the step.
  if logrotate --debug /etc/logrotate.d/pickle >/dev/null; then
    echo "  [pve-node] pickle: ok"
  else
    echo "  [pve-node] pickle: logrotate --debug rejected /etc/logrotate.d/pickle" >&2
    exit 1
  fi
  if pct exec "$APP_CTID" -- logrotate --debug /etc/logrotate.d/pickle-mock-mail >/dev/null; then
    echo "  [ct $APP_CTID] pickle-mock-mail: ok"
  else
    echo "  [ct $APP_CTID] pickle-mock-mail: logrotate --debug rejected the policy" >&2
    exit 1
  fi
  echo "journal usage after apply"
  printf '  [pve-node] '; journalctl --disk-usage
  for ct in "$PROXY_CTID" "$APP_CTID" "$SSHGW_CTID"; do
    printf '  [ct %s] ' "$ct"; pct exec "$ct" -- journalctl --disk-usage
  done
fi

echo "log retention policy applied"
