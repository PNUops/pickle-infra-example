#!/usr/bin/env bash
# Plan or stage a new candidate LLM gateway without enabling its application.
set -euo pipefail
exec python3 "$(dirname "$0")/lib/candidate_llm.py" "$@"
