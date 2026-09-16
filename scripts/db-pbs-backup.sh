#!/usr/bin/env bash
# Defaults to a plan. Backup, initialization and notifications are explicit modes.
set -euo pipefail
exec python3 "$(dirname "$0")/lib/db_pbs_backup.py" "$@"
