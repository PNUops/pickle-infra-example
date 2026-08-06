#!/usr/bin/env bash
# Builds pickle-api on the host and deploys it into the pickle-app LXC.
# Keeps the last 5 releases; rolls back automatically if the health check fails.
set -euo pipefail

CTID="${CTID:-101}"
API_DIR="${API_DIR:-/root/pickle/api}"
RELEASES_DIR=/opt/pickle/api/releases
HEALTH_URL="http://127.0.0.1:8080/actuator/health"

cd "$API_DIR"
scripts/verify.sh
# `clean` is not optional here. Maven copies resources into target/ but never
# removes ones that have left the source tree, so a renamed or deleted migration
# stays behind and ships inside the jar next to its replacement. Flyway then
# refuses to start with "Found more than one migration with version N" — the api
# does not come up at all, and the deploy's own health check rolls back to a jar
# that cannot explain why. Rebuilding from scratch costs a couple of minutes and
# removes the whole class of failure.
mvn -q -DskipTests clean package
jar=$(find target -maxdepth 1 -name 'pickle-api-*.jar' | head -1)
[ -n "$jar" ] || { echo "deploy FAILED: no pickle-api-*.jar under target/ (build layout changed?)" >&2; exit 1; }
ts=$(date +%Y%m%d-%H%M%S)

# Health deadline is parameterizable: Flyway runs inside the new jar's startup,
# so long migrations need a longer window — killing the JVM mid-migration via
# the rollback restart leaves a Flyway lock. 30 ticks x 2s = 60s default.
HEALTH_TICKS="${HEALTH_TICKS:-30}"

pct exec "$CTID" -- mkdir -p "$RELEASES_DIR"
pct push "$CTID" "$jar" "$RELEASES_DIR/pickle-api-$ts.jar"

# NOTE: the health-check rollback below restores the previous JAR only — Flyway
# migrations the new jar already applied stay in the DB. This is safe only under
# the expand/contract rule: migrations must be backward-compatible
# with the previous release's jar. A deploy that breaks that rule has no automatic
# rollback — take a DB backup point first (see runbook).

pct exec "$CTID" -- bash -c "
set -e
cd /opt/pickle/api
prev=\$(readlink current.jar 2>/dev/null || true)
ln -sfn $RELEASES_DIR/pickle-api-$ts.jar current.jar
systemctl restart pickle-api
for i in \$(seq 1 $HEALTH_TICKS); do
  sleep 2
  if curl -fsS $HEALTH_URL >/dev/null 2>&1; then echo 'health OK'; exit 0; fi
done
echo 'health check failed; rolling back' >&2
if [ -n \"\$prev\" ]; then
  ln -sfn \"\$prev\" current.jar; systemctl restart pickle-api
else
  echo 'WARNING: first deploy — no previous release to roll back to; service left stopped-ish, investigate' >&2
fi
exit 1
"
# prune old releases
pct exec "$CTID" -- bash -c "cd $RELEASES_DIR && ls -t | tail -n +6 | xargs -r rm --"
echo "deployed pickle-api-$ts.jar"
