#!/usr/bin/env bash
# Container identity guard.
#
# Every script here addresses a container by number, and takes that number from
# an environment variable so a rebuild can point it elsewhere. Several use the
# bare name CTID, so a value exported for one script silently redirects the next
# one run in the same shell: export CTID=102 to redeploy the gateway, then run
# the api deploy in that same shell, and the api lands in the gateway container.
# Nothing downstream notices — pct will happily write into whichever container
# the number names, and the deploy's own health check probes an address that
# answers either way.
#
# require_ct asserts that the container answering to a number is the one the
# caller means, by the only property that is stable across rebuilds and cannot
# be confused with a neighbour: its hostname. A mismatch stops the run before
# the first write instead of after it.
#
# Usage, once per target, before any pct call:
#   . "$(dirname "$0")/lib/ct.sh"
#   require_ct "$CTID" pickle-app
require_ct() {
  local ctid="$1" want="$2" cfg got
  # Read and parse in two steps. Piping pct into sed hands the pipeline sed's
  # exit status, which is 0 whether the container exists or not, so a missing
  # container would reach the comparison with an empty hostname and be reported
  # as the wrong thing entirely.
  if ! cfg=$(pct config "$ctid" 2>&1); then
    echo "cannot read container $ctid: ${cfg%%$'\n'*}" >&2
    return 1
  fi
  got=$(printf '%s\n' "$cfg" | sed -n 's/^hostname: //p')
  if [ -z "$got" ]; then
    echo "container $ctid declares no hostname; refusing to write into it" >&2
    return 1
  fi
  if [ "$got" != "$want" ]; then
    echo "container $ctid is '$got', but this script writes to '$want'" >&2
    echo "the number came from the environment; check the CTID variables set in this shell" >&2
    return 1
  fi
}
