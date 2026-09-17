#!/usr/bin/env bash
# Builds pickle-api on the host and deploys it into the pickle-app LXC.
# Keeps the last 5 releases; rolls back automatically if readiness fails.
set -euo pipefail

CTID="${CTID:-101}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
API_DIR="${API_DIR:-/srv/pickle/api}"
RELEASES_DIR=/opt/pickle/api/releases
# Deployment asks whether the new application finished startup and accepts
# traffic, not whether every external dependency is reachable at that instant.
# The aggregate endpoint includes the SMTP health indicator: a transient
# DNS/provider outage once rolled back a
# healthy, already-migrated jar and left its JobRunr targets for the old jar to
# misread. Whole-system health still probes the aggregate endpoint separately.
readonly HEALTH_URL="http://127.0.0.1:8080/actuator/health/readiness"

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

# Readiness deadline is parameterizable: Flyway runs inside the new jar's startup,
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
# The journal is printed here, before the rollback restart: the rollback
# relinks and restarts at once, so the old jar's startup lines would otherwise
# be the last thing in the journal and the real exception would never be seen.
# Without this the operator gets only the line above, and a missing env key
# reads exactly like a bad database password.
#
# A line count, never a time window. The container clock is UTC, and a KST
# timestamp handed to a journalctl time window once returned no entries, which
# was read as no errors. A count needs no clock, and the failed attempt is by
# construction the most recent lines. systemd's own restart-counter lines come
# with it, and those are what separate a crash loop from a migration still
# running.
#
# The trailing true keeps a journal hiccup from aborting the rollback below.
#
# This output reaches the deploy workflow log, which is read by more people
# than root on this host. A startup guard's message must name the shape of a
# rejected value and never the value itself.
echo '--- pickle-api journal, last 150 lines ---' >&2
journalctl -u pickle-api -n 150 --no-pager >&2 || true
echo '--- end journal ---' >&2
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
