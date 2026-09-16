#!/usr/bin/env bash
# The rollback timer always invokes this wrapper with an explicit interpreter.
set -euo pipefail
script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 -B "$script_dir/production-network.py" "$@"
