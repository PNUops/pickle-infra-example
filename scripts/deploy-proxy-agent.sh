#!/usr/bin/env bash
# Builds pickle-proxy-agent on the host and deploys it into the reverse-proxy LXC
# (100). Syncs the repo's systemd unit, backs up the running binary and unit,
# atomic-swaps, restarts, health-checks via the agent's own /status endpoint, and
# rolls both back automatically on failure. Keeps the last N of each. Mirrors
# deploy-api.sh's argument/env conventions.
#
# The daemon renders /etc/nginx/pickle.d/*.conf, but nginx keeps serving its
# current config if the daemon is down — so a failed deploy does not drop live
# traffic. A bad binary is still rolled back so route pushes from pickle-api
# (/apply, /sync-all) keep working. The target host must already have nginx with
# the base include and the wildcard certificate in place.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"   # resolved before any cd
CTID="${CTID:-100}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" reverse-proxy
AGENT_DIR="${AGENT_DIR:-/root/pickle/proxy-agent}"
GO="${GO:-/usr/local/go/bin/go}"
REMOTE_BIN=/usr/local/bin/pickle-proxy-agent
REMOTE_UNIT=/etc/systemd/system/pickle-proxy-agent.service
RELEASES_DIR=/opt/pickle/proxy-agent/releases
KEEP="${KEEP:-5}"
# x2s per tick. The daemon has no migrations and starts fast; 15 ticks = 30s.
HEALTH_TICKS="${HEALTH_TICKS:-15}"

cd "$AGENT_DIR"
scripts/verify.sh                 # lint + go vet + go build + go test gate

ts=$(date +%Y%m%d-%H%M%S)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
echo "==> building static linux/amd64 binary"
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  "$GO" build -trimpath -ldflags="-s -w" -o "$build/pickle-proxy-agent" ./cmd/proxy-agent

pct exec "$CTID" -- mkdir -p "$RELEASES_DIR"

# Snapshot the live unit before syncing the repo copy, so a bad unit change can
# be rolled back with the binary.
pct exec "$CTID" -- bash -c "
if [ -f $REMOTE_UNIT ]; then cp -a $REMOTE_UNIT $RELEASES_DIR/pickle-proxy-agent-$ts.unit.prev; fi"

# The proxy-agent repo owns the unit definition; this is the step that carries it
# to the container, so a unit edit ships with the binary it belongs to.
"$HERE/sync-systemd-units.sh" "$CTID" \
  "$AGENT_DIR/scripts/proxy-agent.service:pickle-proxy-agent.service"

pct push "$CTID" "$build/pickle-proxy-agent" "$REMOTE_BIN.new"

# Backup-of-running + atomic swap + restart, on the agent's own container.
pct exec "$CTID" -- bash -c "
set -euo pipefail
mkdir -p $RELEASES_DIR
if [ -f $REMOTE_BIN ]; then cp -a $REMOTE_BIN $RELEASES_DIR/pickle-proxy-agent-$ts.prev; fi
chmod 0755 $REMOTE_BIN.new
mv $REMOTE_BIN.new $REMOTE_BIN
systemctl restart pickle-proxy-agent
"

# Health probe FROM LXC 101 (198.18.1.20) — the agent's /status fails closed on
# both the bearer token AND the source-IP allowlist (only pickle-api is allowed),
# so a probe from the agent's own container returns 403 even when healthy. Read
# URL+token from LXC 101 api.env; HTTP 200 = healthy.
healthy=0; code=""
for _ in $(seq 1 "$HEALTH_TICKS"); do
  sleep 2
  # shellcheck disable=SC2016
  code=$(pct exec 101 -- sh -c 'U=$(grep "^PICKLE_PROXY_AGENT_URL=" /etc/pickle/api.env | cut -d= -f2); T=$(grep "^PICKLE_PROXY_AGENT_TOKEN=" /etc/pickle/api.env | cut -d= -f2); curl -s -o /dev/null -w "%{http_code}" --max-time 5 -H "Authorization: Bearer $T" "$U/status"' 2>/dev/null || true)
  if [ "$code" = 200 ]; then healthy=1; break; fi
done

if [ "$healthy" != 1 ]; then
  echo "health check failed (last /status=$code); rolling back" >&2
  pct exec "$CTID" -- bash -c "
set -euo pipefail
restored=0
if [ -f $RELEASES_DIR/pickle-proxy-agent-$ts.unit.prev ]; then
  cp -a $RELEASES_DIR/pickle-proxy-agent-$ts.unit.prev $REMOTE_UNIT
  systemctl daemon-reload
  restored=1
  echo 'restored the previous unit file' >&2
fi
if [ -f $RELEASES_DIR/pickle-proxy-agent-$ts.prev ]; then
  cp -a $RELEASES_DIR/pickle-proxy-agent-$ts.prev $REMOTE_BIN
  restored=1
fi
if [ \$restored -eq 1 ]; then
  systemctl restart pickle-proxy-agent
  echo 'rolled back to the previous binary and unit' >&2
else
  echo 'WARNING: first deploy — nothing to roll back to; investigate' >&2
fi
"
  exit 1
fi
echo "health OK (/status 200 from LXC 101)"
# prune old backups, keep the newest N of each kind (binary, unit)
pct exec "$CTID" -- bash -c "cd $RELEASES_DIR && ls -t pickle-proxy-agent-*[0-9].prev 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm --"
pct exec "$CTID" -- bash -c "cd $RELEASES_DIR && ls -t pickle-proxy-agent-*.unit.prev 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm --"
echo "deployed pickle-proxy-agent-$ts (previous binary saved under $RELEASES_DIR)"
