#!/usr/bin/env bash
# Applies the log-retention policy to a platform host and its pickle containers.
#
#   apply-log-retention.sh [--dry-run] [development|production|host]
#
#   development (default, on pve-node): pve-node, LXC 100-103, and the two logrotate
#               policies below.
#   production  (on the production host): that host and the five production
#               containers (proxy, app, sshgw, LLM gateway, database).
#   host        (on any other platform host, e.g. the standby node): the host's
#               own journald cap only.
#
# Production and standby hosts were never covered until 2026-10-24: the script
# named pve-node's containers by number, so every host commissioned later ran on the
# journald default, and the production sshgw lost the deliberate 1G its journal
# is supposed to get (see the cap comment below).
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

# --dry-run is accepted in any position: a preview typed as `production
# --dry-run` must not silently become a real run. Anything beyond one target is
# refused for the same reason.
DRY_RUN=0
TARGET=
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *)
      if [ -n "$TARGET" ]; then
        echo "usage: $0 [--dry-run] [development|production|host]" >&2
        exit 2
      fi
      TARGET="$arg"
      ;;
  esac
done
TARGET="${TARGET:-development}"

# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
case "$TARGET" in
  development)
    APP_CTID="${APP_CTID:-101}"
    PROXY_CTID="${PROXY_CTID:-100}"
    SSHGW_CTID="${SSHGW_CTID:-102}"
    LLMGW_CTID="${LLMGW_CTID:-103}"
    require_ct "$PROXY_CTID" reverse-proxy
    require_ct "$APP_CTID" pickle-app
    require_ct "$SSHGW_CTID" pickle-sshgw
    require_ct "$LLMGW_CTID" pickle-llmgw
    ;;
  production)
    # Different variable names from development on purpose: a value exported
    # for one environment must not redirect the other (lib/ct.sh explains why).
    PROXY_CTID="${PICKLE_PRODUCTION_PROXY_CTID:-1200}"
    APP_CTID="${PICKLE_PRODUCTION_APP_CTID:-1201}"
    SSHGW_CTID="${PICKLE_PRODUCTION_SSHGW_CTID:-1202}"
    LLMGW_CTID="${PICKLE_PRODUCTION_LLMGW_CTID:-1203}"
    DB_CTID="${PICKLE_PRODUCTION_DB_CTID:-1204}"
    require_ct "$PROXY_CTID" example-proxy-prod
    require_ct "$APP_CTID" example-app-prod
    require_ct "$SSHGW_CTID" example-sshgw-prod
    require_ct "$LLMGW_CTID" example-llmgw-prod
    require_ct "$DB_CTID" example-db-prod
    ;;
  host) ;;
  *)
    echo "usage: $0 [--dry-run] [development|production|host]" >&2
    exit 2
    ;;
esac

# journald caps, sized ~4-6x the usage measured on 2026-07-26 and well inside
# each root filesystem: pve-node 75M/94G, 100 12M/7.8G, 101 56M/32G, 102 136M/7.8G.
# The gateway container gets far more than that ratio on purpose: its journal is
# the only record of REFUSED ssh attempts with their source IPs (the database
# audit trail only starts once a route is allowed), and the gateway is actively
# probed from the internet. A brute-force flood must not be able to roll the
# window and evict the reconnaissance that preceded it. 1G on a 7.8G rootfs with
# 6G free (measured 2026-07-26).
PROXY_JOURNAL_CAP="${PROXY_JOURNAL_CAP:-200M}"
APP_JOURNAL_CAP="${APP_JOURNAL_CAP:-500M}"
SSHGW_JOURNAL_CAP="${SSHGW_JOURNAL_CAP:-1G}"
# The LLM gateway journal holds one structured line per request and nothing
# else; 200M is the same ratio the reverse proxy gets, on the same 7.8G root.
LLMGW_JOURNAL_CAP="${LLMGW_JOURNAL_CAP:-200M}"
# The production LLM gateway is sized for what it actually writes, not for the
# development ratio: since 2026-10-20 its journal grows about 16M a day (32M on
# 2026-10-23, 48M a day later), mostly a tmp.mount failure logged on every run
# of a PrivateTmp monitoring unit. 500M keeps about a month at that rate and
# about a year once that noise is gone; the 7.8G root has 6.6G free.
if [ "$TARGET" = production ]; then
  LLMGW_JOURNAL_CAP="${PRODUCTION_LLMGW_JOURNAL_CAP:-500M}"
fi
# Production adds the database container, which development keeps inside the
# app container. Measured 2026-10-24: 70.6M in 35 days on a 63G root, so 500M
# holds about eight months.
DB_JOURNAL_CAP="${DB_JOURNAL_CAP:-500M}"
# The production and standby hosts carry far more than pve-node did when its 500M
# was sized (pve-node-2 476M in 51 days, pve-node-3 746M in 51 days, both on 94G roots,
# measured 2026-10-24). 500M would cut their window to two months or less; 4G
# is the journald default ceiling those roots already had, made explicit, and
# keeps roughly nine months to a year. The default is chosen per target before
# any override is read: setting a 500M default first and then `:-4G` for the
# other targets would never take the second branch.
if [ "$TARGET" = development ]; then
  HOST_JOURNAL_CAP="${HOST_JOURNAL_CAP:-500M}"
else
  HOST_JOURNAL_CAP="${HOST_JOURNAL_CAP:-4G}"
fi

# A cap below the journal's current size deletes the oldest entries the moment
# journald restarts. That is never what a retention policy rollout means, so it
# is refused unless asked for explicitly.
ALLOW_SHRINK="${ALLOW_SHRINK:-0}"
journal_usage_bytes() { # $1 = ctid ("" for the host)
  local line
  if [ -n "${1:-}" ]; then
    line=$(pct exec "$1" -- journalctl --disk-usage)
  else
    line=$(journalctl --disk-usage)
  fi
  line=$(printf '%s\n' "$line" | sed -n 's/.* take up \([0-9.]*[KMGT]\?\)B\? in the file system.*/\1/p')
  [ -n "$line" ] || return 1
  numfmt --from=iec "$line"
}

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

# install_file <source> <target> <mode> [ctid] → 0 unchanged, 1 written, 2 failed.
#
# The failure code is not decoration. Every caller invokes this in a `|| ...`
# list to read the written/unchanged answer, and a function called that way runs
# with `set -e` suspended for its whole body — so a push that fails carries on to
# the next line, prints "written", and returns the same 1 a real write returns.
# The run then reports the policy applied while nothing was installed. Each write
# is therefore checked where it happens, and the result is read back rather than
# assumed.
install_file() {
  local src="$1" target="$2" mode="$3" ctid="${4:-}" want have label
  want=$(sha256sum < "$src" | cut -d' ' -f1)
  if [ -n "$ctid" ]; then
    label="[ct $ctid]"
    have=$(pct exec "$ctid" -- sha256sum "$target" 2>/dev/null | cut -d' ' -f1 || true)
  else
    label="[host]"
    have=$(sha256sum "$target" 2>/dev/null | cut -d' ' -f1 || true)
  fi
  if [ "$want" = "$have" ]; then
    echo "  $label $target: unchanged"
    return 0
  fi
  if [ "$DRY_RUN" != 0 ]; then
    echo "  $label $target: would be written"
    return 1
  fi
  if [ -n "$ctid" ]; then
    pct exec "$ctid" -- mkdir -p "$(dirname "$target")" || return 2
    pct push "$ctid" "$src" "$target" -u root -g root -p "$mode" || return 2
    have=$(pct exec "$ctid" -- sha256sum "$target" 2>/dev/null | cut -d' ' -f1 || true)
  else
    mkdir -p "$(dirname "$target")" || return 2
    install -m "$mode" -o root -g root "$src" "$target" || return 2
    have=$(sha256sum "$target" 2>/dev/null | cut -d' ' -f1 || true)
  fi
  [ "$want" = "$have" ] || {
    echo "  $label $target: written, but the file does not read back as written" >&2
    return 2
  }
  echo "  $label $target: written"
  return 1
}

# install_or_die <same arguments> → 0 unchanged, 1 written; exits on failure.
install_or_die() {
  local rc=0
  install_file "$@" || rc=$?
  [ "$rc" -ne 2 ] || { echo "failed to install $2" >&2; exit 1; }
  return "$rc"
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# check_cap <cap> [ctid] — read-only; exits before anything is written when a
# cap would delete existing entries. Every target is checked before the first
# write, so a refusal never leaves a run half applied.
check_cap() {
  local cap="$1" ctid="${2:-}" used limit label="[host]"
  [ -z "$ctid" ] || label="[ct $ctid]"
  if ! used=$(journal_usage_bytes "$ctid"); then
    echo "  $label cannot read journal usage; refusing to set a cap blind" >&2
    exit 1
  fi
  limit=$(numfmt --from=iec "$cap")
  if [ "$used" -ge "$limit" ] && [ "$ALLOW_SHRINK" != 1 ]; then
    echo "  $label journal uses $used bytes, at or above the cap $cap;" >&2
    echo "  applying it would delete the oldest entries. Raise the cap or set ALLOW_SHRINK=1." >&2
    exit 1
  fi
  echo "  $label cap $cap, journal now $(numfmt --to=iec "$used")"
}

apply_journald() { # $1 = cap, $2 = ctid ("" for the host)
  local cap="$1" ctid="${2:-}" rc=0
  journald_dropin "$cap" > "$TMP/journald.conf"
  install_or_die "$TMP/journald.conf" "$JOURNAL_DROPIN" 644 "$ctid" || rc=$?
  if [ "$rc" -eq 1 ] && [ "$DRY_RUN" = 0 ]; then
    if [ -n "$ctid" ]; then
      pct exec "$ctid" -- systemctl restart systemd-journald
    else
      systemctl restart systemd-journald
    fi
  fi
}

# Targets as "cap|ctid" pairs, the host first with an empty ctid.
CAPS=("$HOST_JOURNAL_CAP|")
CONTAINERS=()
case "$TARGET" in
  development|production)
    CAPS+=("$PROXY_JOURNAL_CAP|$PROXY_CTID" "$APP_JOURNAL_CAP|$APP_CTID"
           "$SSHGW_JOURNAL_CAP|$SSHGW_CTID" "$LLMGW_JOURNAL_CAP|$LLMGW_CTID")
    CONTAINERS=("$PROXY_CTID" "$APP_CTID" "$SSHGW_CTID" "$LLMGW_CTID")
    ;;
esac
if [ "$TARGET" = production ]; then
  CAPS+=("$DB_JOURNAL_CAP|$DB_CTID")
  CONTAINERS+=("$DB_CTID")
fi

echo "journald caps ($TARGET): checking every target first"
for pair in "${CAPS[@]}"; do
  check_cap "${pair%%|*}" "${pair#*|}"
done
echo "journald drop-ins"
for pair in "${CAPS[@]}"; do
  apply_journald "${pair%%|*}" "${pair#*|}"
done

if [ "$TARGET" != development ]; then
  # The two logrotate policies below are for files only pve-node and its app
  # container have (the shared cron log, the dev mock-mail spool).
  if [ "$DRY_RUN" = 0 ]; then
    echo "journal usage after apply"
    printf '  [host] '; journalctl --disk-usage
    for ct in "${CONTAINERS[@]}"; do
      printf '  [ct %s] ' "$ct"; pct exec "$ct" -- journalctl --disk-usage
    done
    echo "log retention policy applied"
  else
    echo "dry run: nothing was written"
  fi
  exit 0
fi

echo "logrotate policies"
host_logrotate > "$TMP/pickle.logrotate"
# `|| true` absorbs the "written" answer, which these two have no use for: unlike
# the journald drop-ins they need no restart. A failure is not absorbed with it —
# install_or_die has already exited by then.
install_or_die "$TMP/pickle.logrotate" /etc/logrotate.d/pickle 644 || true
app_logrotate > "$TMP/pickle-mock-mail.logrotate"
install_or_die "$TMP/pickle-mock-mail.logrotate" /etc/logrotate.d/pickle-mock-mail 644 "$APP_CTID" || true

if [ "$DRY_RUN" = 0 ]; then
  echo "validation (logrotate --debug)"
  # A rejected policy used to abort here through set -e with nothing printed —
  # say which file logrotate refused, since that is the whole point of the step.
  if logrotate --debug /etc/logrotate.d/pickle >/dev/null; then
    echo "  [host] pickle: ok"
  else
    echo "  [host] pickle: logrotate --debug rejected /etc/logrotate.d/pickle" >&2
    exit 1
  fi
  if pct exec "$APP_CTID" -- logrotate --debug /etc/logrotate.d/pickle-mock-mail >/dev/null; then
    echo "  [ct $APP_CTID] pickle-mock-mail: ok"
  else
    echo "  [ct $APP_CTID] pickle-mock-mail: logrotate --debug rejected the policy" >&2
    exit 1
  fi
  echo "journal usage after apply"
  printf '  [host] '; journalctl --disk-usage
  for ct in "${CONTAINERS[@]}"; do
    printf '  [ct %s] ' "$ct"; pct exec "$ct" -- journalctl --disk-usage
  done
fi

echo "log retention policy applied"
