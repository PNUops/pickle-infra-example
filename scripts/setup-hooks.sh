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
  # Ask git where the hooks live instead of assuming <repo>/.git/hooks. A
  # linked worktree keeps a .git FILE, not a directory, and its hooks resolve
  # to the main checkout's shared directory: the old test skipped such a path
  # with "no .git dir", which reads as "this tree has no hooks" when in fact it
  # runs the ones installed here.
  if ! hooks=$(git -C "$repo" rev-parse --git-path hooks 2>/dev/null); then
    echo "skip: $repo is not a git checkout" >&2
    continue
  fi
  case "$hooks" in /*) ;; *) hooks="$repo/$hooks" ;; esac
  mkdir -p "$hooks"
  install -m 0755 "$HOOK_SRC" "$hooks/pre-commit"
  install -m 0755 "$MSG_SRC" "$hooks/commit-msg"
  echo "installed hooks -> $hooks/{pre-commit,commit-msg}"
done
