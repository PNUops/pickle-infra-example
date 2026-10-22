#!/usr/bin/env bash
# Run the fixed public observations sequentially; the service owns the deadline.
set -uo pipefail

if [ "$#" -ne 0 ]; then
  echo 'public-observation-batch: arguments are not supported' >&2
  exit 2
fi

probe=/usr/local/libexec/pickle/public-service-health-probe.py
failed=0
for instance in console api development tls-production tls-development tls-gateway staging tls-staging staging-expiry-warning; do
  if /usr/bin/python3 -I -B "$probe" \
      --config "/etc/pickle-llm-health/public-$instance.json" \
      --state-dir "/var/lib/pickle-public-$instance"; then
    :
  else
    failed=$((failed + 1))
  fi
done

printf 'public-observation-batch: attempted=9 failed=%s\n' "$failed"
if [ "$failed" -ne 0 ]; then
  exit 1
fi
exit 0
