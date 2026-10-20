#!/usr/bin/env bash
# Run only offline fixtures for external health observation and restricted import.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONDONTWRITEBYTECODE=1 python3 -I -B scripts/tests/test_public_health.py
PYTHONDONTWRITEBYTECODE=1 python3 -I -B scripts/tests/test_health_result_transport.py
