#!/usr/bin/env bash
# Verification gate: shellcheck every script in this repo.
set -euo pipefail
cd "$(dirname "$0")/.."
mapfile -t scripts < <(find . -name '*.sh' -not -path './.git/*')
shellcheck "${scripts[@]}"

# This tree is a sanitized copy, and the way it stops being one is that a value
# from the original arrives with a mirrored change. Once that happens the two
# trees agree and no comparison between them can notice, so the check runs from
# this side and asks only what this side can answer.
# shellcheck source=scripts/sanitization-check.sh
. scripts/sanitization-check.sh
sanitization_selftest
sanitization_check

# Scheduled units must not depend on a script's execute bit. Cron entries did,
# and because two of the scripts are committed non-executable every backup and
# health run died at exec for ten days with the error going nowhere. A unit that
# names a repo script directly would reintroduce exactly that.
unit_fail=0
# Materialise the list first, then refuse an empty one. `find hosts … 2>/dev/null`
# piped straight into the loop walks zero units when `hosts/` is renamed or
# absent and reports success -- and the count printed at the end comes from the
# lint array instead, so the number corroborates the lie. A tree with no units
# is a broken checkout, not a clean one.
units_list="$(mktemp)"
trap 'rm -f "$units_list"' EXIT
find hosts -name '*.service' > "$units_list" 2>/dev/null || true
if [ ! -s "$units_list" ]; then
  echo "verify: no systemd units found under hosts/ — the search itself is broken" >&2
  exit 1
fi
unit_count=$(grep -c '' "$units_list")
while IFS= read -r unit; do
  while IFS= read -r line; do
    # The command word is whatever follows the directive, minus systemd's
    # optional prefix characters (-, @, :, +, !).
    cmd=${line#*=}
    cmd=${cmd#"${cmd%%[![:space:]]*}"}
    cmd=$(printf '%s' "$cmd" | sed 's/^[-@:+!]*//')
    case "$cmd" in
      /bin/sh*|/bin/bash*|/usr/bin/env*|/usr/bin/bash*|/usr/bin/sh*) continue ;;
    esac
    case "$cmd" in
      *"/infra/scripts/"*)
        echo "verify: $unit runs a repo script without an interpreter: $cmd" >&2
        echo "        prefix it with /bin/bash so the execute bit cannot break the schedule" >&2
        unit_fail=1
        ;;
    esac
  done < <(grep -hE '^(ExecStart|ExecStartPre|ExecStartPost|ExecStop|ExecReload)=' "$unit" || true)
done < "$units_list"
[ "$unit_fail" -eq 0 ] || { echo "verify: scheduled-unit check failed" >&2; exit 1; }

echo "infra-example verify OK (${#scripts[@]} scripts, $unit_count scheduled units)"
