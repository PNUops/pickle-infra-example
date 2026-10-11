#!/usr/bin/env bash
# Reload only after the selected wildcard lineage has renewed.
set -euo pipefail
[[ ${RENEWED_LINEAGE:-} == /etc/letsencrypt/live/example.com ]] || exit 0
nginx -t
systemctl reload nginx
