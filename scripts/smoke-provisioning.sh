#!/usr/bin/env bash
# Provisioning smoke test against the deployed dev environment (real provisioning).
#
# Run on the pve-node HOST as root — only the host holds a vmbr2 address
# (198.19.0.1) and can reach guest VMs on :22; LXC 101 cannot. Do NOT run
# before the provisioning cutover: the API must already expose the real-provisioning
# endpoints, with the Proxmox token and the VM template both in place.
#
# Journey: signup -> verify (token from mock-mail log via pct) -> login ->
# team group (slug dev-smoke-<ts>; the VM name is generated from the group
# slug, giving the e2e VM the mandatory dev- prefix) ->
# vm request -> ORG_ADMIN approve -> poll RUNNING (real pipeline, <= 15 min,
# progress logged) -> SSH :22 reachable -> password reveal + re-read (masked;
# re-read must be 200 since v0.7.0) -> shutdown/start round-trip (force-stop fallback
# if ACPI is ignored right after boot) -> self-delete (kind SELF, DELETING)
# -> admin cancel-scheduled-delete (deletion cleared, STOPPED) -> SYS_ADMIN
# force-delete -> DB checks (ip RELEASED, vm row DELETED, tasks DONE)
# -> leftover dev-* guard. Cleanup runs even when earlier steps fail.
#
# Contract v0.3.1 note: the X-Pickle-Csrf header is only required on
# /auth/refresh and /auth/logout — neither is called here (login access
# tokens only), so no CSRF handling is needed.
#
# Usage: smoke-provisioning.sh [BASE_URL]   (default https://pickle.pusan.ac.kr)
# Requires: curl, jq, nc, qm, and pct access to CTID 101.
set -uo pipefail # no -e: cleanup and the summary must run even after failures

BASE="${1:-https://pickle.pusan.ac.kr}/api/v1"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

CTID="${CTID:-101}"
TS=$(date +%s)
USER_EMAIL="smoke-${TS}@pusan.ac.kr"
USER_PW="smoke-pass-${TS}!"
# seed passwords live in the LXC's api.env (rotated 2026-07-12 — the repo
# defaults are dead); env vars still override for non-standard setups
seed_env() {
  pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"
}
ORGADMIN_EMAIL="${PICKLE_SEED_ORGADMIN_EMAIL:-orgadmin@pickle.local}"
ORGADMIN_PW="${PICKLE_SEED_ORGADMIN_PASSWORD:-$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)}"
SYSADMIN_EMAIL="${PICKLE_SEED_SYSADMIN_EMAIL:-admin@pickle.local}"
SYSADMIN_PW="${PICKLE_SEED_SYSADMIN_PASSWORD:-$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)}"
if [ -z "$ORGADMIN_PW" ] || [ -z "$SYSADMIN_PW" ]; then
  echo "FATAL: seed admin passwords not found (env or CTID $CTID api.env)" >&2
  exit 1
fi
PASS=0
FAIL=0
USER_AT=""
ADMIN_AT=""
SYSADMIN_AT=""
GROUP_ID=""
FLAVOR_ID=""
REQ_ID=""
VM_ID=""
VM_NAME=""
VM_IP=""

for cmd in curl jq nc qm pct; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "missing required command: $cmd (this script must run on the pve-node host as root)"
    exit 2
  }
done

BODY=$(mktemp)
RT_DIR=$(mktemp -d) # sudo-mode token cache (see reauth below)
trap 'rm -f "$BODY"; rm -rf "$RT_DIR"' EXIT

ok() { echo "PASS  $1"; PASS=$((PASS + 1)); }
ko() { echo "FAIL  $1"; FAIL=$((FAIL + 1)); }

# Sudo-mode reauth: the password reveal and the VM delete answer 403
# REAUTH_REQUIRED without a fresh password proof (X-Reauth-Token). The token is
# per-account and multi-use for 10 minutes, and POST /auth/reverify is
# rate-limited per IP and per account, so cache it per access token rather than
# minting one per call (the cache key changes whenever a re-login is needed,
# which is exactly when a password change has invalidated the old token).
# The cache is FILE-backed on purpose: every call site invokes reauth from a
# command substitution (a subshell), so an in-memory array would be written in
# the subshell and thrown away — each protected call would then mint a fresh
# token and the per-IP reverify limit (every account here shares this host's
# egress IP) would start answering 429 with an empty token, i.e. a spurious
# REAUTH_REQUIRED failure. The cached entry expires well inside the 10-minute
# server TTL, so the long RUNNING/power polls between protected calls simply
# cause one re-mint instead of serving an already-expired token.
# reauth <access-token> <password>: echoes the X-Reauth-Token value
reauth() {
  local f exp tok
  f="$RT_DIR/$(printf '%s' "$1" | md5sum | cut -d' ' -f1)"
  if [ -s "$f" ]; then
    { read -r exp; read -r tok; } <"$f"
    if [ "$SECONDS" -lt "${exp:-0}" ]; then
      printf '%s' "$tok"
      return 0
    fi
  fi
  tok=$(curl -sS -X POST "$BASE/auth/reverify" -H "Authorization: Bearer $1" \
    -H 'Content-Type: application/json' -d "$(jq -nc --arg p "$2" '{password:$p}')" |
    jq -r '.reauthToken // empty')
  [ -n "$tok" ] && printf '%s\n%s\n' "$((SECONDS + 480))" "$tok" >"$f"
  printf '%s' "$tok"
}

# step <name> <expected-status> <curl args...>  (dumps body head on mismatch)
step() {
  local name="$1" expect="$2" out
  shift 2
  out=$(curl -sS -o "$BODY" -w '%{http_code}' "$@")
  if [ "$out" = "$expect" ]; then
    ok "$name ($out)"
    return 0
  fi
  ko "$name (expected $expect, got $out)"
  head -c 400 "$BODY"
  echo
  return 1
}

# step_masked: like step, but NEVER prints the body — for calls whose
# response may carry a secret (initial password). No exceptions.
step_masked() {
  local name="$1" expect="$2" out
  shift 2
  out=$(curl -sS -o "$BODY" -w '%{http_code}' "$@")
  if [ "$out" = "$expect" ]; then
    ok "$name ($out)"
    return 0
  fi
  ko "$name (expected $expect, got $out; body withheld — may contain a secret)"
  return 1
}

# vm_status: GET /vms/{id} into $BODY as the user; echoes .status
vm_status() {
  curl -sS "$BASE/vms/$VM_ID" -H "Authorization: Bearer $USER_AT" -o "$BODY" || {
    echo ""
    return
  }
  jq -r '.status // empty' "$BODY"
}

# wait_vm <name> <target-status> <timeout-sec>
# Polls VM detail, logging provisioning progress (currentStep/stepLabel) as
# it changes. Bails out early if the VM parks in ERROR / NEEDS_ADMIN.
wait_vm() {
  local name="$1" target="$2" timeout="$3"
  local deadline=$((SECONDS + timeout)) st="" prog="" last_prog=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    st=$(vm_status)
    prog=$(jq -r 'if .provisioning != null then
        "step \(.provisioning.currentStep)/\(.provisioning.totalSteps) \(.provisioning.stepLabel) [\(.provisioning.status)] attempts=\(.provisioning.attempts)"
      else empty end' "$BODY" 2>/dev/null || true)
    if [ -n "$prog" ] && [ "$prog" != "$last_prog" ]; then
      echo "      $name: $prog"
      last_prog="$prog"
    fi
    if [ "$st" = "$target" ]; then
      ok "$name (-> $target)"
      return 0
    fi
    case "$st" in
      ERROR | NEEDS_ADMIN)
        ko "$name (parked in $st: $(jq -r '.provisioning.lastError // .statusDetail // "?"' "$BODY" 2>/dev/null))"
        return 1
        ;;
    esac
    sleep 5
  done
  ko "$name (timeout after ${timeout}s, last status: ${st:-none})"
  return 1
}

# db <sql>: run psql -tAc inside the app LXC as postgres
db() {
  pct exec "$CTID" -- su - postgres -c "psql -d pickle_dev -tAc \"$1\""
}

db_vm_status() { db "select status from vms where id = $VM_ID" | tr -d '[:space:]'; }

# wait_db_deleted <timeout-sec>: poll the vms row until DELETED. DB is the
# ground truth here — the API detail view is not specified for destroyed VMs.
wait_db_deleted() {
  local timeout="$1" st=""
  local deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    st=$(db_vm_status)
    if [ "$st" = "DELETED" ]; then
      ok "force delete -> vm row DELETED"
      return 0
    fi
    sleep 5
  done
  ko "force delete (timeout after ${timeout}s, vm row status: ${st:-none})"
  return 1
}

# ── phase 1: user account, reference data, dev-smoke group ──
phase_account() {
  step "signup" 202 -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$USER_EMAIL\",\"password\":\"$USER_PW\",\"name\":\"프로비저닝 스모크\",\"consents\":$CONSENTS_JSON}" || return 1

  sleep 2
  # tokens are withheld from the journal (bearer secrets); the dev mock mailer
  # spools full bodies to a service-user-only file instead
  local token
  token=$(pct exec "$CTID" -- sh -c \
    "grep -o 'token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log 2>/dev/null | tail -1 | cut -d= -f2")
  if [ -z "$token" ]; then
    ko "verification token extraction from CTID $CTID mock-mail spool"
    return 1
  fi
  ok "verification token extracted"

  step "verify-email" 200 -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' \
    -d "{\"token\":\"$token\"}" || return 1

  step "user login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$USER_EMAIL\",\"password\":\"$USER_PW\"}" || return 1
  USER_AT=$(jq -r .accessToken "$BODY")

  step "os-images" 200 "$BASE/os-images" -H "Authorization: Bearer $USER_AT" || return 1
  TEMPLATE_ID=$(jq -r '.[0].id // empty' "$BODY")
  if [ -z "$TEMPLATE_ID" ]; then
    ko "no ACTIVE OS image to request with"
    return 1
  fi

  # The OS axis (os-images) and the spec axis (flavors) are separate catalogs:
  # os-images no longer carry default specs, and POST /vm-requests requires the
  # chosen flavorId. Take the 'basic' preset, or the first ACTIVE row.
  step "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $USER_AT" || return 1
  local sel='(map(select(.name=="basic"))[0] // .[0])'
  FLAVOR_ID=$(jq -r "$sel.id // empty" "$BODY")
  TPL_VCPU=$(jq -r "$sel.vcpu // empty" "$BODY")
  TPL_MEM=$(jq -r "$sel.memoryMb // empty" "$BODY")
  TPL_DISK=$(jq -r "$sel.diskGb // empty" "$BODY")
  if [ -z "$FLAVOR_ID" ]; then
    ko "no ACTIVE vm-flavor to request with"
    return 1
  fi
  ok "flavor id=$FLAVOR_ID (${TPL_VCPU}c/${TPL_MEM}MB/${TPL_DISK}GB)"

  # the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
  step "orgadmin login (org lookup)" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$ORGADMIN_EMAIL\",\"password\":\"$ORGADMIN_PW\"}" || return 1
  OA_LOOKUP_AT=$(jq -r .accessToken "$BODY")
  step "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $OA_LOOKUP_AT" || return 1
  ORG_ID=$(jq -r '.[0].id' "$BODY")

  # VM name/hostname = <group slug> + random suffix (ApprovalService), so a
  # dev-smoke-* slug stamps the e2e VM with the dev- prefix.
  step "create group (slug dev-smoke-$TS)" 201 -X POST "$BASE/groups" \
    -H "Authorization: Bearer $USER_AT" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"TEAM\",\"name\":\"스모크팀 $TS\",\"slug\":\"dev-smoke-$TS\",\"description\":\"provisioning smoke\"}" || return 1
  GROUP_ID=$(jq -r .id "$BODY")
}

# ── phase 2: vm request + ORG_ADMIN approval ──
phase_request_approve() {
  step "create vm-request" 201 -X POST "$BASE/vm-requests" -H "Authorization: Bearer $USER_AT" \
    -H 'Content-Type: application/json' -d "{
      \"groupId\":$GROUP_ID,\"orgId\":$ORG_ID,\"imageId\":$TEMPLATE_ID,\"flavorId\":$FLAVOR_ID,
      \"purpose\":\"프로비저닝 스모크 테스트 (실제 프로비저닝 검증)\",\"courseOrProject\":null,\"specReason\":null,
      \"extraNote\":null,\"reqVcpu\":$TPL_VCPU,\"reqMemoryMb\":$TPL_MEM,\"reqDiskGb\":$TPL_DISK,
      \"reqStartDate\":null,\"reqEndDate\":null}" || return 1
  REQ_ID=$(jq -r .id "$BODY")

  step "orgadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$ORGADMIN_EMAIL\",\"password\":\"$ORGADMIN_PW\"}" || return 1
  ADMIN_AT=$(jq -r .accessToken "$BODY")

  step "approve" 200 -X POST "$BASE/admin/vm-requests/$REQ_ID/approve" \
    -H "Authorization: Bearer $ADMIN_AT" -H 'Content-Type: application/json' -d "{
      \"grantedVcpu\":$TPL_VCPU,\"grantedMemoryMb\":$TPL_MEM,\"grantedDiskGb\":$TPL_DISK,
      \"grantedImageId\":$TEMPLATE_ID,\"grantedStartDate\":null,\"grantedEndDate\":null,
      \"nodeId\":null,\"comment\":\"스모크 승인\"}" || return 1

  step "vm visible in list" 200 "$BASE/vms?groupId=$GROUP_ID" -H "Authorization: Bearer $USER_AT" || return 1
  VM_ID=$(jq -r '.content[0].id // empty' "$BODY")
  VM_NAME=$(jq -r '.content[0].name // empty' "$BODY")
  if [ -z "$VM_ID" ] || [ -z "$VM_NAME" ]; then
    ko "vm id/name extraction from list"
    return 1
  fi
  ok "vm created: id=$VM_ID name=$VM_NAME"
}

# ── phase 4/5: allocated IP + SSH reachability from the host (vmbr2) ──
phase_ssh() {
  vm_status >/dev/null
  VM_IP=$(jq -r '.ipAddress // empty' "$BODY")
  if [ -z "$VM_IP" ]; then
    ko "ipAddress missing on VM detail"
    return 1
  fi
  ok "ipAddress allocated ($VM_IP)"

  local deadline=$((SECONDS + 90))
  until nc -z -w5 "$VM_IP" 22 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      ko "SSH :22 not reachable at $VM_IP within 90s"
      return 1
    fi
    sleep 5
  done
  ok "SSH :22 reachable at $VM_IP"
}

# ── phase 6: password reveal (re-viewable since v0.7.0, masked) ──
phase_password() {
  step_masked "initial-password reveal" 200 "$BASE/vms/$VM_ID/password" \
    -H "Authorization: Bearer $USER_AT" \
    -H "X-Reauth-Token: $(reauth "$USER_AT" "$USER_PW")" || return 1
  local pwlen
  pwlen=$(jq -r '.password // "" | length' "$BODY")
  : >"$BODY" # drop the plaintext immediately; it is never echoed anywhere
  if [ "${pwlen:-0}" -gt 0 ]; then
    ok "initial password non-empty (value masked)"
  else
    ko "initial password empty"
  fi
  # v0.7.0: the reveal no longer consumes — a second read must succeed too
  step_masked "initial-password re-read -> 200" 200 "$BASE/vms/$VM_ID/password" \
    -H "Authorization: Bearer $USER_AT" \
    -H "X-Reauth-Token: $(reauth "$USER_AT" "$USER_PW")"
  : >"$BODY"
}

# ── phase 7: power round-trip (shutdown -> STOPPED -> start -> RUNNING) ──
phase_power() {
  # Spike finding (2026-07-08): right after boot the guest may ignore ACPI;
  # give the OS a moment, and fall back to force-stop if shutdown stalls.
  sleep 20
  step "shutdown request" 202 -X POST "$BASE/vms/$VM_ID/shutdown" \
    -H "Authorization: Bearer $USER_AT" || return 1
  if ! wait_vm "shutdown -> STOPPED" STOPPED 180; then
    echo "WARN  ACPI shutdown did not reach STOPPED in 180s — falling back to force-stop (recorded)"
    step "force-stop fallback" 202 -X POST "$BASE/vms/$VM_ID/force-stop" \
      -H "Authorization: Bearer $USER_AT" || return 1
    wait_vm "force-stop -> STOPPED" STOPPED 120 || return 1
  fi
  step "start request" 202 -X POST "$BASE/vms/$VM_ID/start" \
    -H "Authorization: Bearer $USER_AT" || return 1
  wait_vm "start -> RUNNING" RUNNING 300 || return 1
}

# ── phase 8/9: self-delete (no user cancel exists) + admin cancel ──
phase_delete_cancel() {
  step "self-delete" 202 -X DELETE "$BASE/vms/$VM_ID" \
    -H "Authorization: Bearer $USER_AT" \
    -H "X-Reauth-Token: $(reauth "$USER_AT" "$USER_PW")" || return 1
  local kind
  kind=$(jq -r '.kind // empty' "$BODY")
  if [ "$kind" = "SELF" ]; then
    ok "self-delete response kind=SELF"
  else
    ko "self-delete response kind (expected SELF, got ${kind:-none})"
  fi

  local st
  st=$(vm_status)
  if [ "$st" = "DELETING" ]; then
    ok "vm status DELETING after self-delete"
  else
    ko "vm status after self-delete (expected DELETING, got ${st:-none})"
  fi
  local dkind
  dkind=$(jq -r '.deletion.kind // empty' "$BODY")
  if [ "$dkind" = "SELF" ]; then
    ok "detail deletion.kind=SELF"
  else
    ko "detail deletion.kind (expected SELF, got ${dkind:-none})"
  fi

  # Requesters cannot cancel a deletion (policy: grace = admin safety net);
  # the admin endpoint is the only cancel path.
  step "admin cancel-scheduled-delete" 200 -X POST "$BASE/admin/vms/$VM_ID/cancel-scheduled-delete" \
    -H "Authorization: Bearer $ADMIN_AT" || return 1
  # SELF deletion shuts the VM down on acceptance, so after cancel it must
  # settle at STOPPED (the delete-flow shutdown may still be in flight).
  wait_vm "cancel -> STOPPED" STOPPED 180
  local del
  del=$(jq -r '.deletion' "$BODY")
  if [ "$del" = "null" ]; then
    ok "deletion cleared after cancel"
  else
    ko "deletion still present after cancel"
  fi
}

# ── cleanup: SYS_ADMIN force delete — runs even after failures ──
cleanup() {
  echo "-- cleanup (always runs) --"
  if [ -z "$VM_ID" ]; then
    echo "      no VM was created; nothing to clean up"
    return 0
  fi
  step "sysadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$SYSADMIN_EMAIL\",\"password\":\"$SYSADMIN_PW\"}" || return 1
  SYSADMIN_AT=$(jq -r .accessToken "$BODY")

  if [ "$(db_vm_status)" = "DELETED" ]; then
    echo "      vm row already DELETED; force delete not needed"
    return 0
  fi
  step "force-delete (confirmName=$VM_NAME)" 202 -X POST "$BASE/admin/vms/$VM_ID/force-delete" \
    -H "Authorization: Bearer $SYSADMIN_AT" -H 'Content-Type: application/json' \
    -d "{\"confirmName\":\"$VM_NAME\"}" || return 1
  wait_db_deleted 300
}

# ── post-delete verification: Proxmox + DB ground truth ──
post_verify() {
  [ -z "$VM_ID" ] && return 0

  # `qm list` failing (pvedaemon down, storage stuck) used to fall through to the
  # else branch and report the VM as absent — an unrun query is not evidence of
  # absence, so establish that the listing succeeded before reading it.
  local qmout qmrc
  qmout=$(qm list 2>&1); qmrc=$?
  if [ "$qmrc" -ne 0 ]; then
    ko "qm list failed (rc=$qmrc) — cannot confirm $VM_NAME is gone: $(printf '%s' "$qmout" | head -c 200)"
  elif printf '%s\n' "$qmout" | awk 'NR>1 {print $2}' | grep -Fxq "$VM_NAME"; then
    ko "qm list still shows $VM_NAME"
  else
    ok "qm list: $VM_NAME absent"
  fi

  local total released
  total=$(db "select count(*) from ip_allocations where vm_id = $VM_ID" | tr -d '[:space:]')
  released=$(db "select count(*) from ip_allocations where vm_id = $VM_ID and status = 'RELEASED'" | tr -d '[:space:]')
  if [ "${total:-0}" -ge 1 ] && [ "$total" = "$released" ]; then
    ok "ip_allocations: $released/$total RELEASED"
  else
    ko "ip_allocations: expected all rows RELEASED (total=${total:-?}, released=${released:-?})"
  fi

  local vmst
  vmst=$(db_vm_status)
  if [ "$vmst" = "DELETED" ]; then
    ok "vms row status DELETED"
  else
    ko "vms row status (expected DELETED, got ${vmst:-none})"
  fi

  local undone
  undone=$(db "select count(*) from provisioning_tasks where vm_id = $VM_ID and status <> 'DONE'" | tr -d '[:space:]')
  if [ "${undone:-1}" = "0" ]; then
    ok "provisioning_tasks: all DONE"
  else
    ko "provisioning_tasks: ${undone:-?} row(s) not DONE"
  fi
}

# ── residue guard: list (never auto-delete) any leftover dev-* VMs ──
residue_guard() {
  local leftovers qmout qmrc
  # Same trap as post_verify: a failed listing must not read as "nothing left".
  qmout=$(qm list 2>&1); qmrc=$?
  if [ "$qmrc" -ne 0 ]; then
    ko "residue guard: qm list failed (rc=$qmrc) — leftover dev-* VMs unverified"
    return 0
  fi
  leftovers=$(printf '%s\n' "$qmout" | awk 'NR>1 {print $2}' | grep '^dev-' || true)
  if [ -n "$leftovers" ]; then
    echo "WARN  leftover dev-* VMs remain in qm list (NOT auto-deleted — clean up manually):"
    while IFS= read -r vm; do echo "      $vm"; done <<<"$leftovers"
  else
    echo "      residue guard: no dev-* VMs left in qm list"
  fi
}

echo "== Provisioning smoke against $BASE (CTID $CTID) =="

if phase_account && phase_request_approve; then
  # Real pipeline: clone, cloud-init, IP allocation, boot — allow 15 minutes.
  if wait_vm "provision -> RUNNING" RUNNING 900; then
    phase_ssh
    phase_password
    phase_power
    phase_delete_cancel
  fi
fi

cleanup
post_verify
residue_guard

TOTAL=$((PASS + FAIL))
echo "PROVISIONING SMOKE: $PASS/$TOTAL"
[ "$FAIL" -eq 0 ]
