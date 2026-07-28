#!/usr/bin/env bash
# Fast pre-commit secret scan. Scans the STAGED BLOB CONTENT via `git show :path`
# (the index object), NOT the working tree — so a secret that was staged and then
# edited/reverted in the working copy is still caught, and an unstaged
# working-tree secret does not block an unrelated commit. Full build/tests run
# via scripts/verify.sh before each commit batch.
set -euo pipefail

# Regexes tuned for near-zero false positives. A bare "PVEAPIToken=" placeholder
# and angle-bracket placeholders like <relay-wg-private-key> never match — only
# real secret shapes do.
patterns=(
  -e 'BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY'
  # Proxmox API token WITH a real uuid secret (bare literal placeholder allowed).
  -e 'PVEAPIToken=[^ =]+![^ =]+=[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
  -e 'ghp_[A-Za-z0-9]{36}'
  -e 'AKIA[0-9A-Z]{16}'
  # WireGuard / preshared key: a 32-byte value is 43 base64 chars + '=' on a
  # PrivateKey/PresharedKey line (covers wg0.conf secrets; placeholders excluded).
  -e '(PrivateKey|PresharedKey)[[:space:]]*=[[:space:]]*[A-Za-z0-9+/]{43}='
)

mapfile -t files < <(git diff --cached --name-only --diff-filter=ACM)
blob=$(mktemp)
trap 'rm -f "$blob"' EXIT
hit=0
for path in "${files[@]}"; do
  [ -n "$path" ] || continue
  # Skip the hook script itself: its pattern definitions must not self-trip.
  case "$path" in scripts/pre-commit.sh) continue ;; esac
  # Read the index object into a file first. Piping `git show` straight into
  # grep threw away git's exit status, so a blob that could not be read produced
  # grep's "no match" and the file passed as clean without ever being scanned.
  # An unreadable staged object is an unscanned file, so it blocks the commit.
  if ! git show ":$path" >"$blob" 2>/dev/null; then
    echo "pre-commit: cannot read staged content of: $path (not scanned)" >&2
    hit=1
    continue
  fi
  if grep -EIq "${patterns[@]}" "$blob"; then
    echo "pre-commit: possible secret in staged content of: $path" >&2
    hit=1
  fi
done

if [ "$hit" = 1 ]; then
  echo "pre-commit: aborting (secret pattern in staged content, or content that could not be scanned). Use --no-verify only for a confirmed false positive." >&2
  exit 1
fi
exit 0
