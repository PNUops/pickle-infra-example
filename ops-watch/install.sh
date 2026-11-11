#!/usr/bin/env bash
# Install ops-watch on one host. Run as root from a checkout of this repository.
#
#   install.sh agent           copy the package and the agent units (every host)
#   install.sh judge           also create the judge account, directories and units
#   install.sh enable-agent    enable and start the agent timer
#   install.sh enable-judge    enable and start the judge timer
#   install.sh disable         stop and disable both timers (rollback step)
#
# Configuration and secrets are never written here. They are placed by hand
# under /etc/pickle-ops-watch as the runbook describes, and this script refuses
# to enable a timer whose configuration is missing.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/usr/local/lib/pickle-ops-watch
UNITS=/etc/systemd/system
CONF=/etc/pickle-ops-watch

[ "$(id -u)" -eq 0 ] || { echo "install.sh: run as root" >&2; exit 1; }

copy_package() {
  install -d -m 0755 "$PREFIX" "$PREFIX/bin" "$PREFIX/opswatch" "$PREFIX/opswatch/checks" \
    "$PREFIX/catalog" "$PREFIX/catalog/items" "$PREFIX/status-page"
  install -m 0755 "$SRC/bin/ops-watch" "$PREFIX/bin/ops-watch"
  install -m 0644 "$SRC"/opswatch/*.py "$PREFIX/opswatch/"
  install -m 0644 "$SRC"/opswatch/checks/*.py "$PREFIX/opswatch/checks/"
  install -m 0644 "$SRC/catalog/hosts.json" "$PREFIX/catalog/hosts.json"
  # Replace the item set exactly, so an item removed from the repository stops
  # being checked instead of lingering on the host.
  find "$PREFIX/catalog/items" -maxdepth 1 -name '*.json' -delete
  install -m 0644 "$SRC"/catalog/items/*.json "$PREFIX/catalog/items/"
  install -m 0644 "$SRC/status-page/index.html" "$PREFIX/status-page/index.html"
  # Create the configuration directories once and never re-mode them: on the
  # judge host they are 0711 so the judge account can reach its own files,
  # and re-running `install.sh agent` there must not lock it out.
  [ -d "$CONF" ] || install -d -m 0700 "$CONF"
  [ -d "$CONF/secrets" ] || install -d -m 0700 "$CONF/secrets"
  /usr/bin/python3 -I -B "$PREFIX/bin/ops-watch" render-samples --catalog "$PREFIX/catalog" >/dev/null
}

install_units() {
  local unit
  for unit in "$@"; do
    install -m 0644 "$SRC/systemd/$unit" "$UNITS/$unit"
  done
  systemctl daemon-reload
}

case "${1:-}" in
  agent)
    copy_package
    install_units pickle-ops-watch-agent.service pickle-ops-watch-agent.timer
    ;;
  judge)
    copy_package
    if ! id opswatch >/dev/null 2>&1; then
      useradd --system --home-dir /home/opswatch --create-home --shell /bin/sh opswatch
    fi
    install -d -m 0755 -o opswatch -g opswatch /var/www/pickle-status
    install -m 0644 "$PREFIX/status-page/index.html" /var/www/pickle-status/index.html
    install -d -m 0700 -o opswatch -g opswatch /var/lib/pickle-ops-watch
    install -d -m 0700 -o opswatch -g opswatch /home/opswatch/.ssh
    # The judge account reads judge.json and the webhook, each owned by it with
    # mode 0600; the directories only need to be traversable.
    chmod 0711 "$CONF" "$CONF/secrets"
    install_units pickle-ops-watch-judge.service pickle-ops-watch-judge.timer \
      pickle-ops-watch-agent.service pickle-ops-watch-agent.timer
    ;;
  enable-agent)
    [ -f "$CONF/agent.json" ] || { echo "install.sh: $CONF/agent.json is missing" >&2; exit 1; }
    systemctl enable --now pickle-ops-watch-agent.timer
    ;;
  enable-judge)
    [ -f "$CONF/judge.json" ] || { echo "install.sh: $CONF/judge.json is missing" >&2; exit 1; }
    systemctl enable --now pickle-ops-watch-judge.timer
    ;;
  disable)
    systemctl disable --now pickle-ops-watch-agent.timer pickle-ops-watch-judge.timer 2>/dev/null || true
    ;;
  *)
    echo "usage: install.sh agent|judge|enable-agent|enable-judge|disable" >&2
    exit 2
    ;;
esac
