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

# is_ipv6 CANDIDATE → 0 when a colon-separated token is an address at all.
# Colons are common punctuation: 22:30:15 is a time and 2c:54:91:ab:cd:ef is a
# hardware address, and both begin with the hex digits that the global-unicast
# rule refuses. Without this the check called a log timestamp a public address —
# a false statement in the message of a gate, which is worse than a miss. An
# address either carries the :: that stands for omitted groups, or writes all
# eight groups out; a time has three groups and a hardware address six, neither
# with ::.
is_ipv6() {
  case "$1" in
    *::*) return 0 ;;
  esac
  [ "$(printf '%s' "$1" | tr -cd ':' | wc -c)" -eq 7 ]
}

# addr6_allowed ADDRESS → 0 when an IPv6 address may appear in this tree.
# There is none here today, which is exactly why the rule is written now: the
# first one to arrive would arrive unexamined. Addresses are extracted in their
# compressed form too — a rule that only saw the fully written-out form would
# miss almost every address anyone actually writes. Documentation, loopback,
# unspecified, link-local and unique-local are fine; a global unicast address
# (2000::/3) belongs to somebody.
addr6_allowed() {
  local a
  a=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$a" in
    2001:db8:* | 2001:0db8:*) return 0 ;;   # RFC 3849 documentation
    ::1 | :: | ::/*) return 0 ;;
    fe80:*) return 0 ;;                      # link-local
    fc??:* | fd??:*) return 0 ;;             # unique-local
    2* | 3*) return 1 ;;                     # global unicast
  esac
  return 0
}

# addr_allowed ADDRESS → 0 when the address may appear in this tree.
addr_allowed() {
  case "$1" in
    # RFC 5737 documentation ranges — the public-facing substitutes.
    192.0.2.* | 198.51.100.* | 203.0.113.*) return 0 ;;
    # RFC 2544 benchmarking space stands in for the internal bridges and RFC
    # 6598 shared space for the tunnel. Private addressing does NOT pass: this
    # tree used to allow RFC 1918 wholesale, which made a substituted address
    # and a real one indistinguishable to the one check meant to tell them apart.
    198.18.* | 198.19.*) return 0 ;;
    100.64.0.*) return 0 ;;
    # Loopback, unspecified, broadcast, and the public resolver used in examples.
    127.* | 0.0.0.0 | 255.255.255.* | 8.8.8.8 | 8.8.4.4) return 0 ;;
  esac
  return 1
}

# Domain names, recognised by a final label that is actually a top-level domain.
# Without that condition the pattern reads `api.env` and `origin.key` as hosts.
SANITIZE_HOST_TLD='(com|net|org|edu|gov|int|mil|io|dev|app|ai|cloud|info|biz|me|co|kr|jp|cn|tw|uk|us|de|fr|eu|ru|in)'
SANITIZE_HOST='\b([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+'"$SANITIZE_HOST_TLD"'\b'

# sanitize_host_allowed NAME -> 0 when a domain name may appear in this tree.
# The reserved example and test names cover every substituted host; github.com
# and its raw file host are the project's own links, public by definition.
sanitize_host_allowed() {
  local h
  h=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$h" in
    example.com | example.net | example.org | *.example.com | *.example.net | *.example.org) return 0 ;;
    example.ac.kr | *.example.ac.kr) return 0 ;;
    example.dev | *.example.dev) return 0 ;;
    *.example | *.invalid | *.test | *.localhost) return 0 ;;
    # The project's own public addresses: the service answers at them, the
    # organisation page prints them, and every published repository names them.
    # Withholding them here would hide nothing and would only make these
    # scripts read less like the ones they mirror.
    pusan.ac.kr | *.pusan.ac.kr | pusan.dev | *.pusan.dev) return 0 ;;
    pnuops.com | *.pnuops.com | pcl.kr | *.pcl.kr) return 0 ;;
    # Public infrastructure these scripts genuinely fetch from.
    github.com | *.github.com | githubusercontent.com | *.githubusercontent.com) return 0 ;;
    *.debian.org | *.ubuntu.com | *.postgresql.org | *.proxmox.com | *.docker.com) return 0 ;;
    *.letsencrypt.org | *.cloudflare.com | *.npmjs.org | *.golang.org | *.maven.org) return 0 ;;
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
    # This file is skipped because it is made of samples the rules exist to
    # reject: its selftest needs values the address rule must refuse, so
    # scanning itself would bury a real finding among its own probes.
  done < <(git ls-files -z | grep -zv '^scripts/sanitization-check\.sh$' \
    | xargs -0 grep -EoI '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' 2>/dev/null | sort -u)

  # 1b. The same question for IPv6.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    file=${line%%:*}
    addr=${line#*:}
    is_ipv6 "$addr" || continue
    addr6_allowed "$addr" && continue
    sfail "$file carries $addr, which is a global IPv6 address rather than a documentation range"
  done < <(git ls-files -z | grep -zv '^scripts/sanitization-check\.sh$' \
    | xargs -0 grep -EoI '([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}' 2>/dev/null | sort -u)

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
  # Case-insensitive on BOTH sides. The finder was case-sensitive while the
  # allowance that drops the standard value was not, so one capital letter --
  # `Admin sshd` against `admin sshd` -- turned the rule off for that line. The
  # spellings that actually occur are added too: `ssh -p N`, sshd's own
  # `Port N`, a bare `SSH_PORT=N`, and the Korean prose these runbooks are
  # written in, which the English-only pattern could never reach.
  while IFS= read -r line; do
    sfail "an administrative SSH port is written out as something other than the standard one: $line"
  done < <(git ls-files -z | grep -zv '^scripts/sanitization-check\.sh$' \
    | xargs -0 grep -EnIi '(admin(istrative)? ssh[^0-9/]{0,24}|관리(용)? ?ssh[^0-9/]{0,24}|_SSH_PORT[^0-9/]{0,8}|\bSSH_PORT=|ssh +-p +|^[[:space:]]*Port +)[`:]?[0-9]+' 2>/dev/null \
    | grep -viE '(admin(istrative)? ssh[^0-9/]{0,24}|관리(용)? ?ssh[^0-9/]{0,24}|_SSH_PORT[^0-9/]{0,8}|\bSSH_PORT=|ssh +-p +|^[^:]*:[0-9]+:[[:space:]]*Port +)[`:]?22([^0-9]|$)')

  # 4. Host names. `CLAUDE.md` lists "host names -> example names" as one of the
  # substitutions this copy promises, and nothing checked it: the real console,
  # gateway and SSH host names all passed. The sibling gate on the vault copy
  # has had this rule since it was written; this one never received it.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    file=${line%%:*}
    host=${line#*:}
    sanitize_host_allowed "$host" && continue
    sfail "$file names the host $host, which is not one of the reserved example names"
  done < <(git ls-files -z | grep -zv '^scripts/sanitization-check\.sh$' \
    | xargs -0 grep -HoIE "$SANITIZE_HOST" 2>/dev/null | sort -u)

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
  for probe in 203.0.113.20 198.18.9.9 100.64.0.9 127.0.0.1; do
    if ! addr_allowed "$probe"; then
      echo "sanitization selftest: $probe would be rejected" >&2
      return 1
    fi
  done
  # Private addressing is rejected now. These probes are synthetic on purpose:
  # writing the addresses this tree actually substituted would publish the very
  # layout the substitution removed.
  for probe in 10.99.99.99 172.31.99.99 192.168.99.99; do
    if addr_allowed "$probe"; then
      echo "sanitization selftest: $probe would be accepted" >&2
      return 1
    fi
  done
  # Punctuation that is not an address at all.
  for probe in 22:30:15 23:59:59 2c:54:91:ab:cd:ef 3a:1b:2c:3d:4e:5f 12:34:56; do
    if is_ipv6 "$probe"; then
      echo "sanitization selftest: $probe would be read as an address" >&2
      return 1
    fi
  done
  for probe in 2606:4700:4700::1111 2606::1 2001:db8::1 2606:4700:4700:1111:2222:3333:4444:5555; do
    if ! is_ipv6 "$probe"; then
      echo "sanitization selftest: $probe would not be read as an address" >&2
      return 1
    fi
  done
  for probe in 2606:4700:4700::1111 2a03:2880:f10c::35; do
    if addr6_allowed "$probe"; then
      echo "sanitization selftest: $probe would be accepted" >&2
      return 1
    fi
  done
  for probe in 2001:db8::1 fe80::1 fd00::1 ::1; do
    if ! addr6_allowed "$probe"; then
      echo "sanitization selftest: $probe would be rejected" >&2
      return 1
    fi
  done
  echo "sanitization selftest OK"
}
