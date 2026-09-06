#!/usr/bin/env bash
# Re-reads the platform wildcard certificate's expiry off the installed
# lineage and writes it into the certificates row that describes it.
#
# Why this exists: the wildcard for the platform root is a Let's Encrypt
# lineage that certbot renews on its own inside the reverse-proxy container,
# and that container has no route to the database. The row's not_after is
# display and monitoring only (the admin certificate list and the health
# snapshot warn off it; publishing checks only that the row is ACTIVE), so a
# stale date does not break anything, it just makes the warning fire on a
# certificate that has already been replaced. This script closes that gap from
# the host, which reaches both containers, and runs daily from
# pickle-wildcard-cert-row.timer under cron-wrap.sh.
#
# Update only. apply-platform-inventory.sh is the one place that creates the
# row, because creating it is an inventory decision; if no row exists this
# script exits non-zero and says to run that instead. A REVOKED row is left
# alone for the reason that script gives: revoked means the key is considered
# compromised, and a daily job must not quietly bring it back.
#
# Idempotent: re-running writes the same date again. Safe under cron-wrap.sh:
# every failure is a non-zero exit with the reason on stderr, and nothing is
# written unless every guard passed.
#
# Usage: bash scripts/refresh-wildcard-cert-row.sh
#
# Environment (same names and defaults as apply-platform-inventory.sh):
#   PICKLE_APP_CTID        101      container running PostgreSQL + the api
#   PICKLE_PROXY_CTID      100      container holding the certbot lineage
#   PICKLE_DB              pickle_dev
#   PICKLE_ROOT_DOMAIN     pusan.dev
#   PICKLE_WILDCARD_CERT   /etc/letsencrypt/live/<root>/fullchain.pem
set -euo pipefail

export PATH="/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

CTID="${PICKLE_APP_CTID:-101}"
PROXY_CTID="${PICKLE_PROXY_CTID:-100}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
require_ct "$PROXY_CTID" reverse-proxy
DB="${PICKLE_DB:-pickle_dev}"

ROOT_DOMAIN="${PICKLE_ROOT_DOMAIN:-pusan.dev}"
CERT_SCOPE="*.$ROOT_DOMAIN"
# openssl x509 reads the first certificate in fullchain.pem, which is the leaf,
# so the SAN and end-date checks below see the wildcard and not the chain.
WILDCARD_CERT="${PICKLE_WILDCARD_CERT:-/etc/letsencrypt/live/${ROOT_DOMAIN}/fullchain.pem}"

# Statements are fed on STDIN, never as `psql -c "…"`: a -c argument travels
# through the second shell `su -c` spawns, which re-parses it.
pgq() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'query failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }
die() { echo "refresh-wildcard-cert-row: $*" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 || die "openssl is not on PATH"

# ── the material: present, covering the scope, not expired ───────────────────
pct exec "$PROXY_CTID" -- test -f "$WILDCARD_CERT" \
  || die "no wildcard certificate at $WILDCARD_CERT in container $PROXY_CTID"
cert_text=$(pct exec "$PROXY_CTID" -- openssl x509 -noout -text -in "$WILDCARD_CERT") \
  || die "openssl could not read $WILDCARD_CERT"
grep -qF "DNS:$CERT_SCOPE" <<<"$cert_text" \
  || die "the certificate at $WILDCARD_CERT does not cover $CERT_SCOPE; refusing to write
                its date into the row for that scope"
cert_end=$(pct exec "$PROXY_CTID" -- \
  openssl x509 -noout -enddate -in "$WILDCARD_CERT" | cut -d= -f2-)
CERT_NOT_AFTER=$(date -u -d "$cert_end" +'%Y-%m-%dT%H:%M:%S+00:00') \
  || die "could not parse the certificate end date '$cert_end'"
cert_epoch=$(date -u -d "$cert_end" +%s)
CERT_DAYS_LEFT=$(( (cert_epoch - $(date +%s)) / 86400 ))
[ "$CERT_DAYS_LEFT" -ge 0 ] \
  || die "the certificate at $WILDCARD_CERT expired ${CERT_DAYS_LEFT#-} days ago; an expired
                lineage is a renewal failure, not a date to record (check certbot.timer in
                container $PROXY_CTID)"
echo "certificate $CERT_SCOPE valid until $CERT_NOT_AFTER (${CERT_DAYS_LEFT}d left)"

# ── the row: update in place, never create ───────────────────────────────────
scope_sql=$(sql_escape "$CERT_SCOPE")
previous=$(pgq "
  select coalesce(to_char(not_after at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS+00:00'), '<null>')
         || ' ' || status
    from certificates
   where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null and scope = '$scope_sql'
   order by id;")
if [ -z "$previous" ]; then
  die "no certificates row for $CERT_SCOPE; run apply-platform-inventory.sh, which creates it"
fi
echo "row(s) before: $(tr '\n' ';' <<<"$previous")"

updated=$(pgq "
  with upd as (
    update certificates
       set not_after = '$CERT_NOT_AFTER', status = 'ACTIVE', last_error = null,
           updated_at = now()
     where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
       and scope = '$scope_sql'
       and status <> 'REVOKED'
    returning id
  )
  select count(*) from upd;")
if [ "${updated:-0}" -eq 0 ]; then
  # A row exists (the check above passed) but none was writable, so every row
  # for this scope is REVOKED. That is the operator's decision to keep.
  die "the only row(s) for $CERT_SCOPE are REVOKED and were left alone; a revoked wildcard
                is not brought back by a date refresh (see apply-platform-inventory.sh)"
fi

now_rows=$(pgq "
  select to_char(not_after at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS+00:00') || ' ' || status
    from certificates
   where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null and scope = '$scope_sql'
   order by id;")
echo "row(s) after:  $(tr '\n' ';' <<<"$now_rows")"
echo "OK updated $updated row(s) for $CERT_SCOPE to not_after $CERT_NOT_AFTER"
