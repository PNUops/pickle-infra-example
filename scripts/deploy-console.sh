#!/usr/bin/env bash
# Builds pickle-console on the host and deploys the static bundle into the
# pickle-app LXC nginx root.
set -euo pipefail

CTID="${CTID:-101}"
CONSOLE_DIR="${CONSOLE_DIR:-/root/pickle/console}"
WEB_ROOT=/var/www/pickle-console

cd "$CONSOLE_DIR"
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
# post-deploy check: SPA must answer 200 on / (roll back on failure)
if ! curl -fsS -o /dev/null http://127.0.0.1/; then
  echo 'console health check failed; rolling back' >&2
  if [ -d $WEB_ROOT.old ]; then
    rm -rf $WEB_ROOT.failed && mv $WEB_ROOT $WEB_ROOT.failed && mv $WEB_ROOT.old $WEB_ROOT
    systemctl reload nginx
  fi
  exit 1
fi
"
rm -f /tmp/pickle-console-dist.tgz
echo "console deployed"
