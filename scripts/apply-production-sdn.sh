#!/usr/bin/env bash
# Only the explicit --apply switch permits API configuration changes.
set -euo pipefail
script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 -B "$script_dir/production-sdn.py" "$@"
