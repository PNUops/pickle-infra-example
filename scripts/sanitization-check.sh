#!/usr/bin/env bash
# Checks that this copy still is a sanitized copy.
#
# The failure this exists for is not a wrong value appearing out of nowhere. It
# is a value from the original being pasted over the placeholder while mirroring
# a change — after which the two trees agree, and comparing them can never
# report anything. One such reversion sat here for two days and no check could
# have seen it, because every check that existed compared this tree against the
# one it was copied from.
#
# So the rule is about shape, and it is checked from this side alone. A public
# repository cannot hold a list of the values it must not contain; writing them
# down to search for them would publish them. What it can hold is the set of
# addresses that are allowed to appear at all:
#
#   - the ranges RFC 5737 reserves for documentation, which is what every
#     substituted address becomes;
#   - the private ranges this copy deliberately keeps, because a script whose
#     internal addressing has been blanked out no longer explains anything;
#   - loopback, the unspecified address, and a well-known public resolver.
#
# Anything else is a routable address that belongs to somebody, and in this tree
# that means an original value came along with a change.
set -euo pipefail

SANITIZE_FAIL=0
sfail() { echo "sanitization: $1" >&2; SANITIZE_FAIL=1; }

# addr_allowed ADDRESS → 0 when the address may appear in this tree.
addr_allowed() {
  case "$1" in
    # RFC 5737 documentation ranges — every substituted address lands here.
    192.0.2.* | 198.51.100.* | 203.0.113.*) return 0 ;;
    # Private addressing kept on purpose (RFC 1918).
    10.*) return 0 ;;
    192.168.*) return 0 ;;
    172.1[6-9].* | 172.2[0-9].* | 172.3[01].*) return 0 ;;
    # Loopback, unspecified, broadcast, and the public resolver used in examples.
    127.* | 0.0.0.0 | 255.255.255.* | 8.8.8.8 | 8.8.4.4) return 0 ;;
  esac
  return 1
}

sanitization_check() {
  SANITIZE_FAIL=0
  local file line addr count=0

  # 1. No routable address that belongs to anyone.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    file=${line%%:*}
    addr=${line#*:}
    addr_allowed "$addr" && continue
    sfail "$file carries $addr, which is neither a documentation range nor private addressing"
    # This file is the one place synthetic routable addresses belong: its
    # selftest needs values the rule must reject, so scanning itself would
    # report its own probes and the real finding would arrive buried in them.
  done < <(git ls-files -z | grep -zv '^scripts/sanitization-check\.sh$' \
    | xargs -0 grep -EoI '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' 2>/dev/null | sort -u)

  # 2. Every placeholder the README promises has to be somewhere in the tree.
  # A substitution that quietly stopped happening leaves its promise standing in
  # the table while nothing in the tree matches it — which is how a reverted
  # address went unnoticed while the document still claimed it was replaced.
  for addr in 192.0.2.10 203.0.113.10 203.0.113.20 198.51.100.10; do
    count=$(git ls-files -z | xargs -0 grep -lF "$addr" 2>/dev/null | wc -l)
    [ "$count" -gt 0 ] || sfail "README promises $addr but nothing in the tree uses it"
  done

  # 3. The administrative SSH port is one of the substituted facts. Checking the
  # variable defaults alone missed the places that write the port as a literal —
  # a runbook table, a sentence in prose — which is exactly where a mirrored
  # change deposits it. Both forms are checked, and by the same reasoning as the
  # addresses: this file cannot name the value it is looking for, so it names
  # the only value allowed to appear.
  while IFS= read -r line; do
    sfail "an administrative SSH port default is not the standard value: $line"
  done < <(git ls-files -z \
    | xargs -0 grep -EnI '(RELAY_SSH_PORT|SSH_PORT)="?\$\{[A-Z_]+:-[0-9]+\}' 2>/dev/null \
    | grep -vE ':-22\}')
  while IFS= read -r line; do
    sfail "an administrative SSH port is written out as something other than the standard one: $line"
  done < <(git ls-files -z | grep -zv '^scripts/sanitization-check\.sh$' \
    | xargs -0 grep -EnI '(admin(istrative)? ssh[^0-9]{0,24}|_SSH_PORT[^0-9]{0,8})[`:]?[0-9]+' 2>/dev/null \
    | grep -viE '(admin(istrative)? ssh[^0-9]{0,24}|_SSH_PORT[^0-9]{0,8})[`:]?22([^0-9]|$)')

  [ "$SANITIZE_FAIL" -eq 0 ] || return 1
  echo "sanitization OK"
}

# sanitization_selftest — proves the address rule can still fail. A check that
# only ever passes is indistinguishable from one that does nothing.
sanitization_selftest() {
  local probe
  # Synthetic routable addresses. The values this check exists to catch must
  # never be written down here — a list of what a public tree may not contain
  # publishes exactly that.
  for probe in 11.22.33.44 55.66.77.88 8.8.8.9; do
    if addr_allowed "$probe"; then
      echo "sanitization selftest: $probe would be accepted" >&2
      return 1
    fi
  done
  for probe in 203.0.113.20 198.18.1.10 100.64.0.2 127.0.0.1; do
    if ! addr_allowed "$probe"; then
      echo "sanitization selftest: $probe would be rejected" >&2
      return 1
    fi
  done
  echo "sanitization selftest OK"
}
