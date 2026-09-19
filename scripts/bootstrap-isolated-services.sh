#!/usr/bin/env bash
# Candidate proxy/SSH-gateway LXC bootstrap. Plans by default; --apply is explicit.
set -euo pipefail
exec python3 "$(dirname "$0")/lib/isolated_services.py" "$@"
