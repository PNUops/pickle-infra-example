#!/usr/bin/env bash
# Installs the shared git hooks (secret-scan pre-commit + commit-msg
# message-convention check). With no arguments it installs
# into THIS (infra) repo. Given one or more repo roots it installs the SAME
# hardened hook into each — cover every workspace repo with one command:
#
#   scripts/setup-hooks.sh <repo-root> [<repo-root> ...]
#
# The hook is self-contained (pure git plumbing), so a single shared copy works
# in every repo regardless of that repo's own scripts/.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_SRC="$here/scripts/pre-commit.sh"
MSG_SRC="$here/scripts/commit-msg.sh"

targets=("$@")
[ "${#targets[@]}" -eq 0 ] && targets=("$here")

for repo in "${targets[@]}"; do
  if [ ! -d "$repo/.git" ]; then
    echo "skip: $repo has no .git dir (not a standard repo checkout)" >&2
    continue
  fi
  install -m 0755 "$HOOK_SRC" "$repo/.git/hooks/pre-commit"
  install -m 0755 "$MSG_SRC" "$repo/.git/hooks/commit-msg"
  echo "installed hooks -> $repo/.git/hooks/{pre-commit,commit-msg}"
done
