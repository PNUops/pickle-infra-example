#!/bin/bash
# shellcheck disable=SC2015  # ok/ko idiom is safe here
# ==========================================================================
# Web-terminal e2e — run on pve-node as root after deploy.
#
# Exercises the full path: console-origin WS (through Cloudflare + the LXC 100
# TLS tier's /terminal/ws branch) → sshgw-terminal-bridge (LXC 102) → VM SSH,
# with pickle-api as ticket oracle/audit sink.
#
# Needs: websocat on pve-node (static binary), jq, one provisioned
# dev VM (created + destroyed here). ~6 minutes incl. one 75s revalidation
# window. The 15-min idle timeout is NOT tested here (bridge go test covers
# it); CF ~100s WS idle survival is covered by the >100s revalidation case.
#
#   bash smoke-web-terminal.sh
# ==========================================================================
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
WS_URL="${WS_URL:-wss://pickle.pusan.ac.kr/terminal/ws}"
ORIGIN="${ORIGIN:-https://pickle.pusan.ac.kr}"
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

CTID="${CTID:-101}"
TS=$(date +%s)-$RANDOM
B=$(mktemp); WSOUT=$(mktemp); TMPFILES=("$B" "$WSOUT")

seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
pgq(){ pct exec "$CTID" -- su - postgres -c "psql -d pickle_dev -qtAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
# psql -c travels through the shell `su -c` spawns, and that second parse expands
# $$ to the shell's PID: every DO block below arrived as `do <pid> ... end <pid>`
# and died on a syntax error the old >/dev/null swallowed, so the scratch-user
# cleanup had never once run. Feed the statement on stdin, where nothing
# re-parses it, and let a failing statement say so instead of vanishing.
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
# Sudo-mode reauth: group-member mutations (and the VM password/settings/ssh-key
# endpoints) answer 403 REAUTH_REQUIRED without a fresh password proof. The
# token is per-account and multi-use for 10 minutes, and POST /auth/reverify is
# rate-limited per IP and per account, so cache it per access token; a password
# change invalidates it, but that also forces a re-login and therefore a new
# cache key here.
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

mk_user(){
  pgx "delete from auth_rate_limits"
  curl -sS -o /dev/null -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\",\"name\":\"$3\",\"consents\":$CONSENTS_JSON}"
  sleep 2
  local tok; tok=$(pct exec "$CTID" -- sh -c "grep -o 'token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log | tail -1 | cut -d= -f2")
  curl -sS -o /dev/null -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' -d "{\"token\":\"$tok\"}"
  echo "$(login "$1" "$2") $(pgq "select id from users where email='$1'")"
}

# Access tokens live 15 minutes; this run is longer than that, because half of
# it is spent waiting out revalidation windows and a provisioning cycle. A token
# minted at the top is therefore expired by the closing phases, and the checks
# there failed on authentication rather than on what they assert. Anything used
# after the long waits takes a fresh one.
refresh_tokens(){
  U1T=$(login "$U1" "$U1PW")
  SAT=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
  [ -n "$U1T" ] && [ -n "$SAT" ] || ko "could not refresh tokens for the closing phase"
}

# mint VMID TOKEN → sets MINT_CODE + TICKET/SESSION_ID/WSPATH/SUBPROTO on 201
mint(){
  MINT_CODE=$(curl -sS -o "$B" -w '%{http_code}' -X POST "$BASE/vms/$1/terminal-sessions" -H "$(auth "$2")")
  TICKET=$(jq -r '.ticket // empty' "$B"); SESSION_ID=$(jq -r '.sessionId // empty' "$B")
  WSPATH=$(jq -r '.wsPath // empty' "$B"); SUBPROTO=$(jq -r '.subprotocol // empty' "$B")
}

# ws_run TICKET SECONDS [ORIGIN] — one WS session: sends an echo probe, captures
# all frames (binary passthrough + JSON control frames) into $WSOUT.
ws_run(){
  local t="$1" secs="$2" origin="${3:-$ORIGIN}"
  # The marker must not appear in what is typed. A pty echoes keystrokes back
  # over the same socket, so grepping the transcript for a string this script
  # sent proves only that the terminal echoes — it passes even if no shell ever
  # ran. Assembling the marker on the far side from a format string and an
  # argument means a match can only come from output.
  { printf "printf 'pickle-smoke-%%s' %s\r" "$TS"; sleep "$secs"; } | \
    timeout $((secs+20)) websocat --binary -H "Origin: $origin" \
      --protocol "pickle.terminal.v1, ticket.$t" "$WS_URL" >"$WSOUT" 2>&1
}

# open_live_session TOKEN HOLD_SECS → sets LIVE_SID + WSPID for a held session,
# retrying mint→WS until the session actually registers in the admin mirror
# (bridge session-start reported). cloud-init keeps bouncing the guest sshd for a
# window after RUNNING (known boot-timing behavior), so even a session opened
# after an earlier success can hit `connection refused` — retry the real
# operation, like a client.
# Returns 0 with a live WSPID, or 1 (caller fails). Uses $SAT for the mirror poll.
open_live_session(){
  local tok="$1" hold="$2"
  for _ in $(seq 1 6); do
    mint "$VM" "$tok"; LIVE_SID=$SESSION_ID
    { printf 'sleep 300\n'; sleep "$hold"; } | timeout $((hold+20)) websocat --binary \
        -H "Origin: $ORIGIN" --protocol "pickle.terminal.v1, ticket.$TICKET" "$WS_URL" >"$WSOUT" 2>&1 &
    WSPID=$!
    for _ in $(seq 1 8); do
      sleep 2
      curl -sS -o "$B" -H "$(auth "$SAT")" "$BASE/admin/terminal-sessions" 2>/dev/null
      jq -e --arg s "$LIVE_SID" 'map(select(.sessionId==$s)) | length == 1' "$B" >/dev/null 2>&1 && return 0
      kill -0 "$WSPID" 2>/dev/null || break   # websocat exited (SSH failed) → retry
    done
    kill "$WSPID" 2>/dev/null; wait "$WSPID" 2>/dev/null; sleep 6
  done
  return 1
}

declare -a SCRATCH_EMAILS=()
VM=""; VM_DELETED=1; SAT=""; KILL_INITIAL=""
cleanup(){
  local rc=$?
  # restore the kill switch to its pre-run value
  # Only worth restoring if the run did not already put it back — and a token
  # from the top of the run is expired by now, so take a fresh one. Without both,
  # the trap issues a doomed request and reports a failure to undo something that
  # was never left undone.
  local now_kill
  now_kill=$(pgq "select value::text from settings where key='web_terminal_enabled'")
  if [ -n "$KILL_INITIAL" ] && [ "$now_kill" != "$KILL_INITIAL" ]; then
    SAT=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
    local kc
    kc=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BASE/admin/settings/web_terminal_enabled" \
      -H "$(auth "$SAT")" -H 'Content-Type: application/json' \
      -d "{\"value\":$KILL_INITIAL}") || kc=000
    [ "$kc" = 200 ] || echo "-- cleanup: web_terminal_enabled NOT restored to $KILL_INITIAL (http=${kc:-none}, live=$now_kill) --" >&2
  fi
  if [ -n "$VM" ] && [ "$VM_DELETED" != 1 ]; then
    echo "-- cleanup: removing leftover VM $VM --"
    local at; at=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
    local vname; vname=$(pgq "select name from vms where id=$VM")
    # curl succeeds on a rejection as readily as on a 202; without the code the
    # trap reports a cleanup it never performed and a real guest stays up.
    local dc
    dc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/admin/vms/$VM/force-delete" \
      -H "$(auth "$at")" -H 'Content-Type: application/json' \
      -d "{\"confirmName\":\"$vname\",\"overrideProtection\":true}") || dc=000
    if [ "$dc" = 202 ]; then
      echo "-- cleanup: force-delete accepted (202) --"
    else
      echo "-- cleanup: force-delete REJECTED (http=${dc:-none}); manual cleanup needed (vm id $VM) --" >&2
    fi
  fi
  for e in "${SCRATCH_EMAILS[@]:-}"; do
    [ -n "$e" ] || continue
    # Two things the statement had never been able to tell anyone, because it had
    # never executed: `groups` has no owner_id column (personal-group ownership
    # is a group_members row), and the personal group has to be identified
    # BEFORE those membership rows are deleted, otherwise there is nothing left
    # to find it by. audit_logs is append-only: detach the actor instead.
    pgx "do \$\$ declare uid bigint; gids bigint[]; begin
      select id into uid from users where email='$e';
      if uid is not null and not exists (select 1 from vms v join groups g on v.group_id=g.id
          join group_members gm on gm.group_id=g.id where gm.user_id=uid)
         and not exists (select 1 from vm_requests r where r.requester_id=uid) then
        select coalesce(array_agg(group_id), '{}') into gids from group_members where user_id=uid;
        update audit_logs set actor_id=null where actor_id=uid;
        update groups set deleted_by=null where deleted_by=uid;
        delete from notifications where user_id=uid;
        delete from user_consents where user_id=uid;
        delete from mfa_recovery_codes where user_id=uid;
        delete from mfa_login_tokens where user_id=uid;
        delete from user_mfa where user_id=uid;
        delete from user_status_changes where user_id=uid or actor_id=uid;
        delete from email_verifications where user_id=uid;
        delete from refresh_tokens where user_id=uid;
        delete from user_ssh_keys where user_id=uid;
        delete from auth_reverifications where user_id=uid;
        delete from group_members where user_id=uid;
        delete from groups g where g.id = any(gids) and g.kind='PERSONAL' and g.deleted_by is null
          and not exists (select 1 from group_members gm where gm.group_id=g.id)
          and not exists (select 1 from vms v where v.group_id=g.id);
        delete from users where id=uid;
      end if; end \$\$;"
    # A user who owns a VM or a request keeps their row by design (the not-exists
    # guard above skips them), so residue is not a failure — but it must be
    # visible, otherwise a cleanup that silently stops working looks identical.
    local left; left=$(pgq "select count(*) from users where email='$e'")
    [ "${left:-0}" = 0 ] || echo "-- cleanup: scratch user $e retained (owns a vm/request row) --"
  done
  rm -f "${TMPFILES[@]}"; rm -rf "$RT_DIR"
  echo; echo "web-terminal smoke: PASS=$P FAIL=$F"
  [ "$F" -eq 0 ] && [ $rc -eq 0 ] || exit 1
}
trap cleanup EXIT

command -v websocat >/dev/null || { echo "FATAL: websocat not installed"; exit 1; }

SAT=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
[ -n "$SAT" ] && ok "sysadmin login" || { ko "sysadmin login"; exit 1; }
OAT=$(login "$ORGADMIN_EMAIL" "$ORGADMIN_PW")
[ -n "$OAT" ] && ok "orgadmin login" || ko "orgadmin login"

KILL_INITIAL=$(pgq "select value from settings where key='web_terminal_enabled'")
req "kill switch on (200)" 200 -X PUT "$BASE/admin/settings/web_terminal_enabled" -H "$(auth "$SAT")" \
  -H 'Content-Type: application/json' -d '{"value":true}'

# ── provision one dev VM (owner U1) ──────────────────────────────────────
# `limit 1` with no ORDER BY hands back whatever row the scan reaches first, and
# that is heap order, not catalog order — it silently moved from Ubuntu to Rocky
# once the catalog grew. The bridge resolves the guest account from the VM row, so
# any image works; pin the pick anyway so a guest-side failure is reproducible.
TPL=$(pgq "select id from os_images where status='ACTIVE' order by id limit 1")
# The state the bootstrap runbook leaves behind — catalog rows registered but
# none enabled yet — makes this empty, and an empty id is interpolated into the
# payload as "imageId":, which is not JSON. The request then fails as a bare
# 400 that says nothing about the catalog, so state the reason here instead.
[ -n "$TPL" ] || { ko "no ACTIVE OS image to request with (enable one in the catalog)"; exit 1; }
ORG=$(pgq "select id from orgs limit 1")
[ -n "$ORG" ] || { ko "no org to request against"; exit 1; }
# os-images is a pure OS catalog — the spec axis is vm_flavors, and
# POST /vm-requests requires the chosen flavorId. Read the presets off the API
# (the removed catalog default_* columns would error in psql).
req "vm-flavors 200" 200 "$BASE/vm-flavors" -H "$(auth "$SAT")"
FSEL='(map(select(.name=="basic"))[0] // .[0])'
FID=$(jq -r "$FSEL.id // empty" "$B"); TPL_VCPU=$(jq -r "$FSEL.vcpu // empty" "$B")
TPL_MEM=$(jq -r "$FSEL.memoryMb // empty" "$B"); TPL_DISK=$(jq -r "$FSEL.diskGb // empty" "$B")
[ -n "$FID" ] && ok "flavor id=$FID (${TPL_VCPU}c/${TPL_MEM}MB/${TPL_DISK}GB)" || { ko "no ACTIVE vm-flavor — abort"; exit 1; }

U1PW='terminal-owner-1'
U1="smoke-term-own-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U1")
read -r U1T _ <<<"$(mk_user "$U1" "$U1PW" '터미널소유자')"
req "create team 201" 201 -X POST "$BASE/groups" -H "$(auth "$U1T")" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"TEAM\",\"name\":\"smoke-term-$TS\",\"slug\":\"smoke-term-$TS\"}"
GID=$(jq -r '.id' "$B")
req "vm request 201" 201 -X POST "$BASE/vm-requests" -H "$(auth "$U1T")" -H 'Content-Type: application/json' \
  -d "{\"groupId\":$GID,\"orgId\":$ORG,\"imageId\":$TPL,\"flavorId\":$FID,\"purpose\":\"터미널 스모크\",\"courseOrProject\":null,\"specReason\":null,\"extraNote\":null,\"reqVcpu\":$TPL_VCPU,\"reqMemoryMb\":$TPL_MEM,\"reqDiskGb\":$TPL_DISK,\"reqStartDate\":null,\"reqEndDate\":null}"
RID=$(jq -r '.id // empty' "$B"); [ -n "$RID" ] || { ko "request not created — abort"; exit 1; }
req "approve 200" 200 -X POST "$BASE/admin/vm-requests/$RID/approve" -H "$(auth "$SAT")" \
  -H 'Content-Type: application/json' \
  -d "{\"grantedVcpu\":$TPL_VCPU,\"grantedMemoryMb\":$TPL_MEM,\"grantedDiskGb\":$TPL_DISK,\"grantedImageId\":$TPL,\"grantedStartDate\":null,\"grantedEndDate\":null,\"nodeId\":null,\"comment\":\"터미널 스모크\"}"
VM=$(pgq "select id from vms where request_id=$RID"); [ -n "$VM" ] || { ko "no VM row — abort"; exit 1; }
VM_DELETED=0
echo "  waiting for RUNNING (vm=$VM)…"
ST=""
for _ in $(seq 1 60); do ST=$(pgq "select status from vms where id=$VM"); [ "$ST" = RUNNING ] && break; sleep 10; done
[ "$ST" = RUNNING ] && ok "vm RUNNING" || { ko "vm RUNNING (got $ST) — abort"; exit 1; }
# cloud-init keeps settling after RUNNING and restarts sshd in that window, so an
# immediate connect can hit `connection refused` (RUNNING ≠ sshd
# ready). Poll :22 from the bridge LXC (which has the vmbr2 vantage) until it
# accepts, bounded — a real user connects after the VM-created mail, by which
# time sshd has settled; the smoke reproduces that by waiting.
VM_IP=$(pgq "select a.ip from vms v join ip_allocations a on v.ip_allocation_id=a.id where v.id=$VM")
SSHD_READY=0
for _ in $(seq 1 30); do
  if pct exec 102 -- bash -c "timeout 3 bash -c '</dev/tcp/${VM_IP}/22' 2>/dev/null"; then SSHD_READY=1; break; fi
  sleep 5
done
[ "$SSHD_READY" = 1 ] && ok "vm sshd accepting (:22)" || ko "vm sshd not ready after ~150s"

# ── who may open a terminal ──────────────────────────────────────────────
# Group membership no longer decides this. The group carries two rungs, and
# reaching a VM's contents takes an entry in that VM's own access list, so the
# three people below differ only in what the list says about them: U2 is listed,
# U3 is a group member the list does not mention, U4 is outside the group
# entirely. U1 requested the VM and is therefore its listed owner.
U2="smoke-term-mem-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U2")
read -r U2T U2ID <<<"$(mk_user "$U2" 'terminal-member-1' '터미널멤버')"
U3="smoke-term-view-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U3")
read -r U3T _ <<<"$(mk_user "$U3" 'terminal-viewer-1' '터미널뷰어')"
U4="smoke-term-out-$TS@pusan.ac.kr"; SCRATCH_EMAILS+=("$U4")
read -r U4T _ <<<"$(mk_user "$U4" 'terminal-outsider-1' '터미널외부')"
req "add U2 to group 201" 201 -X POST "$BASE/groups/$GID/members" -H "$(auth "$U1T")" \
  -H "$(rt "$U1T" "$U1PW")" -H 'Content-Type: application/json' -d "{\"email\":\"$U2\",\"role\":\"MEMBER\"}"
req "add U3 to group 201" 201 -X POST "$BASE/groups/$GID/members" -H "$(auth "$U1T")" \
  -H "$(rt "$U1T" "$U1PW")" -H 'Content-Type: application/json' -d "{\"email\":\"$U3\",\"role\":\"MEMBER\"}"

# The seeded list: whoever requested the VM, and nobody else.
req "access list seeded with the requester" 200 "$BASE/vms/$VM/access" -H "$(auth "$U1T")"
SEEDED=$(jq -r '[.grants[] | select(.role=="OWNER")] | length' "$B")
[ "$SEEDED" = 1 ] && ok "  one OWNER entry" || ko "  one OWNER entry (got $SEEDED)"
[ "$(jq -r '.grants | length' "$B")" = 1 ] && ok "  and no other entry" || ko "  and no other entry ($(jq -r '.grants|length' "$B"))"
[ "$(jq -r '.vm.id' "$B")" = "$VM" ] && ok "  list names its VM" || ko "  list names its VM"

# U3 stays unlisted for the denial assertions further down; U2 gets the rung
# that carries terminal access.
req "grant U2 MEMBER on this VM 201" 201 -X POST "$BASE/vms/$VM/access" -H "$(auth "$U1T")" \
  -H "$(rt "$U1T" "$U1PW")" -H 'Content-Type: application/json' \
  -d "{\"granteeType\":\"USER\",\"userId\":$U2ID,\"role\":\"MEMBER\"}"
U2GRANT=$(jq -r '.id' "$B")
[ -n "$U2GRANT" ] && [ "$U2GRANT" != null ] && ok "  grant id returned" || { ko "  grant id returned"; exit 1; }
req "  granting needs a fresh password proof (403)" 403 -X POST "$BASE/vms/$VM/access" \
  -H "$(auth "$U1T")" -H 'Content-Type: application/json' \
  -d "{\"granteeType\":\"USER\",\"userId\":$U2ID,\"role\":\"VIEWER\"}"
C=$(jq -r '.code' "$B"); [ "$C" = REAUTH_REQUIRED ] && ok "  code REAUTH_REQUIRED" || ko "  code ($C)"

# ── happy path: mint → WS → echo → audit → admin visibility ──────────────
mint "$VM" "$U2T"
[ "$MINT_CODE" = 201 ] && ok "MEMBER mint (201)" || ko "MEMBER mint (got $MINT_CODE)"
[ "$WSPATH" = "/terminal/ws" ] && ok "wsPath /terminal/ws" || ko "wsPath ($WSPATH)"
[ "$SUBPROTO" = "pickle.terminal.v1" ] && ok "subprotocol fixed" || ko "subprotocol ($SUBPROTO)"
# cloud-init bounces sshd several times in the settle window after RUNNING (a bare
# TCP probe can catch a transient up-state that then drops), so the bridge→VM SSH
# can still hit `connection refused` on the first attempts. A real user reconnects;
# reproduce that by retrying the mint→WS→echo up to 5×. Once it succeeds sshd has
# settled and the rest of the run (admin/revalidation phases) is stable. The
# successful session's SESSION_ID feeds the audit assertions below.
ECHO_OK=0
for _ in $(seq 1 5); do
  ws_run "$TICKET" 8
  if grep -q "pickle-smoke-$TS" "$WSOUT"; then ECHO_OK=1; break; fi
  sleep 12
  mint "$VM" "$U2T"   # fresh one-time ticket for the retry
done
[ "$ECHO_OK" = 1 ] && ok "shell echo round-trip" || { ko "shell echo round-trip"; head -c 300 "$WSOUT"; echo; }
AUD_START=$(pgq "select count(*) from audit_logs where action='terminal.session_start' and detail::text like '%$SESSION_ID%'")
[ "${AUD_START:-0}" -ge 1 ] && ok "session_start audited" || ko "session_start audited"
# The audited clientIp must be the true TCP peer the LXC 100 stream tier saw.
# Since the 2026-07-28 main-domain cutover the default WS_URL resolves straight
# to LXC 100 (pve-node /etc/hosts hairpin entry — no CDN, no NAT round-trip), so the
# expected peer is this host's own egress address toward 198.18.1.10, computed
# here instead of a hardcoded public IP (which was only correct on the old
# CDN-proxied path).
# Compare the JSON field directly: pgq nests the SQL inside two levels of shell
# quoting, so a literal double quote in the pattern would terminate it early and
# the query would silently return nothing (i.e. a permanent false failure).
SMOKE_PEER_IP=$(ip -o route get 198.18.1.10 2>/dev/null | grep -o 'src [0-9.]*' | awk '{print $2}')
if [ -z "$SMOKE_PEER_IP" ]; then
  ko "peer IP for the audit assertion could not be resolved"
else
  AUD_IP=$(pgq "select count(*) from audit_logs where action='terminal.session_start' and detail->>'sessionId'='$SESSION_ID' and detail->>'clientIp'='$SMOKE_PEER_IP'")
  [ "${AUD_IP:-0}" -ge 1 ] && ok "audited clientIp = smoke peer IP ($SMOKE_PEER_IP)" || ko "audited clientIp = smoke peer IP ($SMOKE_PEER_IP)"
fi
AUD_END=$(pgq "select count(*) from audit_logs where action='terminal.session_end' and detail::text like '%$SESSION_ID%'")
[ "${AUD_END:-0}" -ge 1 ] && ok "session_end audited (CLIENT_CLOSED)" || ko "session_end audited"

# ── admin ops: live session listed (SYS + ORG scope), force-terminate ────
if open_live_session "$U2T" 90; then
  ok "live session established (visible to SYS_ADMIN)"
else
  ko "live session established"; WSPID=""
fi
req "admin list (ORG scope) 200" 200 -X GET "$BASE/admin/terminal-sessions" -H "$(auth "$OAT")"
jq -e --arg s "$LIVE_SID" 'map(select(.sessionId==$s)) | length == 1' "$B" >/dev/null \
  && ok "session visible to own-org ORG_ADMIN" || ko "session visible to own-org ORG_ADMIN"
req "force terminate 204" 204 -X POST "$BASE/admin/terminal-sessions/$LIVE_SID/terminate" -H "$(auth "$SAT")"
sleep 5
grep -q '"code":4002' "$WSOUT" && ok "client got exit frame 4002" || ko "client got exit frame 4002"
AUD_FT=$(pgq "select count(*) from audit_logs where action='terminal.force_terminate' and detail::text like '%$LIVE_SID%'")
[ "${AUD_FT:-0}" -ge 1 ] && ok "force_terminate audited" || ko "force_terminate audited"
AUD_FEND=$(pgq "select count(*) from audit_logs where action='terminal.session_end' and detail::text like '%$LIVE_SID%' and detail::text like '%FORCE_TERMINATED%'")
[ "${AUD_FEND:-0}" -ge 1 ] && ok "session_end FORCE_TERMINATED" || ko "session_end FORCE_TERMINATED"
req "terminate idempotent 204" 204 -X POST "$BASE/admin/terminal-sessions/$LIVE_SID/terminate" -H "$(auth "$SAT")"
wait "$WSPID" 2>/dev/null

# ── negatives ────────────────────────────────────────────────────────────
mint "$VM" "$U2T"; REUSE_TICKET=$TICKET
ws_run "$REUSE_TICKET" 3 >/dev/null 2>&1
ws_run "$REUSE_TICKET" 3
grep -q '"code":4000' "$WSOUT" && ok "ticket reuse → 4000" || ko "ticket reuse → 4000"
# Origin enforcement is asserted from the refusal itself, not from the absence of
# the echo marker: a dead bridge, a DNS blip or a broken websocat all produce
# that same absence and used to read as "Origin enforced". The bridge refuses a
# foreign Origin at the HTTP layer, before the upgrade, so websocat reports a 403
# handshake. The ticket survives that refusal (it is never redeemed), so
# replaying the SAME ticket with the console Origin is a paired control: only the
# Origin differs, and it must get past the handshake.
mint "$VM" "$U2T"; ORIGIN_TICKET=$TICKET
ws_run "$ORIGIN_TICKET" 3 "https://evil.example.com"
grep -q '403 Forbidden' "$WSOUT" \
  && ok "foreign Origin refused at the handshake (403)" \
  || { ko "foreign Origin refused at the handshake (403)"; head -c 300 "$WSOUT"; echo; }
grep -q "pickle-smoke-$TS" "$WSOUT" \
  && ko "foreign Origin reached the shell" \
  || ok "foreign Origin never reached the shell"
ws_run "$ORIGIN_TICKET" 3
grep -q '403 Forbidden' "$WSOUT" \
  && ko "console Origin also got 403 — the refusal above was not about the Origin" \
  || ok "same ticket with the console Origin passes the handshake (control)"
# Being in the group is not being on the list. U3 can see the VM exists — the
# group listing shows it — and gets no further.
mint "$VM" "$U3T"; [ "$MINT_CODE" = 403 ] && ok "group member not on the list denied (403)" || ko "group member not on the list denied (got $MINT_CODE)"
req "  and the VM detail is closed to them (403)" 403 "$BASE/vms/$VM" -H "$(auth "$U3T")"
mint "$VM" "$U4T"; [ "$MINT_CODE" = 404 ] && ok "non-member masked (404)" || ko "non-member masked (got $MINT_CODE)"
req "  the access list is closed to them too (403)" 403 "$BASE/vms/$VM/access" -H "$(auth "$U3T")"

# Removing the entry has to reach a session that is already open, not just the
# next mint: the bridge revalidates on a timer, so a revoked person keeps a live
# shell until it does.
# Held far longer than the poll below runs: if the session were merely reaching
# the end of its own hold, its disappearance would prove nothing.
if open_live_session "$U2T" 300; then
  ok "U2 holds a live session before the entry is removed"
  req "remove U2 from the access list 204" 204 -X DELETE "$BASE/vms/$VM/access/$U2GRANT" \
    -H "$(auth "$U1T")" -H "$(rt "$U1T" "$U1PW")"
  mint "$VM" "$U2T"; [ "$MINT_CODE" = 403 ] && ok "  new mint refused after removal (403)" || ko "  new mint refused after removal (got $MINT_CODE)"
  GONE=0
  for _ in $(seq 1 12); do
    sleep 10
    curl -sS -o "$B" -H "$(auth "$SAT")" "$BASE/admin/terminal-sessions" 2>/dev/null
    jq -e --arg s "$LIVE_SID" 'map(select(.sessionId==$s)) | length == 0' "$B" >/dev/null 2>&1 && { GONE=1; break; }
  done
  [ "$GONE" = 1 ] && ok "  the open session was closed by revalidation" || ko "  the open session survived removal for over 120s"
  kill "$WSPID" 2>/dev/null; wait "$WSPID" 2>/dev/null
  req "re-grant U2 MEMBER 201" 201 -X POST "$BASE/vms/$VM/access" -H "$(auth "$U1T")" \
    -H "$(rt "$U1T" "$U1PW")" -H 'Content-Type: application/json' \
    -d "{\"granteeType\":\"USER\",\"userId\":$U2ID,\"role\":\"MEMBER\"}"
  U2GRANT=$(jq -r '.id' "$B")
else
  ko "U2 could not hold a live session — removal convergence unchecked"
fi
# ssh_gateway_blocked is a vms COLUMN (V13, sys-admin per-VM block), not a
# vm_settings row — the mint gate + sshgw route both read vm.isSshGatewayBlocked().
pgx "update vms set ssh_gateway_blocked=true where id=$VM"
mint "$VM" "$U2T"; [ "$MINT_CODE" = 403 ] && ok "admin-blocked VM denied (403)" || ko "admin-blocked VM denied (got $MINT_CODE)"
pgx "update vms set ssh_gateway_blocked=false where id=$VM"

# session caps: pending tickets count toward the per-user cap (3)
mint "$VM" "$U2T"; mint "$VM" "$U2T"; mint "$VM" "$U2T"
mint "$VM" "$U2T"
if [ "$MINT_CODE" = 409 ] && grep -q TERMINAL_SESSION_LIMIT "$B"; then ok "per-user cap → 409 TERMINAL_SESSION_LIMIT"
else ko "per-user cap (got $MINT_CODE)"; fi
echo "  waiting 65s for ticket TTL to clear the cap…"; sleep 65

# ── revalidation: kill switch off closes a live session (also covers >100s CF idle) ──
if open_live_session "$U2T" 150; then ok "reval session established"; else ko "reval session established"; WSPID=""; fi
REVAL_SID=$LIVE_SID
req "kill switch off (200)" 200 -X PUT "$BASE/admin/settings/web_terminal_enabled" -H "$(auth "$SAT")" \
  -H 'Content-Type: application/json' -d '{"value":false}'
echo "  waiting ≤90s for the revalidation poll to close the session…"
# Detect closure by the server's exit control frame landing in WSOUT, NOT by
# `kill -0 $WSPID`: an exited-but-unwaited background job is a zombie that
# `kill -0` still reports as alive until reaped. The 60s revalidation poll +
# ~5s close, plus establishment slack, fits in this window.
CLOSED=0
for _ in $(seq 1 18); do grep -q '"type":"exit"' "$WSOUT" && { CLOSED=1; break; }; sleep 5; done
[ "$CLOSED" = 1 ] && ok "revalidation closed session ≤90s" || ko "revalidation closed session"
kill "$WSPID" 2>/dev/null; wait "$WSPID" 2>/dev/null
grep -q '"code":4005' "$WSOUT" && ok "client got exit frame 4005" || ko "client got exit frame 4005"
AUD_REND=$(pgq "select count(*) from audit_logs where action='terminal.session_end' and detail::text like '%$REVAL_SID%' and detail::text like '%REVALIDATION_DENIED%'")
[ "${AUD_REND:-0}" -ge 1 ] && ok "session_end REVALIDATION_DENIED" || ko "session_end REVALIDATION_DENIED"
mint "$VM" "$U2T"; [ "$MINT_CODE" = 503 ] && ok "mint with switch off → 503" || ko "mint with switch off (got $MINT_CODE)"
req "kill switch back on (200)" 200 -X PUT "$BASE/admin/settings/web_terminal_enabled" -H "$(auth "$SAT")" \
  -H 'Content-Type: application/json' -d '{"value":true}'

# ── password change ends a live terminal (ticket token_version vs current) ──
# The reason this check exists: "I think I am compromised, so I change my
# password" has to end the attacker's shell, and until the ticket carried the
# token version it did not — the REST token died while the terminal kept running.
echo "  waiting 65s for ticket TTL to clear the cap…"; sleep 65
if open_live_session "$U2T" 150; then ok "pw-change session established"; else ko "pw-change session established"; WSPID=""; fi
PWCHG_SID=$LIVE_SID
req "self password change (200)" 200 -X PUT "$BASE/me/password" -H "$(auth "$U2T")" \
  -H 'Content-Type: application/json' \
  -d '{"currentPassword":"terminal-member-1","newPassword":"terminal-member-2"}'
U2T=$(jq -r '.accessToken // empty' "$B")   # the change returns a fresh pair
echo "  waiting ≤90s for the revalidation poll to notice the token version…"
CLOSED=0
for _ in $(seq 1 18); do grep -q '"type":"exit"' "$WSOUT" && { CLOSED=1; break; }; sleep 5; done
[ "$CLOSED" = 1 ] && ok "password change closed session ≤90s" || ko "password change closed session"
kill "$WSPID" 2>/dev/null; wait "$WSPID" 2>/dev/null
grep -q '"code":4004' "$WSOUT" && ok "client got exit frame 4004" || ko "client got exit frame 4004"
AUD_PWEND=$(pgq "select count(*) from audit_logs where action='terminal.session_end' and detail::text like '%$PWCHG_SID%'")
[ "${AUD_PWEND:-0}" -ge 1 ] && ok "session_end audited after password change" || ko "session_end audited after password change"

# ── STOPPED VM → 409 ─────────────────────────────────────────────────────
refresh_tokens
req "shutdown 202" 202 -X POST "$BASE/vms/$VM/shutdown" -H "$(auth "$U1T")"
echo "  waiting for STOPPED…"
for _ in $(seq 1 30); do ST=$(pgq "select status from vms where id=$VM"); [ "$ST" = STOPPED ] && break; sleep 5; done
if [ "$ST" = STOPPED ]; then
  mint "$VM" "$U2T"; [ "$MINT_CODE" = 409 ] && ok "STOPPED mint → 409" || ko "STOPPED mint (got $MINT_CODE)"
else ko "vm did not stop (got $ST)"; fi

# ── teardown ─────────────────────────────────────────────────────────────
VNAME=$(pgq "select name from vms where id=$VM")
req "force-delete VM 202" 202 -X POST "$BASE/admin/vms/$VM/force-delete" -H "$(auth "$SAT")" \
  -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\",\"overrideProtection\":true}"
echo "  waiting for destroy…"
for _ in $(seq 1 30); do ST=$(pgq "select status from vms where id=$VM"); [ "$ST" = DELETED ] && break; sleep 5; done
[ "$ST" = DELETED ] && { ok "vm destroyed"; VM_DELETED=1; } || ko "vm destroyed (got $ST)"
