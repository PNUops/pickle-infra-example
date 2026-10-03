#!/usr/bin/env bash
# Read-only platform DNS checks. The caller supplies rec() and psqv().

dns_health_names() {
  tr '[:upper:]' '[:lower:]' | tr '[:space:]' '\n' | sed '/^$/d; s/\.$//' | sort -u | paste -sd ' ' -
}

dns_health_single_label() {
  local name=$1 root=$2 label
  label=${name%".$root"}
  [ "$name" = "$label.$root" ] && [[ "$label" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
}

# Keep the response code: an empty answer on timeout or SERVFAIL is not absence.
dns_health_query() {
  local server=$1 name=$2 type=$3 response
  local -a args=(+time=3 +tries=1 +noall +comments +answer)
  DNS_HEALTH_STATUS='' DNS_HEALTH_ADDRS='' DNS_HEALTH_NAMES='' DNS_HEALTH_AA=''
  DNS_HEALTH_A_OWNERS='' DNS_HEALTH_CNAME='' DNS_HEALTH_ANSWER_COUNT=''
  if [ -n "$server" ]; then args+=("@$server" +norecurse); fi
  if ! response=$(dig "${args[@]}" "$name" "$type" 2>/dev/null); then return 1; fi
  DNS_HEALTH_STATUS=$(printf '%s\n' "$response" | sed -n 's/.*status: \([^,]*\),.*/\1/p')
  DNS_HEALTH_ANSWER_COUNT=$(printf '%s\n' "$response" | sed -n 's/.*ANSWER: \([0-9]*\),.*/\1/p')
  [ -n "$DNS_HEALTH_STATUS" ] || return 1
  if printf '%s\n' "$response" | grep -Eq '^;; flags:.*[ ;]aa([ ;]|$)'; then DNS_HEALTH_AA=yes; fi
  DNS_HEALTH_ADDRS=$(printf '%s\n' "$response" | awk '$4 == "A" {print $5}' | sort -u | paste -sd ' ' -)
  DNS_HEALTH_A_OWNERS=$(printf '%s\n' "$response" | awk '$4 == "A" {print $1}' | dns_health_names)
  DNS_HEALTH_CNAME=$(printf '%s\n' "$response" | awk '$4 == "CNAME" {print $5}')
  DNS_HEALTH_NAMES=$(printf '%s\n' "$response" | awk '$4 == "NS" {print $5}' | dns_health_names)
}

dns_health_a() {
  local server=$1 name=$2 expected=$3 label=$4
  if ! dns_health_query "$server" "$name" A; then
    rec "$label" FAIL "$server: $name A query failed"
  elif [ "$DNS_HEALTH_AA" != yes ] || [ "$DNS_HEALTH_STATUS" != NOERROR ]; then
    rec "$label" FAIL "$server: $name A is not authoritative NOERROR (${DNS_HEALTH_STATUS:-no status})"
  elif [ "$DNS_HEALTH_ADDRS" = "$expected" ] && [ "$DNS_HEALTH_A_OWNERS" = "${name%.}" ] && [ -z "$DNS_HEALTH_CNAME" ]; then
    rec "$label" OK "$server: $name → $DNS_HEALTH_ADDRS"
  else
    rec "$label" FAIL "$server: $name A=${DNS_HEALTH_ADDRS:-none} (expected $expected)"
  fi
}

check_platform_dns() {
  local root=$PLATFORM_ROOT_DOMAIN mode=$PLATFORM_DNS_MODE
  local expected_ns nameserver probe records failed serving fqdn manual
  if [ -z "$root" ]; then
    rec dns:platform SKIP "PLATFORM_ROOT_DOMAIN unset — platform-root DNS unarmed"
    return
  fi
  if [[ ! "$root" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]]; then
    rec dns:platform FAIL "invalid PLATFORM_ROOT_DOMAIN"
    return
  fi
  case "$mode" in
    explicit|wildcard) ;;
    *) rec dns:platform FAIL "PLATFORM_DNS_MODE must be explicit or wildcard"; return ;;
  esac
  expected_ns=$(printf '%s\n' "$PLATFORM_DNS_EXPECTED_NS" | dns_health_names)
  if [ -z "$expected_ns" ]; then
    rec dns:ns FAIL "PLATFORM_DNS_EXPECTED_NS unset — exact delegation unverified"
    return
  fi
  for nameserver in $expected_ns; do
    if [[ ! "$nameserver" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]]; then
      rec dns:ns FAIL "invalid PLATFORM_DNS_EXPECTED_NS"
      return
    fi
  done
  if ! command -v dig >/dev/null 2>&1; then
    rec dns:platform FAIL "dig not installed — platform-root DNS unverified"
    return
  fi
  probe=${PLATFORM_DNS_PROBE_FQDN:-hc-$$-$(date +%s)-${RANDOM}.${root}}
  if ! dns_health_single_label "$probe" "$root"; then
    rec dns:unregistered FAIL "PLATFORM_DNS_PROBE_FQDN must be a single unregistered label under the root"
    return
  fi
  manual=$(printf '%s\n' "$PLATFORM_DNS_MANUAL_FQDNS" | dns_health_names)
  for fqdn in $manual; do
    if ! dns_health_single_label "$fqdn" "$root"; then
      rec dns:manual FAIL "PLATFORM_DNS_MANUAL_FQDNS must contain single-label names under the root"
      return
    fi
  done
  if ! dns_health_query "" "$root" NS; then
    rec dns:ns FAIL "$root NS query failed"
  elif [ "$DNS_HEALTH_STATUS" = NOERROR ] && [ "$DNS_HEALTH_NAMES" = "$expected_ns" ]; then
    rec dns:ns OK "$root → $DNS_HEALTH_NAMES"
  else
    rec dns:ns FAIL "$root NS=${DNS_HEALTH_NAMES:-none} (${DNS_HEALTH_STATUS}; expected $expected_ns)"
  fi

  # A successful empty serving set differs from a database query failure.
  if ! records=$(psqv "select 'FAILED|'||count(*) from domains where kind in ('PLATFORM','AUTO') and status <> 'REMOVED' and released_at is null and root_domain='$root' and dns_status='FAILED' union all select 'SERVING|'||d.fqdn from domains d where d.kind in ('PLATFORM','AUTO') and d.status='ACTIVE' and d.released_at is null and d.root_domain='$root' and exists (select 1 from routes r where r.domain_id=d.id and r.status='APPLIED') order by 1"); then
    rec dns:domains FAIL "cannot read platform DNS state from the database"
    return
  fi
  failed=$(printf '%s\n' "$records" | sed -n 's/^FAILED|//p' | tr -d '[:space:]')
  if [[ ! "$failed" =~ ^[0-9]+$ ]]; then
    rec dns:domains FAIL "invalid platform DNS state from the database"
    return
  fi
  if [ "$failed" -gt 0 ]; then rec dns:apply FAIL "$failed platform DNS applications FAILED"
  else rec dns:apply OK "no failed platform DNS applications"; fi
  serving=$(printf '%s\n' "$records" | sed -n 's/^SERVING|//p' | sort -u)
  if [ -z "$serving" ]; then rec dns:serving SKIP "no ACTIVE, unreleased PLATFORM/AUTO domain with an APPLIED route"; fi

  for nameserver in $expected_ns; do
    # Check absence before serving names so a wildcard cannot hide missing A sets.
    if [ "$mode" = explicit ]; then
      if ! dns_health_query "$nameserver" "$probe" A; then
        rec "dns:unregistered:$nameserver" FAIL "$probe A query failed"
      elif [ "$DNS_HEALTH_AA" = yes ] && [ "$DNS_HEALTH_STATUS" = NXDOMAIN ] && [ "$DNS_HEALTH_ANSWER_COUNT" = 0 ]; then
        rec "dns:unregistered:$nameserver" OK "$probe NXDOMAIN"
      else
        rec "dns:unregistered:$nameserver" FAIL "$probe is not authoritative NXDOMAIN (${DNS_HEALTH_STATUS}; A=${DNS_HEALTH_ADDRS:-none})"
      fi
    else
      dns_health_a "$nameserver" "$probe" "$MAIN_DOMAIN_PUBLIC_IP" "dns:wildcard:$nameserver"
    fi
    dns_health_a "$nameserver" "$root" "$MAIN_DOMAIN_PUBLIC_IP" "dns:apex:$nameserver"
    for fqdn in $manual; do
      dns_health_a "$nameserver" "$fqdn" "$MAIN_DOMAIN_PUBLIC_IP" "dns:manual:$fqdn:$nameserver"
    done
    while IFS= read -r fqdn; do
      [ -n "$fqdn" ] || continue
      if ! dns_health_single_label "$fqdn" "$root"; then
        rec dns:serving FAIL "invalid serving name from the database"
        continue
      fi
      dns_health_a "$nameserver" "$fqdn" "$MAIN_DOMAIN_PUBLIC_IP" "dns:serving:$fqdn:$nameserver"
    done <<< "$serving"
  done
}
