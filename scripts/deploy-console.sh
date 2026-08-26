#!/usr/bin/env bash
# Builds pickle-console on the host and deploys the static bundle into the
# pickle-app LXC nginx root.
set -euo pipefail

CTID="${CTID:-101}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
CONSOLE_DIR="${CONSOLE_DIR:-/srv/pickle/console}"
WEB_ROOT=/var/www/pickle-console

cd "$CONSOLE_DIR"
# Install what the lockfile says before building. Without this the build runs
# against whatever node_modules the previous deploy left behind, so a branch
# that adds a dependency fails here on a missing module, and the first thing to
# notice is the type checker. `ci` rather than `install`: it installs the
# lockfile exactly and refuses to proceed when the lockfile and package.json
# disagree, which is the state a half-finished dependency change leaves.
# `ci` empties node_modules before it fills it, so a registry outage on a cold
# cache leaves the checkout unbuildable: prefer the cache where it can. The
# audit that gates this build is the one verify.sh runs, not this one.
npm ci --prefer-offline --no-audit --no-fund
scripts/verify.sh

tar -czf /tmp/pickle-console-dist.tgz -C dist .
pct exec "$CTID" -- bash -c "mkdir -p $WEB_ROOT.new && rm -rf $WEB_ROOT.new/*"
pct push "$CTID" /tmp/pickle-console-dist.tgz /tmp/pickle-console-dist.tgz
pct exec "$CTID" -- bash -c "
set -e
tar -xzf /tmp/pickle-console-dist.tgz -C $WEB_ROOT.new
rm -rf $WEB_ROOT.old
if [ -d $WEB_ROOT ]; then mv $WEB_ROOT $WEB_ROOT.old; fi
mv $WEB_ROOT.new $WEB_ROOT
rm -f /tmp/pickle-console-dist.tgz
nginx -t && systemctl reload nginx
# post-deploy check. Fetching / caught a web root with nothing in it, because
# try_files falls back to /index.html and an absent index.html is a 404. What it
# could not catch is a root that has the index and not the bundle it names: the
# index loads, and every asset path under it gets the SPA shell at 200 instead
# of a 404. So follow the index to its bundle and look at what comes back.
rollback() {
  echo \"console health check failed: \$1\" >&2
  if [ -d $WEB_ROOT.old ]; then
    rm -rf $WEB_ROOT.failed && mv $WEB_ROOT $WEB_ROOT.failed && mv $WEB_ROOT.old $WEB_ROOT
    systemctl reload nginx
    echo 'rolled back to the previous bundle' >&2
  else
    # First deploy into this container, or one whose .old was already consumed.
    # Saying 'rolling back' here and doing nothing is worse than the failure.
    echo 'no previous bundle to roll back to: the web root is left as deployed' >&2
  fi
  exit 1
}
INDEX=\$(curl -fsS http://127.0.0.1/) || rollback 'index did not load'
# Vite emits the entry script before its modulepreload links, so the first match
# is the app bundle. Either would do: both are assets that have to be there.
ASSET=\$(printf '%s' \"\$INDEX\" | grep -oE '/assets/[^\"]+\.js' | head -n 1)
[ -n \"\$ASSET\" ] || rollback 'index names no /assets/*.js bundle'
# Size first: it is always one numeric field, where a content type may carry a
# charset parameter and take two.
PROBE=\$(curl -fsS -o /dev/null -w '%{size_download} %{content_type}' \
  \"http://127.0.0.1\$ASSET\") || rollback \"bundle \$ASSET did not load\"
read -r SIZE TYPE <<<\"\$PROBE\"
case \"\$TYPE\" in
  *javascript*|*ecmascript*) ;;
  *) rollback \"bundle \$ASSET is served as \$TYPE, not a script\" ;;
esac
# A truncated write leaves a real .js file that nginx types correctly, so the
# type alone still passes it. This is a floor, not a measurement: the app bundle
# is hundreds of kilobytes and nothing it could legitimately shrink to is here.
[ \"\${SIZE:-0}\" -gt 10000 ] || rollback \"bundle \$ASSET is \$SIZE bytes\"
"
rm -f /tmp/pickle-console-dist.tgz
echo "console deployed"
