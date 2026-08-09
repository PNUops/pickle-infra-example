#!/usr/bin/env bash
# Builds the sshgw Go daemons on the host and deploys them into the sshgw LXC
# (102): sshgw-proxyfront (the :22 ingress shim), sshgw-route-plugin (the
# sshpiperd routing plugin) and sshgw-terminal-bridge (the web-terminal WS/SSH
# bridge). Syncs the sshgw repo's systemd units first (that repo owns the unit
# definitions), then backs up the running set, atomic-swaps all, restarts the
# services, health-checks, and rolls the WHOLE set back on failure — units
# included. Keeps the last N release sets. Mirrors deploy-api.sh's conventions.
# NOTE: a terminal-bridge swap closes live web-terminal sessions (1001 +
# BRIDGE_SHUTDOWN session-end audit) — users just reconnect.
#
# A broken sshgw breaks USER ssh only — host admin SSH on :22 is a separate,
# untouched path — but that is still a full user-facing outage, so rollback is
# taken seriously. The sshpiperd binary itself (stock v1.5.4) is installed by
# create-sshgw-lxc.sh and is NOT deployed here; only the three Pickle binaries
# (its unit file is synced here, since that unit is Pickle's own).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # resolved before any cd
CTID="${CTID:-102}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-sshgw
SSHGW_DIR="${SSHGW_DIR:-/root/pickle/sshgw}"
UNITS="sshpiperd.service sshgw-proxyfront.service sshgw-terminal-bridge.service"
BIN_DIR=/opt/pickle/sshgw/bin
RELEASES_DIR=/opt/pickle/sshgw/releases
KEEP="${KEEP:-5}"
HEALTH_TICKS="${HEALTH_TICKS:-15}"

cd "$SSHGW_DIR"
scripts/verify.sh                 # lint + gofmt + go vet/build/test gate
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
scripts/build.sh "$build"         # -> $build/sshgw-{proxyfront,route-plugin,terminal-bridge}
BINARIES="sshgw-proxyfront sshgw-route-plugin sshgw-terminal-bridge"
for b in $BINARIES; do
  [ -f "$build/$b" ] || { echo "deploy FAILED: $build/$b missing (build layout changed?)" >&2; exit 1; }
done

ts=$(date +%Y%m%d-%H%M%S)
pct exec "$CTID" -- mkdir -p "$RELEASES_DIR/$ts/units"

# Snapshot the live units BEFORE syncing the repo copies, so the rollback path
# below can undo a bad unit change and not only a bad binary.
pct exec "$CTID" -- bash -c "
for u in $UNITS; do
  if [ -f /etc/systemd/system/\$u ]; then cp -a /etc/systemd/system/\$u $RELEASES_DIR/$ts/units/\$u; fi
done"

# Unit definitions live in the sshgw repo; this is the step that carries them to
# the container, so a unit edit ships with the binary it belongs to.
"$HERE/sync-systemd-units.sh" "$CTID" \
  "$SSHGW_DIR/scripts/systemd/sshpiperd.service" \
  "$SSHGW_DIR/scripts/systemd/sshgw-proxyfront.service" \
  "$SSHGW_DIR/scripts/systemd/sshgw-terminal-bridge.service"

for b in $BINARIES; do
  pct push "$CTID" "$build/$b" "$BIN_DIR/$b.new"
done

# Atomic swap of the pair with backup-of-running + health-gated rollback. Health
# = both units active AND :22 (wg0) and :2222 (loopback) listening again.
pct exec "$CTID" -- bash -c "
set -euo pipefail
BIN=$BIN_DIR; REL=$RELEASES_DIR/$ts
for b in $BINARIES; do
  if [ -f \$BIN/\$b ]; then cp -a \$BIN/\$b \$REL/\$b; fi        # exact-bits backup (preserves pickle:pickle)
done
for b in $BINARIES; do
  chown pickle:pickle \$BIN/\$b.new
  chmod 0755 \$BIN/\$b.new
  mv \$BIN/\$b.new \$BIN/\$b
done
# route-plugin is exec'd by sshpiperd; restart it first, then the ingress shim,
# then the terminal bridge (independent of the SSH pair).
systemctl restart sshpiperd
systemctl restart sshgw-proxyfront
systemctl restart sshgw-terminal-bridge

ok=0
for _ in \$(seq 1 $HEALTH_TICKS); do
  sleep 2
  a_pf=\$(systemctl is-active sshgw-proxyfront || true)
  a_sp=\$(systemctl is-active sshpiperd || true)
  l22=\$(ss -tln | grep -c '100.64.0.2:22' || true)
  l2222=\$(ss -tln | grep -c '127.0.0.1:2222' || true)
  ok_bridge=1
  a_tb=\$(systemctl is-active sshgw-terminal-bridge || true)
  l8082=\$(ss -tln | grep -c ':8082' || true)
  l8083=\$(ss -tln | grep -c ':8083' || true)
  if [ \"\$a_tb\" != active ] || [ \"\$l8082\" -lt 1 ] || [ \"\$l8083\" -lt 1 ]; then ok_bridge=0; fi
  if [ \"\$a_pf\" = active ] && [ \"\$a_sp\" = active ] && [ \"\$l22\" -ge 1 ] && [ \"\$l2222\" -ge 1 ] && [ \$ok_bridge -eq 1 ]; then ok=1; break; fi
done
if [ \$ok -eq 1 ]; then echo 'health OK (services active, expected ports listening)'; exit 0; fi

echo 'health check failed; rolling back the set' >&2
restored=0
for b in $BINARIES; do
  if [ -f \$REL/\$b ]; then cp -a \$REL/\$b \$BIN/\$b; restored=1; fi
done
if ls \$REL/units/*.service >/dev/null 2>&1; then
  cp -a \$REL/units/*.service /etc/systemd/system/
  systemctl daemon-reload
  restored=1
  echo 'restored the previous unit files' >&2
fi
if [ \$restored -eq 1 ]; then
  systemctl restart sshpiperd; systemctl restart sshgw-proxyfront
  systemctl restart sshgw-terminal-bridge
  echo 'rolled back to the previous binaries and units' >&2
else
  echo 'WARNING: first deploy — no previous set to roll back to; investigate' >&2
fi
exit 1
"
# prune old release sets, keep the newest N
pct exec "$CTID" -- bash -c "cd $RELEASES_DIR && ls -dt */ 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -rf --"
echo "deployed sshgw binaries $ts (previous set saved under $RELEASES_DIR/$ts)"
