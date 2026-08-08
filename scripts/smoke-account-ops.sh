#!/bin/bash
# shellcheck disable=SC2015  # ok/ko idiom is safe here
# ==========================================================================
# Account & ops readiness e2e — run on pve1 as root after deploy.
#
# Phases can be run individually; the full set covers the account-ops surface
# deploy (2FA/terms/maintenance, operator roles). Run everything:
#   bash smoke-account-ops.sh            # all deployed phases
#   PHASES="account admin" bash smoke-account-ops.sh   # subset
#
# Phases in this revision:
#   account — password change / reset (mock-mail token via DB) / withdrawal
#   admin   — admin user list/search/detail, disable→immediate 401→enable
#   group   — team delete (+ SUBMITTED request cancel), personal 409
#   protect — REAL-PVE always-on protection invariant (qm config check!) +
#             logical deletion_protection gate, stop_protection, display_name,
#             admin display fields  (provisions one dev VM)
#   sweep   — retention sweeper registration + config sanity (behavior is
#             asserted by api tests; live firing observed at the 04:30 run)
#
# Scratch users use unique +tag emails; every mutated row is cleaned up.
# ==========================================================================
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

CTID="${CTID:-101}"
PHASES="${PHASES:-account admin group protect sweep mfa terms maint roles}"
TS=$(date +%s)-$RANDOM
B=$(mktemp); TMPFILES=("$B")

seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
pgq(){ pct exec "$CTID" -- su - postgres -c "psql -d pickle_dev -qtAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
# psql -c travels through the shell `su -c` spawns, and that second parse expands
# $$ to the shell's PID: the scratch-user DO block below arrived as
# `do <pid> ... end <pid>` and died on a syntax error the old >/dev/null
# swallowed, so that cleanup had never once run. Feed the statement on stdin,
# where nothing re-parses it, and let a failing statement say so.
pgx(){
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -d pickle_dev -v ON_ERROR_STOP=1 -f -" <<<"$1" 2>&1); then
    printf 'pgx failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
}

SYSADMIN_EMAIL="admin@pickle.local"; SYSADMIN_PW="$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)"
ORGADMIN_EMAIL="orgadmin@pickle.local"; ORGADMIN_PW="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"

P=0; F=0; ok(){ echo "PASS  $1"; P=$((P+1)); }; ko(){ echo "FAIL  $1"; F=$((F+1)); }
req(){ local n="$1" e="$2"; shift 2; local c; c=$(curl -sS -o "$B" -w '%{http_code}' "$@"); [ "$c" = "$e" ] && { ok "$n ($c)"; return 0; } || { ko "$n (want $e got $c)"; head -c 300 "$B"; echo; return 1; }; }
login(){ curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"password\":\"$2\"}"; jq -r '.accessToken // empty' "$B"; }
auth(){ echo "Authorization: Bearer $1"; }
# Sudo-mode reauth: VM delete/settings, SSH keys and group-member mutations
# answer 403 REAUTH_REQUIRED without a fresh password proof (X-Reauth-Token).
# The token is per-account and multi-use for 10 minutes, and POST /auth/reverify
# is rate-limited per IP and per account, so cache it per access token instead
# of minting one per call. A password change bumps the token version and kills
# both tokens — the re-login that follows produces a new cache key here, so the
# cache never serves a token the server has already invalidated.
# The cache is FILE-backed on purpose: every call site invokes reauth from a
# command substitution (a subshell), so an in-memory array would be written in
# the subshell and thrown away — each protected call would then mint a fresh
# token and the per-IP reverify limit (every account here shares this host's
# egress IP) would start answering 429 with an empty token, i.e. a spurious
# REAUTH_REQUIRED failure. Entries expire well inside the 10-minute server TTL.
RT_DIR=$(mktemp -d)
reauth(){ # reauth ACCESS_TOKEN PASSWORD → echoes the X-Reauth-Token value
  local f exp tok
  f="$RT_DIR/$(printf '%s' "$1" | md5sum | cut -d' ' -f1)"
  if [ -s "$f" ]; then
    { read -r exp; read -r tok; } < "$f"
    [ "$SECONDS" -lt "${exp:-0}" ] && { printf '%s' "$tok"; return 0; }
  fi
  tok=$(curl -sS -X POST "$BASE/auth/reverify" -H "Authorization: Bearer $1" \
    -H 'Content-Type: application/json' -d "$(jq -nc --arg p "$2" '{password:$p}')" \
    | jq -r '.reauthToken // empty')
  [ -n "$tok" ] && printf '%s\n%s\n' "$((SECONDS+480))" "$tok" > "$f"
  printf '%s' "$tok"
}
rt(){ echo "X-Reauth-Token: $(reauth "$1" "$2")"; }

# mk_user EMAIL PW NAME → "<accessToken> <userId>" (signup→mock-mail token→verify→login)
mk_user(){
  # every smoke request shares one source IP (reverse-proxy hairpin), so the
  # per-IP signup window fills up over a run — clear dev counters first.
  pgx "delete from auth_rate_limits"
  curl -sS -o /dev/null -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\",\"name\":\"$3\",\"consents\":$CONSENTS_JSON}"
  sleep 2
  local tok; tok=$(pct exec "$CTID" -- sh -c "grep -o 'token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log | tail -1 | cut -d= -f2")
  curl -sS -o /dev/null -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' -d "{\"token\":\"$tok\"}"
  echo "$(login "$1" "$2") $(pgq "select id from users where email='$1'")"
}

declare -a SCRATCH_EMAILS=()
VM=""; VM_DELETED=1; SAT=""
cleanup(){
  local rc=$?
  if [ -n "$VM" ] && [ "$VM_DELETED" != 1 ]; then
    echo "-- cleanup: removing leftover VM $VM (protection setting off + force-delete) --"
    local at; at=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
    # the logical setting may still be on; owner-only, so clear via DB as root.
    # No qm --protection 0 here: the destroy pipeline clears the always-on PVE
    # flag itself — clearing it out-of-band would mask a broken clear step.
    pgx "delete from vm_settings where vm_id=$VM and key='deletion_protection'"
    local vname; vname=$(pgq "select name from vms where id=$VM")
    curl -sS -o /dev/null -X POST "$BASE/admin/vms/$VM/force-delete" -H "$(auth "$at")" \
      -H 'Content-Type: application/json' -d "{\"confirmName\":\"$vname\",\"overrideProtection\":true}"
  fi
  # Delete each scratch user FK-safely. Users who created a VM/request leave
  # permanent vm/vm_requests rows (retention model §14) so their user row can't
  # be deleted — that's by design, not residue; skip them (the WHERE not-exists).
  # audit_logs is append-only: detach the actor instead of deleting rows.
  for e in "${SCRATCH_EMAILS[@]:-}"; do
    [ -n "$e" ] || continue
    pgx "do \$\$
      declare uid bigint; gids bigint[];
      begin
        select id into uid from users where email='$e'
          and not exists (select 1 from vm_requests r where r.requester_id=users.id);
        if uid is null then return; end if;
        -- the personal group has to be identified before the membership rows go;
        -- the old id-in-subquery ran after them and therefore matched nothing.
        select coalesce(array_agg(group_id), '{}') into gids from group_members where user_id=uid;
        update audit_logs set actor_id=null where actor_id=uid;
        update groups set deleted_by=null where deleted_by=uid;
        delete from notifications where user_id=uid;
        delete from user_consents where user_id=uid;
        delete from mfa_recovery_codes where user_id=uid;
        delete from mfa_login_tokens where user_id=uid;
        delete from user_mfa where user_id=uid;
        delete from user_ssh_keys where user_id=uid;
        delete from user_status_changes where user_id=uid or actor_id=uid;
        delete from email_verifications where user_id=uid;
        delete from refresh_tokens where user_id=uid;
        delete from auth_reverifications where user_id=uid;
        delete from group_members where user_id=uid;
        delete from groups g where g.id = any(gids) and g.kind='PERSONAL' and g.deleted_by is null
          and not exists (select 1 from group_members gm where gm.group_id=g.id)
          and not exists (select 1 from vms v where v.group_id=g.id);
        delete from users where id=uid;
      end \$\$;"
    # A user who filed a request keeps their row by design (the not-exists guard
    # above skips them), so residue is not a failure — but it must be visible,
    # otherwise a cleanup that silently stops working looks identical.
    local left; left=$(pgq "select count(*) from users where email='$e'")
    [ "${left:-0}" = 0 ] || echo "-- cleanup: scratch user $e retained (owns a request row) --"
  done
  # never leave the live env in maintenance mode / with smoke banner rows
  pgx "update settings set value='false'::jsonb where key='maintenance_mode' and value='true'::jsonb"
  pgx "update settings set value=to_jsonb(''::text) where key='banner_message' and value=to_jsonb('스모크 공지 배너'::text)"
  pgx "delete from user_consents where terms_version_id in (select id from terms_versions where body like '%(스모크 개정판)%')"
  pgx "delete from terms_versions where body like '%(스모크 개정판)%'"
  rm -f "${TMPFILES[@]}" 2>/dev/null; rm -rf "$RT_DIR"
  echo; echo "==== smoke-account-ops: PASS $P / FAIL $F ===="
  [ $F -eq 0 ] && [ $rc -eq 0 ] || exit 1
}
trap cleanup EXIT

has_phase(){ case " $PHASES " in *" $1 "*) return 0;; *) return 1;; esac; }

# OS image + org lookups shared by request-creating phases; the request payload
# mirrors smoke-provisioning (flavor-preset specs, every nullable field explicit) —
# hand-rolled minimal payloads hit server-side spec validation.
TPL=$(pgq "select id from os_images where status='ACTIVE' limit 1")
# The state the bootstrap runbook leaves behind — catalog rows registered but
# none enabled yet — makes this empty, and an empty id is interpolated into the
# payload as "imageId":, which is not JSON. The request then fails as a bare
# 400 that says nothing about the catalog, so state the reason here instead.
[ -n "$TPL" ] || { ko "no ACTIVE OS image to request with (enable one in the catalog)"; exit 1; }
ORG=$(pgq "select id from orgs limit 1")
[ -n "$ORG" ] || { ko "no org to request against"; exit 1; }
req_payload(){ # $1=groupId $2=purpose
  printf '{"groupId":%s,"orgId":%s,"imageId":%s,"flavorId":%s,"purpose":"%s","courseOrProject":null,"specReason":null,"extraNote":null,"reqVcpu":%s,"reqMemoryMb":%s,"reqDiskGb":%s,"reqStartDate":null,"reqEndDate":null,"desiredSubdomain":null,"rootDomain":null}' "$1" "$ORG" "$TPL" "$FLAVOR" "$2" "$TPL_VCPU" "$TPL_MEM" "$TPL_DISK"
}
approve_payload(){
  printf '{"grantedVcpu":%s,"grantedMemoryMb":%s,"grantedDiskGb":%s,"grantedImageId":%s,"grantedStartDate":null,"grantedEndDate":null,"nodeId":null,"comment":"스모크 승인"}' "$TPL_VCPU" "$TPL_MEM" "$TPL_DISK" "$TPL"
}

SAT=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
[ -n "$SAT" ] && ok "sysadmin login" || { ko "sysadmin login"; exit 1; }
OAT=$(login "$ORGADMIN_EMAIL" "$ORGADMIN_PW")
[ -n "$OAT" ] && ok "orgadmin login" || ko "orgadmin login"

# The spec axis moved off the OS catalog into vm_flavors: the default_* columns no
# longer exist (a psql select would just error into an empty value) and
# POST /vm-requests now requires flavorId. Read the presets from the API.
req "vm-flavors 200" 200 "$BASE/vm-flavors" -H "$(auth "$SAT")"
FSEL='(map(select(.name=="basic"))[0] // .[0])'
FLAVOR=$(jq -r "$FSEL.id // empty" "$B"); TPL_VCPU=$(jq -r "$FSEL.vcpu // empty" "$B")
TPL_MEM=$(jq -r "$FSEL.memoryMb // empty" "$B"); TPL_DISK=$(jq -r "$FSEL.diskGb // empty" "$B")
[ -n "$FLAVOR" ] && ok "flavor id=$FLAVOR (${TPL_VCPU}c/${TPL_MEM}MB/${TPL_DISK}GB)" || { ko "no ACTIVE vm-flavor"; exit 1; }

# ───────────────────────── phase: account ─────────────────────────
if has_phase account; then
  echo "── account: password change / reset / withdrawal"
  U1="smoke-acct-acc-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U1")
  read -r U1T U1ID <<<"$(mk_user "$U1" 'first-password-10' '계정스모크')"
  [ -n "$U1T" ] && ok "scratch user created (id=$U1ID)" || ko "scratch user created"

  # change: keeps this session (fresh pair), kills others
  U1T2=$(login "$U1" 'first-password-10')
  req "password change 200" 200 -X PUT "$BASE/me/password" -H "$(auth "$U1T")" \
    -H 'Content-Type: application/json' \
    -d '{"currentPassword":"first-password-10","newPassword":"second-password-10"}'
  NEWT=$(jq -r '.accessToken // empty' "$B")
  req "  fresh token works" 200 "$BASE/me" -H "$(auth "$NEWT")"
  req "  other session died (401)" 401 "$BASE/me" -H "$(auth "$U1T2")"
  req "  wrong current pw 403" 403 -X PUT "$BASE/me/password" -H "$(auth "$NEWT")" \
    -H 'Content-Type: application/json' \
    -d '{"currentPassword":"nope-nope-nope","newPassword":"third-password-10"}'

  # reset: uniform 202 + token from DB (mock mail) + confirm + old sessions die
  req "reset request 202 (existing)" 202 -X POST "$BASE/auth/password-reset" \
    -H 'Content-Type: application/json' -d "{\"email\":\"$U1\"}"
  req "reset request 202 (unknown — uniform)" 202 -X POST "$BASE/auth/password-reset" \
    -H 'Content-Type: application/json' -d "{\"email\":\"no-such-$TS@pusan.ac.kr\"}"
  sleep 2
  RTOK=$(pct exec "$CTID" -- sh -c "grep -o 'reset-password?token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log | tail -1 | cut -d= -f2")
  [ -n "$RTOK" ] && ok "reset token delivered (mock mail)" || ko "reset token delivered"
  req "reset confirm 200" 200 -X POST "$BASE/auth/password-reset/confirm" \
    -H 'Content-Type: application/json' -d "{\"token\":\"$RTOK\",\"newPassword\":\"reset-password-10\"}"
  req "  token single-use (410)" 410 -X POST "$BASE/auth/password-reset/confirm" \
    -H 'Content-Type: application/json' -d "{\"token\":\"$RTOK\",\"newPassword\":\"reset-password-10\"}"
  req "  pre-reset session died (401)" 401 "$BASE/me" -H "$(auth "$NEWT")"
  U1T3=$(login "$U1" 'reset-password-10')
  [ -n "$U1T3" ] && ok "login with reset password" || ko "login with reset password"

  # withdrawal: wrong pw 403 → ok 200 → login 401 → re-signup uniform 202
  req "withdraw wrong pw 403" 403 -X POST "$BASE/me/withdraw" -H "$(auth "$U1T3")" \
    -H 'Content-Type: application/json' -d '{"password":"wrong-password-10"}'
  req "withdraw 200" 200 -X POST "$BASE/me/withdraw" -H "$(auth "$U1T3")" \
    -H 'Content-Type: application/json' -d '{"password":"reset-password-10"}'
  req "  withdrawn login 401" 401 -X POST "$BASE/auth/login" \
    -H 'Content-Type: application/json' -d "{\"email\":\"$U1\",\"password\":\"reset-password-10\"}"
  req "  same-email re-signup 202 (no enumeration)" 202 -X POST "$BASE/auth/signup" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$U1\",\"password\":\"whatever-pass-10\",\"name\":\"재가입\",\"consents\":$CONSENTS_JSON}"
  RS=$(pgq "select count(*) from users where email='$U1'")
  [ "$RS" = 1 ] && ok "  re-signup created no account" || ko "  re-signup created no account ($RS)"
  W=$(pgq "select count(*) from users u where u.email='$U1' and u.status='WITHDRAWN' and u.withdrawn_at is not null")
  [ "$W" = 1 ] && ok "  WITHDRAWN + withdrawn_at stamped" || ko "  WITHDRAWN + withdrawn_at stamped"
  PG=$(pgq "select count(*) from groups g where g.deleted_at is not null and g.kind='PERSONAL'
            and g.deleted_by=(select id from users where email='$U1')")
  [ "$PG" = 1 ] && ok "  personal group soft-deleted" || ko "  personal group soft-deleted"
fi

# ───────────────────────── phase: admin ─────────────────────────
if has_phase admin; then
  echo "── admin: user list / detail / disable / enable"
  U2="smoke-acct-adm-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U2")
  read -r U2T U2ID <<<"$(mk_user "$U2" 'target-password-10' '비활성화대상')"
  req "admin user list 200" 200 "$BASE/admin/users?q=smoke-acct-adm-$TS" -H "$(auth "$SAT")"
  N=$(jq -r '.totalElements' "$B"); [ "$N" = 1 ] && ok "  search hits exactly 1" || ko "  search hits exactly 1 (got $N)"
  req "admin user detail 200" 200 "$BASE/admin/users/$U2ID" -H "$(auth "$SAT")"
  req "self-disable 409" 409 -X POST "$BASE/admin/users/$(pgq "select id from users where email='$SYSADMIN_EMAIL'")/disable" \
    -H "$(auth "$SAT")" -H 'Content-Type: application/json' -d '{"reason":"스모크 자기 비활성화 시도"}'
  req "disable 200" 200 -X POST "$BASE/admin/users/$U2ID/disable" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"reason":"스모크 테스트 비활성화"}'
  req "  disabled token immediately dead (401)" 401 "$BASE/me" -H "$(auth "$U2T")"
  req "  disabled login 401" 401 -X POST "$BASE/auth/login" \
    -H 'Content-Type: application/json' -d "{\"email\":\"$U2\",\"password\":\"target-password-10\"}"
  req "  orgadmin disable forbidden 403" 403 -X POST "$BASE/admin/users/$U2ID/disable" \
    -H "$(auth "$OAT")" -H 'Content-Type: application/json' -d '{"reason":"권한 없음"}'
  req "enable 200" 200 -X POST "$BASE/admin/users/$U2ID/enable" -H "$(auth "$SAT")"
  U2T2=$(login "$U2" 'target-password-10')
  [ -n "$U2T2" ] && ok "  re-enabled login works" || ko "  re-enabled login works"
  H=$(pgq "select count(*) from user_status_changes where user_id=$U2ID")
  [ "$H" = 2 ] && ok "  status history rows = 2" || ko "  status history rows = 2 (got $H)"
fi

# ───────────────────────── phase: group ─────────────────────────
if has_phase group; then
  echo "── group: delete + request-cancel + personal 409"
  U3="smoke-acct-grp-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U3")
  read -r U3T U3ID <<<"$(mk_user "$U3" 'group-password-10' '그룹스모크')"
  req "create team 201" 201 -X POST "$BASE/groups" -H "$(auth "$U3T")" \
    -H 'Content-Type: application/json' \
    -d "{\"kind\":\"TEAM\",\"name\":\"smoke-acct-team-$TS\",\"slug\":\"smoke-acct-team-$TS\"}"
  GID=$(jq -r '.id' "$B")
  # a SUBMITTED request must be canceled by the delete
  req "submit vm request 201" 201 -X POST "$BASE/vm-requests" -H "$(auth "$U3T")" \
    -H 'Content-Type: application/json' -d "$(req_payload "$GID" '그룹삭제 취소 검증용')"
  RID=$(jq -r '.id' "$B")
  req "delete team 204" 204 -X DELETE "$BASE/groups/$GID" -H "$(auth "$U3T")"
  RST=$(pgq "select status from vm_requests where id=$RID")
  [ "$RST" = "CANCELED" ] && ok "  submitted request canceled" || ko "  submitted request canceled (got $RST)"
  req "  deleted group is gone (404)" 404 "$BASE/groups/$GID" -H "$(auth "$U3T")"
  req "  slug reusable (201)" 201 -X POST "$BASE/groups" -H "$(auth "$U3T")" \
    -H 'Content-Type: application/json' \
    -d "{\"kind\":\"TEAM\",\"name\":\"smoke-acct-team-$TS\",\"slug\":\"smoke-acct-team-$TS\"}"
  GID2=$(jq -r '.id' "$B")
  req "  cleanup second team 204" 204 -X DELETE "$BASE/groups/$GID2" -H "$(auth "$U3T")"
  PGID=$(pgq "select g.id from groups g join group_members gm on gm.group_id=g.id
              where gm.user_id=$U3ID and g.kind='PERSONAL' and g.deleted_at is null")
  req "  personal group delete 409" 409 -X DELETE "$BASE/groups/$PGID" -H "$(auth "$U3T")"
  C=$(jq -r '.code' "$B"); [ "$C" = "GROUP_PERSONAL_UNDELETABLE" ] && ok "  code GROUP_PERSONAL_UNDELETABLE" || ko "  code ($C)"
fi

# ───────────────────────── phase: protect (REAL PVE) ─────────────────────────
if has_phase protect; then
  echo "── protect: deletion/stop protection on a real VM (provisions one)"
  U4="smoke-acct-vm-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U4")
  U4PW='vmowner-password-1'   # also the sudo-mode proof for the VM/member calls below
  read -r U4T _ <<<"$(mk_user "$U4" "$U4PW" 'VM보호스모크')"
  # membership tests need an invitable group — PERSONAL membership is immutable
  req "create protect team 201" 201 -X POST "$BASE/groups" -H "$(auth "$U4T")" \
    -H 'Content-Type: application/json' \
    -d "{\"kind\":\"TEAM\",\"name\":\"smoke-acct-prot-$TS\",\"slug\":\"smoke-acct-prot-$TS\"}"
  PGID4=$(jq -r '.id' "$B")
  req "vm request 201" 201 -X POST "$BASE/vm-requests" -H "$(auth "$U4T")" \
    -H 'Content-Type: application/json' -d "$(req_payload "$PGID4" '보호 스모크')"
  RID4=$(jq -r '.id // empty' "$B")
  [ -n "$RID4" ] || { ko "protect phase aborted — request not created"; RID4=0; }
  req "approve 200" 200 -X POST "$BASE/admin/vm-requests/$RID4/approve" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d "$(approve_payload)"
  VM=$(pgq "select id from vms where request_id=$RID4")
  if [ -z "$VM" ]; then ko "protect phase aborted — no VM row"; VM=""; VM_DELETED=1; fi
  [ -n "$VM" ] && VM_DELETED=0
  if [ -n "$VM" ]; then
  echo "  waiting for RUNNING (vm=$VM)…"
  for _ in $(seq 1 60); do ST=$(pgq "select status from vms where id=$VM"); [ "$ST" = RUNNING ] && break; sleep 10; done
  [ "$ST" = RUNNING ] && ok "vm RUNNING" || { ko "vm RUNNING (got $ST)"; }
  VMID=$(pgq "select proxmox_vmid from vms where id=$VM")
  VNAME=$(pgq "select name from vms where id=$VM")

  # always-on invariant: a freshly provisioned VM is hypervisor-protected
  QP=$(qm config "$VMID" 2>/dev/null | grep -c '^protection: 1')
  [ "$QP" = 1 ] && ok "fresh vm carries protection: 1 (always-on)" || ko "fresh vm protection (got $(qm config "$VMID" | grep '^protection' || echo none))"

  req "deletion_protection on (200)" 200 -X PATCH "$BASE/vms/$VM/settings" -H "$(auth "$U4T")" \
    -H "$(rt "$U4T" "$U4PW")" -H 'Content-Type: application/json' -d '{"settings":{"deletion_protection":true}}'
  req "  self-delete blocked 409" 409 -X DELETE "$BASE/vms/$VM" -H "$(auth "$U4T")" -H "$(rt "$U4T" "$U4PW")"
  C=$(jq -r '.code' "$B"); [ "$C" = "VM_DELETION_PROTECTED" ] && ok "  code VM_DELETION_PROTECTED" || ko "  code ($C)"
  req "  force-delete without override 409" 409 -X POST "$BASE/admin/vms/$VM/force-delete" \
    -H "$(auth "$SAT")" -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\"}"
  req "stop_protection on (200)" 200 -X PATCH "$BASE/vms/$VM/settings" -H "$(auth "$U4T")" \
    -H "$(rt "$U4T" "$U4PW")" -H 'Content-Type: application/json' -d '{"settings":{"stop_protection":true}}'
  # add a MEMBER who must be blocked from stopping
  U5="smoke-acct-mem-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U5")
  read -r U5T _ <<<"$(mk_user "$U5" 'member-password-10' '중지보호구성원')"
  req "  add MEMBER 201" 201 -X POST "$BASE/groups/$PGID4/members" -H "$(auth "$U4T")" \
    -H "$(rt "$U4T" "$U4PW")" -H 'Content-Type: application/json' -d "{\"email\":\"$U5\",\"role\":\"MEMBER\"}"
  req "  member shutdown blocked 409" 409 -X POST "$BASE/vms/$VM/shutdown" -H "$(auth "$U5T")"
  C=$(jq -r '.code' "$B"); [ "$C" = "VM_STOP_PROTECTED" ] && ok "  code VM_STOP_PROTECTED" || ko "  code ($C)"
  req "  owner shutdown allowed 202" 202 -X POST "$BASE/vms/$VM/shutdown" -H "$(auth "$U4T")"

  req "display_name set (200)" 200 -X PATCH "$BASE/vms/$VM/settings" -H "$(auth "$U4T")" \
    -H "$(rt "$U4T" "$U4PW")" -H 'Content-Type: application/json' -d '{"settings":{"display_name":"보호 스모크 VM"}}'
  req "  vm list carries displayName" 200 "$BASE/vms" -H "$(auth "$U4T")"
  DN=$(jq -r ".content[] | select(.id==$VM) | .displayName" "$B")
  [ "$DN" = "보호 스모크 VM" ] && ok "  displayName round-trip" || ko "  displayName round-trip ($DN)"
  req "admin vms carries orgName" 200 "$BASE/admin/vms?q=$VNAME" -H "$(auth "$SAT")"
  ON=$(jq -r '.content[0].orgName // empty' "$B"); [ -n "$ON" ] && ok "  orgName present ($ON)" || ko "  orgName present"
  req "admin tasks multi-status 200" 200 "$BASE/admin/tasks?status=FAILED&status=NEEDS_ADMIN" -H "$(auth "$SAT")"

  req "deletion_protection off (200)" 200 -X PATCH "$BASE/vms/$VM/settings" -H "$(auth "$U4T")" \
    -H "$(rt "$U4T" "$U4PW")" -H 'Content-Type: application/json' -d '{"settings":{"deletion_protection":false}}'
  # decoupling proof: the toggle never touches the hypervisor — the always-on
  # PVE flag stays armed until the destroy pipeline clears it pre-delete
  QP=$(qm config "$VMID" 2>/dev/null | grep -c '^protection: 1')
  [ "$QP" = 1 ] && ok "  qm protection still 1 after toggle off (decoupled)" || ko "  qm protection still 1 after toggle off"
  req "self-delete now accepted 202" 202 -X DELETE "$BASE/vms/$VM" -H "$(auth "$U4T")" -H "$(rt "$U4T" "$U4PW")"
  # immediate destroy for the smoke: pull the grace forward and let the sweeper
  # fire — a completed destroy proves the pipeline's clear-then-delete works
  pgx "update vms set delete_scheduled_for=now() where id=$VM"
  echo "  waiting for destroy sweep…"
  for _ in $(seq 1 42); do ST=$(pgq "select status from vms where id=$VM"); [ "$ST" = DELETED ] && break; sleep 10; done
  [ "$ST" = DELETED ] && { ok "vm destroyed clean (pipeline cleared always-on protection)"; VM_DELETED=1; } || ko "vm destroyed clean (got $ST)"
  fi
fi

# ───────────────────────── phase: sweep ─────────────────────────
if has_phase sweep; then
  echo "── sweep: retention sweeper registration"
  R1=$(pgq "select count(*) from jobrunr_recurring_jobs where id like '%notification-retention%'")
  R2=$(pgq "select count(*) from jobrunr_recurring_jobs where id like '%auth-token-retention%'")
  [ "$R1" = 1 ] && ok "notification-retention sweeper registered" || ko "notification-retention sweeper registered"
  [ "$R2" = 1 ] && ok "auth-token-retention sweeper registered" || ko "auth-token-retention sweeper registered"
  RD=$(pgq "select value::text from settings where key='notification_retention_days'")
  [ -n "$RD" ] && ok "notification_retention_days readable ($RD)" || ko "notification_retention_days readable"
fi

# ───────────────────────── phase: mfa ─────────────────────────
# TOTP is computed with stdlib python3 (oathtool is not installed on pve1 by design).
totp(){ python3 - "$1" <<'PY'
import base64,hmac,hashlib,struct,sys,time
key=base64.b32decode(sys.argv[1].upper()+'='*((8-len(sys.argv[1])%8)%8))
ctr=int(time.time())//30
mac=hmac.new(key,struct.pack('>Q',ctr),hashlib.sha1).digest()
off=mac[-1]&0xf
print('{:06d}'.format((struct.unpack('>I',mac[off:off+4])[0]&0x7fffffff)%1000000))
PY
}
if has_phase mfa; then
  echo "── mfa: enroll → step-up login → recovery → admin reset"
  U6="smoke-acct-mfa-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U6")
  read -r U6T _ <<<"$(mk_user "$U6" 'mfa-password-1234' '2단계스모크')"
  req "mfa begin 200" 200 -X POST "$BASE/me/mfa/totp" -H "$(auth "$U6T")" \
    -H 'Content-Type: application/json' -d '{"password":"mfa-password-1234"}'
  SECRET=$(jq -r '.secret' "$B")
  [ -n "$SECRET" ] && ok "  secret issued" || ko "  secret issued"
  req "mfa activate 200" 200 -X POST "$BASE/me/mfa/totp/activate" -H "$(auth "$U6T")" \
    -H 'Content-Type: application/json' -d "{\"code\":\"$(totp "$SECRET")\"}"
  RC=$(jq -r '.recoveryCodes[0]' "$B"); NRC=$(jq -r '.recoveryCodes | length' "$B")
  [ "$NRC" = 10 ] && ok "  10 recovery codes issued once" || ko "  10 recovery codes (got $NRC)"
  # step-up: password gives a challenge, not tokens
  curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$U6\",\"password\":\"mfa-password-1234\"}"
  MT=$(jq -r '.mfaToken // empty' "$B")
  [ -n "$MT" ] && [ "$(jq -r '.accessToken // empty' "$B")" = "" ] \
    && ok "login returns MFA challenge (no tokens)" || ko "login returns MFA challenge"
  req "  wrong code 401 (token kept)" 401 -X POST "$BASE/auth/mfa" \
    -H 'Content-Type: application/json' -d "{\"mfaToken\":\"$MT\",\"code\":\"000000\"}"
  req "  correct code 200" 200 -X POST "$BASE/auth/mfa" \
    -H 'Content-Type: application/json' -d "{\"mfaToken\":\"$MT\",\"code\":\"$(totp "$SECRET")\"}"
  U6T2=$(jq -r '.accessToken' "$B")
  req "  step-up token works on /me" 200 "$BASE/me" -H "$(auth "$U6T2")"
  [ "$(jq -r '.mfaEnabled' "$B")" = "true" ] && ok "  /me mfaEnabled=true" || ko "  /me mfaEnabled"
  req "  challenge token single-use 410" 410 -X POST "$BASE/auth/mfa" \
    -H 'Content-Type: application/json' -d "{\"mfaToken\":\"$MT\",\"code\":\"$(totp "$SECRET")\"}"
  # recovery-code path
  curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$U6\",\"password\":\"mfa-password-1234\"}"
  MT2=$(jq -r '.mfaToken // empty' "$B")
  req "recovery code login 200" 200 -X POST "$BASE/auth/mfa" \
    -H 'Content-Type: application/json' -d "{\"mfaToken\":\"$MT2\",\"recoveryCode\":\"$RC\"}"
  curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$U6\",\"password\":\"mfa-password-1234\"}"
  MT3=$(jq -r '.mfaToken // empty' "$B")
  req "  recovery code single-use 401" 401 -X POST "$BASE/auth/mfa" \
    -H 'Content-Type: application/json' -d "{\"mfaToken\":\"$MT3\",\"recoveryCode\":\"$RC\"}"
  # admin rescue
  U6ID=$(pgq "select id from users where email='$U6'")
  req "admin mfa-reset 200" 200 -X POST "$BASE/admin/users/$U6ID/mfa-reset" -H "$(auth "$SAT")"
  T=$(login "$U6" 'mfa-password-1234')
  [ -n "$T" ] && ok "  post-reset login is direct (no challenge)" || ko "  post-reset login direct"
fi

# ───────────────────────── phase: terms ─────────────────────────
if has_phase terms; then
  echo "── terms: public read / signup consent / re-consent gate"
  req "meta terms 200" 200 "$BASE/meta/terms"
  ND=$(jq -r 'length' "$B"); [ "$ND" = 2 ] && ok "  2 current documents" || ko "  2 documents (got $ND)"
  req "terms body 200" 200 "$BASE/meta/terms/TERMS_OF_SERVICE"
  BL=$(jq -r '.body | length' "$B"); [ "$BL" -gt 200 ] && ok "  body non-trivial ($BL chars)" || ko "  body length ($BL)"
  req "signup without consents 422" 422 -X POST "$BASE/auth/signup" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"smoke-acct-nc-$TS@pusan.ac.kr\",\"password\":\"whatever-pass-10\",\"name\":\"미동의\",\"consents\":[]}"
  U7="smoke-acct-tos-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U7")
  read -r U7T _ <<<"$(mk_user "$U7" 'terms-password-10' '약관스모크')"
  req "my consents 200" 200 "$BASE/me/consents" -H "$(auth "$U7T")"
  NC=$(jq -r 'length' "$B"); [ "$NC" = 2 ] && ok "  signup recorded 2 consents" || ko "  consents (got $NC)"
  # version bump → pendingConsents → re-consent (cleaned up afterwards)
  pgx "insert into terms_versions (doc_type, version, title, body, effective_at)
       select doc_type, version+1, title, body || E'\n\n(스모크 개정판)', now()
         from terms_versions tv where doc_type='TERMS_OF_SERVICE'
          and version=(select max(version) from terms_versions where doc_type='TERMS_OF_SERVICE')"
  req "profile shows pending consent" 200 "$BASE/me" -H "$(auth "$U7T")"
  NP=$(jq -r '.pendingConsents | length' "$B"); [ "$NP" = 1 ] && ok "  pendingConsents=1 after revision" || ko "  pendingConsents ($NP)"
  NV=$(jq -r '.pendingConsents[0].version' "$B")
  req "re-consent 200" 200 -X POST "$BASE/me/consents" -H "$(auth "$U7T")" \
    -H 'Content-Type: application/json' \
    -d "{\"consents\":[{\"docType\":\"TERMS_OF_SERVICE\",\"version\":$NV}]}"
  req "  pending cleared" 200 "$BASE/me" -H "$(auth "$U7T")"
  NP=$(jq -r '.pendingConsents | length' "$B"); [ "$NP" = 0 ] && ok "  pendingConsents empty" || ko "  pending cleared ($NP)"
  pgx "delete from user_consents where terms_version_id in (select id from terms_versions where body like '%(스모크 개정판)%')"
  pgx "delete from terms_versions where body like '%(스모크 개정판)%'"
  ok "  revision rows cleaned up"
fi

# ───────────────────────── phase: maint ─────────────────────────
if has_phase maint; then
  echo "── maint: maintenance mode / banner / contact"
  U8="smoke-acct-mnt-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U8")
  read -r U8T _ <<<"$(mk_user "$U8" 'maint-password-10' '점검스모크')"
  req "meta status 200 (off)" 200 "$BASE/meta/status"
  [ "$(jq -r '.maintenance' "$B")" = "false" ] && ok "  maintenance=false baseline" || ko "  baseline not false"
  req "maintenance on (200)" 200 -X PUT "$BASE/admin/settings/maintenance_mode" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"value":true}'
  sleep 16   # settings cache TTL
  req "  user request 503" 503 "$BASE/vms" -H "$(auth "$U8T")"
  C=$(jq -r '.code' "$B"); [ "$C" = "MAINTENANCE_MODE" ] && ok "  code MAINTENANCE_MODE" || ko "  code ($C)"
  req "  admin passes (200)" 200 "$BASE/admin/users?size=1" -H "$(auth "$SAT")"
  req "  login exempt (200-family)" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$U8\",\"password\":\"maint-password-10\"}"
  req "  meta/status exempt + true" 200 "$BASE/meta/status"
  [ "$(jq -r '.maintenance' "$B")" = "true" ] && ok "  maintenance=true visible" || ko "  maintenance flag"
  HH=$(pct exec "$CTID" -- curl -sS -o /dev/null -w '%{http_code}' http://localhost:8080/actuator/health)
  [ "$HH" = 200 ] && ok "  actuator health exempt (200)" || ko "  actuator health ($HH)"
  req "maintenance off (200)" 200 -X PUT "$BASE/admin/settings/maintenance_mode" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"value":false}'
  sleep 16
  req "  user recovered (200)" 200 "$BASE/vms" -H "$(auth "$U8T")"
  req "banner set (200)" 200 -X PUT "$BASE/admin/settings/banner_message" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"value":"스모크 공지 배너"}'
  sleep 16
  req "  banner visible" 200 "$BASE/meta/status"
  [ "$(jq -r '.bannerMessage' "$B")" = "스모크 공지 배너" ] && ok "  bannerMessage round-trip" || ko "  banner"
  req "banner clear (200)" 200 -X PUT "$BASE/admin/settings/banner_message" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"value":""}'
fi

# ───────────────────────── phase: roles ─────────────────────────
if has_phase roles; then
  echo "── roles: ORG_MANAGER / SYS_MANAGER matrix samples"
  U9="smoke-acct-rol-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U9")
  read -r _ U9ID <<<"$(mk_user "$U9" 'roles-password-10' '운영자스모크')"
  req "ORG_MANAGER without orgId 422" 422 -X PATCH "$BASE/admin/users/$U9ID" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"role":"ORG_MANAGER"}'
  req "ORG_MANAGER with orgId 200" 200 -X PATCH "$BASE/admin/users/$U9ID" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d "{\"role\":\"ORG_MANAGER\",\"orgId\":$ORG}"
  OMT=$(login "$U9" 'roles-password-10')
  [ -n "$OMT" ] && ok "  org-manager login" || ko "  org-manager login"
  req "  admin vms read 200" 200 "$BASE/admin/vms?size=1" -H "$(auth "$OMT")"
  req "  admin users read 200" 200 "$BASE/admin/users?size=1" -H "$(auth "$OMT")"
  req "  settings write 403" 403 -X PUT "$BASE/admin/settings/banner_message" -H "$(auth "$OMT")" \
    -H 'Content-Type: application/json' -d '{"value":"nope"}'
  req "  announcement create 403" 403 -X POST "$BASE/admin/announcements" -H "$(auth "$OMT")" \
    -H 'Content-Type: application/json' -d '{"scope":"ORG","title":"x","body":"y"}'
  req "  user disable 403" 403 -X POST "$BASE/admin/users/$U9ID/disable" -H "$(auth "$OMT")" \
    -H 'Content-Type: application/json' -d '{"reason":"nope"}'
  req "SYS_MANAGER switch 200" 200 -X PATCH "$BASE/admin/users/$U9ID" -H "$(auth "$SAT")" \
    -H 'Content-Type: application/json' -d '{"role":"SYS_MANAGER","orgId":null}'
  SMT=$(login "$U9" 'roles-password-10')
  [ -n "$SMT" ] && ok "  sys-manager login" || ko "  sys-manager login"
  req "  tasks read 200" 200 "$BASE/admin/tasks?size=1" -H "$(auth "$SMT")"
  req "  nodes read 200" 200 "$BASE/admin/nodes" -H "$(auth "$SMT")"
  req "  settings read 200" 200 "$BASE/admin/settings" -H "$(auth "$SMT")"
  req "  system summary 200" 200 "$BASE/admin/system-summary" -H "$(auth "$SMT")"
  req "  settings write 403" 403 -X PUT "$BASE/admin/settings/banner_message" -H "$(auth "$SMT")" \
    -H 'Content-Type: application/json' -d '{"value":"nope"}'
  req "  force-delete 403 (role gate)" 403 -X POST "$BASE/admin/vms/999999/force-delete" -H "$(auth "$SMT")" \
    -H 'Content-Type: application/json' -d '{"confirmName":"nope"}'
  req "  role change 403" 403 -X PATCH "$BASE/admin/users/$U9ID" -H "$(auth "$SMT")" \
    -H 'Content-Type: application/json' -d '{"role":"USER"}'
fi
