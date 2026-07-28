#!/usr/bin/env bash
# Pushes systemd unit files from the workspace service repos into a running LXC
# and reloads systemd when anything changed.
#
# Why this exists: unit definitions are owned by the service repos (sshgw and
# proxy-agent ship their own .service files), but for a long time nothing carried
# them to a running container — the deploy scripts pushed binaries only and the
# live units stayed frozen at container-creation time, so every repo-side unit
# edit was silently never applied. This script is that missing path; the deploy
# scripts call it before they restart anything.
#
# Usage: sync-systemd-units.sh <ctid> <src>[:<unit-name>] ...
#   <src>        path to the unit file in the owning repo
#   <unit-name>  installed name when it differs from the file name
#                (proxy-agent.service -> pickle-proxy-agent.service)
#
# Idempotent: an unchanged unit is left alone. A unit that did not exist on the
# target yet is also `systemctl enable`d, so a first install survives a reboot;
# an already-present unit keeps whatever enable state the operator gave it.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $(basename "$0") <ctid> <src>[:<unit-name>] ..." >&2
  exit 2
fi

CTID="$1"; shift
changed=0

for spec in "$@"; do
  src="${spec%%:*}"
  name="${spec#*:}"
  if [ "$name" = "$spec" ]; then
    name="$(basename "$src")"
  fi
  [ -f "$src" ] || { echo "sync-systemd-units: $src not found" >&2; exit 1; }
  dest="/etc/systemd/system/$name"

  existed=1
  pct exec "$CTID" -- test -f "$dest" || existed=0
  if [ "$existed" = 1 ] && pct exec "$CTID" -- cat "$dest" 2>/dev/null | diff -q - "$src" >/dev/null 2>&1; then
    echo "unit $name unchanged"
    continue
  fi

  pct push "$CTID" "$src" "$dest" --perms 644
  echo "installed $dest (from $src)"
  changed=1
  # Reload per unit rather than once at the end: if a later push fails, set -e
  # aborts the script, and a batch reload would never run — leaving units on disk
  # that systemd has not read.
  pct exec "$CTID" -- systemctl daemon-reload
  if [ "$existed" = 0 ]; then
    pct exec "$CTID" -- systemctl enable -q "$name"
    echo "enabled $name (new unit)"
  fi
done

if [ "$changed" = 1 ]; then
  echo "daemon-reload done on LXC $CTID"
fi
