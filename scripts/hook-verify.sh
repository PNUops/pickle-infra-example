#!/usr/bin/env bash
# Per-commit verification for this repo, run by scripts/pre-commit.sh.
# Cheap here: shellcheck over the scripts plus the publication hygiene gate.
#
# Git exports GIT_DIR, GIT_INDEX_FILE and friends while a hook runs, and they
# follow every git command the verification makes. The hygiene selftest builds
# a throwaway repository and stages files in it; with those variables inherited
# it stages into the repository being committed instead, which empties that
# index. Clear them before handing over.
set -eu
cd "$(dirname "$0")/.."
while IFS= read -r v; do
  [ -n "$v" ] && unset "$v"
done < <(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p')
exec bash scripts/verify.sh
