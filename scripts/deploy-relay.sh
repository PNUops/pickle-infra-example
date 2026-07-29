#!/usr/bin/env bash
# Builds the pickle relay-agent on pve1 and deploys it to the off-campus
# Lightsail relay together with the relay's boot-time network config (nftables,
# sysctl, the conntrack module load and its options, the nftables drop-in and
# the agent unit). Mirrors deploy-proxy-agent.sh's structure and failure model
# (verify gate, timestamped release, atomic swap, health gate, automatic
# rollback, prune) but the transport is ssh/sudo, because the relay is a rented
# instance and not an LXC on this host.
#
# What a failed deploy does NOT break: the agent owns its own nft table and
# neither systemd nor this script tears it down, so live port-forwarding rules
# survive the agent being stopped. One caveat on the restart path — the agent
# re-applies its persisted snapshot at start and converges to the EMPTY set if
# that snapshot is older than its max age or fails re-validation, which is
# fail-closed by design but does mean a restart can legitimately clear mappings.
# What a failed deploy mainly breaks is convergence: new/edited/suspended
# mappings stop being applied, so a bad binary is rolled back automatically.
#
# NOT managed here: /etc/pickle/relay-agent.env (operator config, root 640,
# carries the per-relay sync token) and the HAProxy/WireGuard templates under
# lightsail/, which hold per-instance keys and are applied by hand at bring-up.
# This script only asserts that the env file exists.
#
# Health-gate limit, stated so nobody over-trusts it: the agent logs its boot
# convergence BEFORE the first sync poll, so a deploy that breaks only the sync
# transport (wrong URL, rejected token, a response the agent refuses) can still
# produce a converged line. The gate therefore watches the whole window instead
# of exiting at the first good line, and counts repeated `sync failed` lines as
# a negative signal — but the authoritative check remains the relay's
# last-contact in the admin relay list.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # resolved before any cd
REPO="$(cd "$HERE/.." && pwd)"

RELAY_HOST="${RELAY_HOST:-198.51.100.10}"
RELAY_SSH_PORT="${RELAY_SSH_PORT:-22}"
VAULT="${VAULT:-/path/to/secrets-vault}"
RELAY_SSH_KEY="${RELAY_SSH_KEY:-$VAULT/lightsail-ssh.pem}"
RELAY_USER="${RELAY_USER:-admin}"
RELAY_AGENT_DIR="${RELAY_AGENT_DIR:-/root/pickle/relay-agent}"
GO="${GO:-/usr/local/go/bin/go}"
KEEP="${KEEP:-5}"
# x2s per tick, and the gate consumes the WHOLE window rather than stopping at
# the first converged line (see the header): 20 ticks = 40s, which is two to
# three poll cycles at the agent's 15s default, enough for a broken sync
# transport to show up as repeated failures.
HEALTH_TICKS="${HEALTH_TICKS:-20}"

# The unit runs as User=/Group=pickle-relay. Nothing else on the box creates
# that account, so the preflight below does.
SVC_USER=pickle-relay

REMOTE_ROOT=/opt/pickle/relay-agent
RELEASES_DIR="$REMOTE_ROOT/releases"
REMOTE_BIN="$REMOTE_ROOT/bin/relay-agent"
REMOTE_UNIT=/etc/systemd/system/relay-agent.service
REMOTE_ENV=/etc/pickle/relay-agent.env

ts=$(date +%Y%m%d-%H%M%S)

# The relay host key must already be in this host's known_hosts
# (StrictHostKeyChecking stays at its default): a deploy is not the place to
# accept a new key for a box that terminates every user's SSH.
ssh_opts=(-i "$RELAY_SSH_KEY" -p "$RELAY_SSH_PORT" -o BatchMode=yes
          -o ConnectTimeout=10 -o ServerAliveInterval=15)
# Client-side expansion of the remote command is intended throughout: every
# value interpolated into it is a path constant from this file, and the remote
# side must see the resolved path.
# shellcheck disable=SC2029
rsh() { ssh "${ssh_opts[@]}" "$RELAY_USER@$RELAY_HOST" "$@"; }

[ -f "$RELAY_SSH_KEY" ] || {
  echo "deploy FAILED: relay ssh key not found at $RELAY_SSH_KEY (unlock the vault first)" >&2
  exit 1; }

echo "==> preflight on $RELAY_HOST"
# `sudo -n`: fail immediately on a password prompt instead of hanging on a
# stalled tty half way through the config sync.
rsh 'sudo -n true' </dev/null >/dev/null 2>&1 || {
  echo "deploy FAILED: relay unreachable, or no passwordless sudo for $RELAY_USER" >&2
  exit 1; }
# The env file carries the firewall-shaping variables and the sync token; the
# agent fails closed without it, and this script must never create, read or
# echo it.
rsh "sudo -n test -f $REMOTE_ENV" </dev/null || {
  echo "deploy FAILED: $REMOTE_ENV is missing on the relay." >&2
  echo "               It is operator config (root 640) and is deliberately not managed" >&2
  echo "               by this script; create it per the relay runbook, then rerun." >&2
  exit 1; }
# Service account. Without it the unit fails at start on a rebuilt box, and on a
# first deploy there is no previous release to roll back to either, so the box
# is left crash-looping. Idempotent, and the group is created explicitly rather
# than relying on useradd's USERGROUPS_ENAB default, because the unit names
# Group= as well as User=.
rsh "sudo -n bash -s" <<PREFLIGHT || { echo "deploy FAILED: could not ensure the $SVC_USER service account" >&2; exit 1; }
set -euo pipefail
ensure_account() {
  getent group $SVC_USER >/dev/null || {
    groupadd --system $SVC_USER
    echo "created system group $SVC_USER"
  }
  id -u $SVC_USER >/dev/null 2>&1 || {
    useradd --system --gid $SVC_USER --no-create-home --shell /usr/sbin/nologin $SVC_USER
    echo "created system user $SVC_USER"
  }
}
ensure_account
PREFLIGHT

cd "$RELAY_AGENT_DIR"
bash scripts/verify.sh            # lint + go fmt/vet/build/test gate

echo "==> building static linux/amd64 binary"
# Both agent scripts are invoked through the interpreter, never as bare paths:
# a missing execute bit then fails the deploy at exec instead of silently
# skipping, which is how a set of cron jobs once died unnoticed for ten days.
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 PATH="$(dirname "$GO"):$PATH" bash scripts/build.sh
[ -f dist/relay-agent ] || { echo "deploy FAILED: dist/relay-agent was not produced" >&2; exit 1; }

# --- stage the artifacts ----------------------------------------------------
# Staged locally, shipped in one stream, then installed on the box only where
# the content actually differs. Left column is the source (the agent unit comes
# from the agent repo, so a unit edit ships with the binary it belongs to),
# right column the destination on the relay.
stage_local=$(mktemp -d)
trap 'rm -rf "$stage_local"' EXIT
manifest="$stage_local/MANIFEST"
: > "$manifest"

stage_file() {   # stage_file <local-path> <remote-path>
  local src="$1" dest="$2" flat
  [ -f "$src" ] || { echo "deploy FAILED: missing config artifact $src" >&2; exit 1; }
  flat=$(printf '%s' "${dest#/}" | tr '/' '_')
  cp "$src" "$stage_local/$flat"
  printf '%s\t%s\n' "$flat" "$dest" >> "$manifest"
}

stage_file "$REPO/lightsail/nftables/nftables.conf"                  /etc/nftables.conf
stage_file "$REPO/lightsail/sysctl.d/99-pickle-forward.conf"         /etc/sysctl.d/99-pickle-forward.conf
stage_file "$REPO/lightsail/modules-load.d/pickle-conntrack.conf"    /etc/modules-load.d/pickle-conntrack.conf
stage_file "$REPO/lightsail/modprobe.d/pickle-conntrack.conf"        /etc/modprobe.d/pickle-conntrack.conf
stage_file "$REPO/lightsail/systemd/nftables.service.d/pickle.conf"  /etc/systemd/system/nftables.service.d/pickle.conf
stage_file "$RELAY_AGENT_DIR/scripts/systemd/relay-agent.service"    "$REMOTE_UNIT"

cp dist/relay-agent "$stage_local/relay-agent.bin"

# Remote staging dir from `mktemp -d`, not a predictable /tmp path: root
# installs an executable out of it, and a guessable name in a world-writable
# directory is a name an attacker can own first.
stage_remote=$(rsh 'mktemp -d /tmp/pickle-relay-deploy.XXXXXX' </dev/null)
[ -n "$stage_remote" ] || { echo "deploy FAILED: could not create a remote staging dir" >&2; exit 1; }

# tar over ssh rather than `scp -r`: one stream, and no dependence on how the
# local scp resolves a directory argument (the sftp-backed scp changed that).
echo "==> shipping to $stage_remote"
tar -C "$stage_local" -cf - . | rsh "tar -C $stage_remote -xf -"

# --- resolve the rollback target BEFORE anything is swapped -----------------
# The binary that is live right now is the only known-good one. Deriving it
# later from the releases listing gets it wrong after a failed deploy: the
# newest-but-one release is then the binary that already failed its own health
# gate. A hand-placed regular file (first bring-up) is adopted into the releases
# dir under its own mtime so it becomes an ordinary rollback target.
prev_release=$(rsh "sudo -n bash -s" <<PRE
set -euo pipefail
install -d -m 755 "$RELEASES_DIR" "$REMOTE_ROOT/bin"
if [ -L "$REMOTE_BIN" ]; then
  readlink -f "$REMOTE_BIN"
elif [ -e "$REMOTE_BIN" ]; then
  adopted="$RELEASES_DIR/relay-agent-\$(date -r "$REMOTE_BIN" +%Y%m%d-%H%M%S)"
  [ -e "\$adopted" ] || cp -a "$REMOTE_BIN" "\$adopted"
  printf '%s\n' "\$adopted"
fi
PRE
)
if [ -n "$prev_release" ]; then
  echo "==> rollback target: $prev_release"
else
  echo "==> no live binary found; this is a first deploy (no rollback target)"
fi

# --- install config, swap the binary, apply, restart ------------------------
# The remote body is a function invoked on the last line. `bash -s` executes
# stdin as it arrives, so a session dropped mid-transfer would otherwise run a
# PREFIX of the script; with the function form an incomplete stream simply
# never calls it.
apply_rc=0
rsh "sudo -n bash -s" <<REMOTE || apply_rc=$?
set -euo pipefail
deploy() {
stage=$stage_remote

# Config sync: copy only where the content differs, and keep an on-box copy of
# whatever gets replaced (dated, alongside the file) so a hand rollback needs
# nothing from this host.
while IFS=\$'\t' read -r flat dest; do
  if cmp -s "\$stage/\$flat" "\$dest" 2>/dev/null; then continue; fi
  install -d -m 755 "\$(dirname "\$dest")"
  if [ -f "\$dest" ]; then cp -a "\$dest" "\$dest.bak-$ts"; fi
  install -m 644 -o root -g root "\$stage/\$flat" "\$dest"
  echo "updated \$dest"
done < "\$stage/MANIFEST"

# New release + atomic swap: \`ln -sfn\` is unlink-then-link, which leaves a
# window with no binary at all; a rename over the old link is the atomic form.
install -m 755 -o root -g root "\$stage/relay-agent.bin" "$RELEASES_DIR/relay-agent-$ts"
ln -sfn "$RELEASES_DIR/relay-agent-$ts" "$REMOTE_BIN.new"
mv -Tf "$REMOTE_BIN.new" "$REMOTE_BIN"

systemctl daemon-reload
# RELOAD, NEVER RESTART. The unit's ExecReload is \`nft -f /etc/nftables.conf\`:
# one atomic transaction that touches the static table only. A restart runs
# ExecStop, which destroys the agent's dynamic DNAT table and takes every live
# port-forwarding mapping down until the next generation change. Starting an
# inactive unit is a different thing and is fine: on a fresh box there is no
# dynamic table to lose yet.
if systemctl is-active -q nftables; then
  systemctl reload nftables
else
  systemctl enable --now nftables
fi
# Sysctl AFTER nftables, and tolerant of unknown keys: the nf_conntrack_* keys
# do not exist until the module is loaded, and on a fresh box the thing that
# loads it is the nat/ct ruleset above (the modules-load.d file only acts at
# boot, and the module must not be poked by hand on a live relay). Without
# \`-e\` the first deploy would abort here with the binary already swapped.
sysctl -e -q -p /etc/sysctl.d/99-pickle-forward.conf
# The agent, by contrast, is safe to bounce: it re-applies its persisted
# snapshot at start (while the snapshot is inside its max age) and reconverges
# on the next poll, so the data path survives the restart.
systemctl enable -q relay-agent
systemctl restart relay-agent
rm -rf "\$stage"
}
deploy
REMOTE

# --- health gate ------------------------------------------------------------
# Signals: the unit is up, the agent reached a converged state (not merely "did
# not crash"), its sync poll is not failing repeatedly, and user SSH on :22
# still answers end to end.
healthy=0; converged=0; sync_fail=0; state=""; journal=""
if [ "$apply_rc" != 0 ]; then
  echo "remote apply failed (exit $apply_rc)" >&2
else
  echo "==> health gate"
  # Scope the journal to THIS run of the unit. A time window cannot do it: the
  # restart happens seconds after any timestamp taken here, so the outgoing
  # process's own lines would sit inside the window and could pass the gate.
  invocation=$(rsh 'systemctl show -p InvocationID --value relay-agent' </dev/null 2>/dev/null || true)
  for _ in $(seq 1 "$HEALTH_TICKS"); do
    sleep 2
    state=$(rsh 'systemctl is-active relay-agent' </dev/null 2>/dev/null || true)
    [ "$state" = active ] || continue
    [ -n "$invocation" ] || \
      invocation=$(rsh 'systemctl show -p InvocationID --value relay-agent' </dev/null 2>/dev/null || true)
    [ -n "$invocation" ] || continue
    journal=$(rsh "sudo -n journalctl _SYSTEMD_INVOCATION_ID=$invocation --no-pager -o cat" \
              </dev/null 2>/dev/null || true)
    # INFO lines only, matched on the structured message field. A substring
    # match would pass on the failure text as well: the fatal line reads
    # `msg=fatal error="boot re-apply: ..."`, and a permanently failing sync
    # reports a generation mismatch whose message contains the word "applied".
    if printf '%s' "$journal" \
       | grep -qE 'level=INFO .*msg=(applied|"boot re-apply"|"no persisted snapshot)'; then
      converged=1
    fi
    # Negative signal. Boot convergence is logged before the first poll, so it
    # cannot speak for the sync transport; repeated failures can. Two, not one,
    # so a single transient (api restarting) does not fail an otherwise good
    # deploy. No early exit on the positive signal: the failures only appear a
    # poll or two in.
    sync_fail=$(printf '%s' "$journal" | grep -c 'msg="sync failed"' || true)
    sync_fail=${sync_fail:-0}
    [ "$sync_fail" -ge 2 ] && break
  done
  [ "$converged" = 1 ] && [ "$sync_fail" -lt 2 ] && healthy=1
fi

if [ "$healthy" != 1 ]; then
  echo "health check failed (unit=$state); rolling back the binary and the unit" >&2
  if [ "$converged" = 1 ] && [ "$sync_fail" -ge 2 ]; then
    echo "  The agent converged but its sync poll failed $sync_fail times in this run." >&2
    echo "  Check PICKLE_RELAY_SYNC_URL / PICKLE_RELAY_SYNC_TOKEN in the env file and" >&2
    echo "  the api's reachability over the tunnel before suspecting the binary." >&2
  fi
  [ -n "$journal" ] && printf 'last agent journal:\n%s\n' "$journal" >&2
  # Order matters: STOP the agent first, then swap the binary back, then start.
  # A restart over a live process would run the new binary once more before the
  # swap lands, and a plain stop leaves the agent's nft table in place, so the
  # forwarding data path stays up across the whole rollback.
  rollback_rc=0
  rsh "sudo -n bash -s" <<ROLLBACK || rollback_rc=$?
set -uo pipefail
rc=0
systemctl stop relay-agent || rc=1
if [ -e "$prev_release" ]; then
  if ln -sfn "$prev_release" "$REMOTE_BIN.rollback" && mv -Tf "$REMOTE_BIN.rollback" "$REMOTE_BIN"; then
    echo "restored $prev_release" >&2
  else
    echo "FAILED to restore the previous binary" >&2
    rc=1
  fi
else
  # First deploy: there is nothing to fall back to, so leave the unit DOWN and
  # disabled rather than crash-looping a binary that just failed its gate.
  systemctl disable --now relay-agent || true
  echo "WARNING: no previous release to roll back to; relay-agent left stopped and disabled" >&2
fi
if [ -f "$REMOTE_UNIT.bak-$ts" ]; then
  cp -a "$REMOTE_UNIT.bak-$ts" "$REMOTE_UNIT" || rc=1
  systemctl daemon-reload || rc=1
  echo "restored the previous unit file" >&2
fi
if [ -e "$prev_release" ]; then
  systemctl start relay-agent || rc=1
fi
rm -rf "$stage_remote"
exit \$rc
ROLLBACK
  if [ "$rollback_rc" != 0 ]; then
    echo "" >&2
    echo "*** ROLLBACK ITSELF FAILED (exit $rollback_rc) — the relay still has release" >&2
    echo "*** relay-agent-$ts live. Intervene by hand: point $REMOTE_BIN at" >&2
    echo "*** ${prev_release:-a known-good release} and restart relay-agent." >&2
  fi
  echo "note: /etc config files were NOT reverted; each replaced file kept a .bak-$ts copy on the relay" >&2
  exit 1
fi

# Regression gate: the port-forwarding plumbing shares this box with user SSH
# (HAProxy on :22, forwarded through the tunnel to the campus gateway). Read the
# banner rather than just completing a handshake — HAProxy in `mode tcp` accepts
# the client connection whether or not the backend is reachable, so a bare
# connect test would stay green through exactly the breakage this gate is for.
banner=$(timeout 10 bash -c "exec 3<>/dev/tcp/$RELAY_HOST/22 && head -c 4 <&3" 2>/dev/null || true)
if [ "$banner" != "SSH-" ]; then
  # Deliberately NOT rolled back. A :22 regression comes from the network
  # config, which the rollback does not revert anyway, so swapping the agent
  # binary back would only produce an old-binary/new-config pair nobody has
  # tested. Fail loudly and hand the operator the actual lever.
  echo "DEPLOY FAILED: relay :22 no longer answers with an SSH banner (got '${banner:-nothing}')" >&2
  echo "  The agent is healthy, so this is the relay network config." >&2
  echo "  Revert the offending file from its .bak-$ts copy on the relay and" >&2
  echo "  \`systemctl reload nftables\` (never restart), then re-verify :22." >&2
  exit 1
fi

echo "health OK (relay-agent active, snapshot applied, :22 answers)"
# Prune old releases, newest KEEP kept. The live symlink always points at the
# newest, so the tail is safe to remove. Floor of 1: KEEP=0 would delete the
# release the symlink points at.
# Pruning is housekeeping, never a deploy verdict: a failed `cd` here used to
# surface as a nonzero exit right after "health OK", which reads as a failed
# deploy.
[ "$KEEP" -ge 1 ] 2>/dev/null || KEEP=1
rsh "sudo -n bash -c 'cd $RELEASES_DIR 2>/dev/null || exit 0; ls -1dt relay-agent-* 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f --'" \
  </dev/null || echo "note: pruning old releases failed; the deploy itself is fine" >&2
echo "deployed relay-agent-$ts to $RELAY_HOST (previous releases under $RELEASES_DIR)"
