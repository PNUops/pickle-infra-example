#!/usr/bin/env bash
# Per-commit verification for this repo, run by the commit hook when it is
# executable. Cheap here: shellcheck over the scripts and the sample checks.
#
# Git exports GIT_DIR, GIT_INDEX_FILE and friends while a hook runs, and they
# follow every git command the verification makes. A selftest that builds a
# throwaway repository and stages files in it would, with those variables
# inherited, stage into the repository being committed instead and empty that
# index. Clear them before handing over.
set -eu
cd "$(dirname "$0")/.."
while IFS= read -r v; do
  [ -n "$v" ] && unset "$v"
done < <(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p')
exec bash scripts/verify.sh
