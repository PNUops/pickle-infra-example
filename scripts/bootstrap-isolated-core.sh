#!/usr/bin/env bash
# Plan by default; host/container changes require the explicit --apply flag.
set -euo pipefail
exec python3 "$(dirname "$0")/lib/isolated_core.py" "$@"
