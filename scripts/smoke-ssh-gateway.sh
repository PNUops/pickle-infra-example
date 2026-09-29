#!/bin/bash
# shellcheck disable=SC2015,SC2086  # ok/ko idiom safe; $SSHKO/$SSHPO are intentional word-split option lists
# ==========================================================================
# SSH-gateway e2e — run on pve-node as root, AFTER api/console/sshgw deploy and
# the VM template build. Several checks
# assume the template's sudoers PASSWD override and the platform upstream
# key wiring (api.env PICKLE_SSH_PLATFORM_PUBLIC_KEY + sshgw upstream key).
#
# Provisions one real dev VM and drives the SSH-gateway launch-gate scenarios
# (per-user key identity, host-key pin, password opt-in, kill switches, route
# denials, client-IP preservation) through the Lightsail relay, exactly as a
# real off-campus client:
#   relay :22 (HAProxy send-proxy-v2) → WireGuard → sshgw proxyfront (PROXY v2)
#   → sshpiperd + route plugin v2 → user VM.
#
# Identity audit is verified on the **sshgw.session** row (actor_id = real
# user), the authenticated-session audit, NOT the route lookup (the route
# lookup runs on an unauthenticated offered key and is never a per-user
# record). Denials are verified on **sshgw.route_denied** rows scoped by this
# run's unique slug (+ the offered key fingerprint where it disambiguates).
#
# Force-deletes the VM and restores all mutated global/DB state on exit.
# ==========================================================================
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.

RELAY="${RELAY:-198.51.100.10}"          # raw Lightsail IP (works pre/post DNS flip)
CTID="${CTID:-101}"                      # pickle-api LXC (DB + env live here)
TS=$(date +%s)-$RANDOM
B=$(mktemp)
declare -a TMPFILES=("$B")

seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
# shellcheck source=scripts/lib/auth.sh
. "$(dirname "$0")/lib/auth.sh"
pgq(){ pct exec "$CTID" -- su - postgres -c "psql -d pickle_dev -tAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
# psql -c travels through the shell `su -c` spawns, which re-parses the statement
# (a `$$` there would expand to that shell's PID). Feed it on stdin instead, and
# let a failing statement say so: the host-key pin case below only tests the pin
# if its UPDATE really landed.
pgx(){
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -d pickle_dev -v ON_ERROR_STOP=1 -f -" <<<"$1" 2>&1); then
    printf 'pgx failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
}

ORGADMIN_EMAIL="$(seed_env PICKLE_SEED_ORGADMIN_EMAIL)"; ORGADMIN_EMAIL="${ORGADMIN_EMAIL:-orgadmin@pnuops.com}"; ORGADMIN_PW="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"
SYSADMIN_EMAIL="$(seed_env PICKLE_SEED_SYSADMIN_EMAIL)"; SYSADMIN_EMAIL="${SYSADMIN_EMAIL:-admin@pnuops.com}"; SYSADMIN_PW="$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)"

P=0; F=0; ok(){ echo "PASS  $1"; P=$((P+1)); }; ko(){ echo "FAIL  $1"; F=$((F+1)); }
req(){ local n="$1" e="$2"; shift 2; local c; c=$(curl -sS -o "$B" -w '%{http_code}' "$@"); [ "$c" = "$e" ] && { ok "$n ($c)"; return 0; } || { ko "$n (want $e got $c)"; head -c 300 "$B"; echo; return 1; }; }
# code_is EXPECTED NAME — asserts the Problem `code` of the last response, so a
# generic 403 can never satisfy a role-gate check.
code_is(){ local c; c=$(jq -r '.code // empty' "$B"); [ "$c" = "$1" ] && ok "$2 (code=$c)" || ko "$2 (code=${c:-none}, want $1)"; }

SSHKO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o PreferredAuthentications=publickey -o IdentitiesOnly=yes"
SSHPO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o PreferredAuthentications=password -o PubkeyAuthentication=no"
kssh(){ ssh $SSHKO -i "$1" "$2@$RELAY" "$3" 2>&1; }               # $1=keyfile $2=slug $3=cmd
pssh(){ sshpass -p "$1" ssh $SSHPO "$2@$RELAY" "$3" 2>&1; }        # $1=pw $2=slug $3=cmd
# try_connect MARKER CONNECT-FN ARGS… — retry a *positive* connect until MARKER
# appears; the guest sshd can still be (re)started by cloud-init for a window
# after the VM reports RUNNING (connection refused, not an auth failure). Prints
# the last output; returns 0 on success, 1 on timeout. Denial scenarios assert on
# gateway audit rows instead and never reach the VM sshd, so they don't use this.
try_connect(){ local marker="$1"; shift; local out=""; for _ in $(seq 1 12); do out=$("$@"); echo "$out" | grep -q "$marker" && { printf '%s' "$out"; return 0; }; sleep 5; done; printf '%s' "$out"; return 1; }
denied(){ local q="select count(*) from audit_logs where action='sshgw.route_denied' and detail->>'slug'='$SLUG' and detail->>'reason'='$1'"; [ -n "${2:-}" ] && q="$q and detail->>'fingerprint'='$2'"; pgq "$q"; }
mklocalkey(){ ssh-keygen -q -t ed25519 -N '' -C '' -f "$1"; TMPFILES+=("$1" "$1.pub"); }
fp_of(){ ssh-keygen -lf "$1" | awk '{print $2}'; }

# mk_user EMAIL PW NAME → echoes "<accessToken> <userId> <userId_DB>"
# (signup→verify→login). The id comes back twice because the two sides of this
# script want different values: the API takes the public UUID (grant payloads),
# while audit_logs.actor_id — like every foreign key — still holds the internal
# bigint. Callers keep the plain name for the API id and the _DB suffix for SQL.
mk_user(){ mk_verified_user "$BASE" "$@"; }
# issue_key TOKEN KEYFILE → issues THIS VM's key for that account, writes the
# private half to KEYFILE and echoes the fingerprint. Keys are per (user, VM)
# now, so there is nothing to paste and nothing to issue before the VM exists.
issue_key(){ curl -sS -o "$B" -X POST "$BASE/vms/$VM/ssh-key" -H "Authorization: Bearer $1" >/dev/null; jq -r '.privateKey // empty' "$B" > "$2"; chmod 600 "$2"; jq -r '.key.fingerprint // empty' "$B"; }
# key_status TOKEN → HTTP status of an issue attempt, for the refusal checks.
issue_status(){ curl -sS -o "$B" -w '%{http_code}' -X POST "$BASE/vms/$VM/ssh-key" -H "Authorization: Bearer $1"; }
# addmember EMAIL ROLE (as group OWNER). Asserted (201): a silently failed add
# would make the membership-scoped checks below vacuous — a VIEWER/MEMBER that
# was never added is denied as a plain non-member and the test still "passes".
# Members join through the invitation endpoint (the direct add was removed in
# contract v0.88.0). An item for an ACTIVE account answers ADDED and the account
# is a member at once, as MEMBER: the endpoint takes no role, and MEMBER is what
# the direct add used here. The outcome is asserted as well as the status,
# because a 200 also carries INVITED or ALREADY_MEMBER, and neither would make
# the membership checks below mean anything.
addmember(){
  req "add member ($1)" 200 -X POST "$BASE/workspaces/$GID/invitations" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"entries\":[{\"email\":\"$1\"}]}" || return 1
  local out; out=$(jq -r '.results[0].outcome // empty' "$B")
  [ "$out" = ADDED ] && ok "  $1 joined as a member (ADDED)" || { ko "  $1 not added (outcome=${out:-none})"; return 1; }
}
# addgrant USERID ROLE — put somebody on THIS VM's access list. Group membership
# admits nobody to a VM on its own; every rung below is granted per resource.
addgrant(){ req "grant $2 on the vm (user $1)" 201 -X POST "$BASE/vms/$VM/access" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"granteeType\":\"USER\",\"userId\":\"$1\",\"role\":\"$2\"}"; }

# ---- state to restore on exit ----
VM=""; VM_DB=""; VNAME=""; VM_DELETED=0; ORIG_HK_B64=""; ORIG_KILL=""
cleanup(){
  local rc=$?
  # ssh_host_key is multi-line (one entry per host-key type); back it up/restore
  # it as base64 so whitespace survives (pgq's tr -d space would corrupt it).
  [ -n "$ORIG_HK_B64" ] && [ -n "$VM_DB" ] && pgx "update vms set ssh_host_key=convert_from(decode('$ORIG_HK_B64','base64'),'UTF8') where id=$VM_DB"
  [ -n "$ORIG_KILL" ] && pgx "update settings set value='$ORIG_KILL'::jsonb where key='ssh_gateway_enabled'"
  if [ -n "$VM" ] && [ "$VM_DELETED" != 1 ]; then
    echo "-- cleanup: force-deleting leftover VM $VM --"
    local at; at=$(login_token "$BASE" "$SYSADMIN_EMAIL" "$SYSADMIN_PW") || at=""
    # The warning used to hang off `||` at the end of an && chain, so it could
    # only fire when the token or the name was missing: curl itself exits 0 on a
    # 403 or a 500, and the rejection printed nothing at all.
    if [ -n "$at" ] && [ -n "$VNAME" ]; then
      local dc
      dc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/admin/vms/$VM/force-delete" \
        -H "Authorization: Bearer $at" -H 'Content-Type: application/json' \
        -d "{\"confirmName\":\"$VNAME\",\"reason\":\"smoke cleanup (trap)\"}") || dc=000
      if [ "$dc" = 202 ]; then
        echo "-- cleanup: force-delete accepted (202) --"
      else
        echo "-- cleanup: force-delete REJECTED (http=${dc:-none}); manual cleanup needed (vm id $VM) --" >&2
      fi
    else
      echo "-- cleanup: no admin token or VM name; manual cleanup needed (vm id $VM) --" >&2
    fi
  fi
  # After the VM, which is deleted with the administrator's token: the run's
  # scratch users are closed last, on every exit. Every one of them is
  # sgw-<role>-$TS; a role this run never reached has no account and is skipped.
  local role
  for role in owner nm unlisted member editor; do
    if disable_scratch_user "sgw-${role}-${TS}@example.com"; then
      echo "-- cleanup: scratch user sgw-${role}-${TS} disabled --"
    else
      echo "-- cleanup: scratch user sgw-${role}-${TS} NOT disabled --" >&2; rc=1
    fi
  done
  rm -f "${TMPFILES[@]}"
  exit "$rc"
}
trap cleanup EXIT

# ==========================================================================
echo "== [0] advertised SSH hostname resolves to the relay =="
# The scenarios below connect to the relay by raw IP so they work either side of a
# DNS flip. That deliberately leaves the name users are actually told to type
# untested: a missing or stale A record would not fail a single check here. This
# asserts the advertised host — the value the API hands to the console — points at
# the relay this run just exercised, and that something answers SSH on it.
ADV_HOST=$(seed_env PICKLE_SSH_HOST)
if [ -z "$ADV_HOST" ]; then
  ko "advertised SSH host is NOT set in api.env (PICKLE_SSH_HOST)"
else
  ok "advertised SSH host = $ADV_HOST"
  ADV_IPS=$(getent ahostsv4 "$ADV_HOST" 2>/dev/null | awk '{print $1}' | sort -u)
  if echo "$ADV_IPS" | grep -qx "$RELAY"; then
    ok "$ADV_HOST resolves to the relay ($RELAY)"
  else
    ko "$ADV_HOST resolves to [${ADV_IPS:-nothing}], expected $RELAY"
  fi
  # Capture the banner, then test the string. Piping nc into `head -c` and
  # testing the pipeline instead would report FAIL on a healthy relay: head
  # exits at its byte count, nc keeps the connection until timeout kills it
  # (124), and pipefail makes that the pipeline's status regardless of the match.
  BANNER=$(timeout 5 nc "$ADV_HOST" 22 2>/dev/null | head -c 64 || true)
  case "$BANNER" in
    SSH-*) ok "$ADV_HOST:22 answers with an SSH banner" ;;
    *)     ko "$ADV_HOST:22 did not answer with an SSH banner" ;;
  esac
fi

echo "== provision (owner O creates group + VM) =="
OWNER_EMAIL="sgw-owner-${TS}@example.com"; OWNER_PW="sgw-pass-${TS}!"
read -r OAT OUID OUID_DB < <(mk_user "$OWNER_EMAIL" "$OWNER_PW" "SGW Owner")
{ [ -n "$OAT" ] && [ -n "$OUID" ] && [ -n "$OUID_DB" ]; } && ok "owner user id=$OUID" || { ko "owner signup"; exit 1; }
req "group" 201 -X POST "$BASE/workspaces" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"name\":\"sgw\",\"kind\":\"PROJECT\"}" || exit 1
GID=$(jq -r .id "$B")
# the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
req "orgadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$ORGADMIN_EMAIL\",\"password\":\"$ORGADMIN_PW\"}" || exit 1
AAT=$(jq -r .accessToken "$B")
req "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $AAT" || exit 1; OID=$(jq -r '.[0].id' "$B")
req "os-images" 200 "$BASE/os-images" -H "Authorization: Bearer $OAT" || exit 1
TID=$(jq -r '.[0].id // empty' "$B")
# An empty catalog — the state the bootstrap runbook leaves behind, rows
# registered but none enabled — would otherwise reach the request payload as
# "imageId":, which is not JSON, and surface as a bare 400 saying nothing
# about the catalog.
[ -n "$TID" ] || { ko "no ACTIVE OS image to request with (enable one in the catalog)"; exit 1; }
# os-images is the OS catalog; the spec axis is vm-flavors and POST
# /requests requires the chosen flavorId ('basic', else the first ACTIVE row)
req "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $OAT" || exit 1
FSEL='(map(select(.name=="basic"))[0] // .[0])'
FID=$(jq -r "$FSEL.id // empty" "$B"); VC=$(jq -r "$FSEL.vcpu // empty" "$B"); MM=$(jq -r "$FSEL.memoryMb // empty" "$B"); DG=$(jq -r "$FSEL.diskGb // empty" "$B")
[ -n "$FID" ] && ok "flavor id=$FID (${VC}c/${MM}MB/${DG}GB)" || { ko "no ACTIVE vm-flavor"; exit 1; }
# Every id the API speaks is a UUID, so each one goes into the payload quoted;
# only the spec numbers (vcpu/memory/disk) stay bare.
req "request" 201 -X POST "$BASE/requests" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"type\":\"VM\",\"displayName\":\"SSH 게이트웨이 e2e $TS\",\"workspaceId\":\"$GID\",\"orgId\":\"$OID\",\"purpose\":\"ssh gateway e2e\",\"courseOrProject\":null,\"extraNote\":null,\"reqStartDate\":null,\"reqEndDate\":null,\"reqIndefinite\":true,\"vm\":{\"imageId\":\"$TID\",\"flavorId\":\"$FID\",\"reqVcpu\":$VC,\"reqMemoryMb\":$MM,\"reqDiskGb\":$DG,\"specReason\":null}}" || exit 1
RID=$(jq -r .id "$B")
req "approve" 200 -X POST "$BASE/admin/requests/$RID/approve" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"grantedStartDate\":null,\"grantedEndDate\":null,\"comment\":\"sgw\",\"vm\":{\"grantedVcpu\":$VC,\"grantedMemoryMb\":$MM,\"grantedDiskGb\":$DG,\"grantedImageId\":\"$TID\",\"nodeId\":null}}" || exit 1
req "vm list" 200 "$BASE/vms?workspaceId=$GID" -H "Authorization: Bearer $OAT" || exit 1
VM=$(jq -r '.content[0].id // empty' "$B"); VNAME=$(jq -r '.content[0].name // empty' "$B")
[ -n "$VM" ] && ok "vm id=$VM name=$VNAME" || { ko "vm id (empty list)"; exit 1; }
# Two ids for one VM, and they are not interchangeable: VM is the UUID the API
# speaks (URLs, JSON), VM_DB is the internal key every direct statement below
# runs on — including the host-key swap the whole pin scenario depends on.
# Resolve it once, here, and refuse to go on without it: an empty value would
# turn `where id=` into a syntax error much later, and the cleanup trap would
# then quietly fail to restore the original host key.
VM_DB=$(pgq "select id from vms where public_id='$VM'")
[ -n "$VM_DB" ] && ok "internal vm id resolved ($VM_DB)" || { ko "no vms row for public_id $VM"; exit 1; }

echo "== poll RUNNING =="
DL=$((SECONDS+900)); ST=""
while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $OAT"; ST=$(jq -r .status "$B"); VIP=$(jq -r '.ipAddress // empty' "$B")
  [ "$ST" = "RUNNING" ] && break; { [ "$ST" = "ERROR" ] || [ "$ST" = "NEEDS_ADMIN" ]; } && { ko "provision $ST"; break; }
  [ "$SECONDS" -ge "$DL" ] && { ko "not RUNNING (last=$ST)"; break; }; sleep 10; done
[ "$ST" = "RUNNING" ] && ok "VM RUNNING ip=$VIP" || exit 1
SLUG=$(pgq "select hostname from vms where id=$VM_DB")
[ -n "$SLUG" ] && ok "slug(hostname)=$SLUG" || { ko "slug"; exit 1; }
# base64 so the multi-line value round-trips intact (pgq strips whitespace).
ORIG_HK_B64=$(pgq "select encode(convert_to(ssh_host_key,'UTF8'),'base64') from vms where id=$VM_DB")
[ -n "$ORIG_HK_B64" ] && ok "host key collected at provisioning" || ko "no vms.ssh_host_key (HOSTKEY step?)"
for _ in $(seq 1 18); do nc -z -w5 "$VIP" 22 2>/dev/null && break; sleep 5; done

# --- 1. issue this VM's key + re-download the private half ---
echo "== [1] issue the VM's key =="
req "issue key" 201 -X POST "$BASE/vms/$VM/ssh-key" -H "Authorization: Bearer $OAT" || exit 1
OFPR=$(jq -r .key.fingerprint "$B")
OKEY=$(mktemp); TMPFILES+=("$OKEY"); jq -r .privateKey "$B" > "$OKEY"; chmod 600 "$OKEY"
[ -n "$OFPR" ] && ok "issued fp=$OFPR file=$(jq -r .fileName "$B")" || ko "no fingerprint in the issue response"
req "issuing twice conflicts" 409 -X POST "$BASE/vms/$VM/ssh-key" -H "Authorization: Bearer $OAT" || ko "second issue did not conflict"
req "re-download private key" 200 "$BASE/vms/$VM/ssh-key/private-key" -H "Authorization: Bearer $OAT" || exit 1
[ "$(jq -r .key.fingerprint "$B")" = "$OFPR" ] && ok "re-download returns the same key" || ko "re-download fingerprint differs"

# --- 2. connect with the key ---
echo "== [2] publickey SSH via relay =="
OUT=$(kssh "$OKEY" "$SLUG" 'echo PICKLE-SGW-OK; id -un')
echo "$OUT" | grep -q PICKLE-SGW-OK && ok "publickey SSH reached VM shell" || ko "publickey SSH failed ($(echo "$OUT" | tr '\n' ' ' | head -c 80))"

# --- 3. audit actor = real user (sshgw.session, NOT route) ---
echo "== [3] audit: sshgw.session actor_id = real user =="
sleep 1
ACT=$(pgq "select actor_id from audit_logs where action='sshgw.session' and detail->>'slug'='$SLUG' order by id desc limit 1")
[ "$ACT" = "$OUID_DB" ] && ok "sshgw.session actor_id=$ACT is the real user" || ko "session actor_id=$ACT want $OUID_DB"

# --- session audit: real client IP preserved end-to-end (not tunnel/gw addr) ---
echo "== session audit: client IP preserved =="
AIP=$(pgq "select ip from audit_logs where action='sshgw.session' and detail->>'slug'='$SLUG' order by id desc limit 1")
[ -n "$AIP" ] && ok "sshgw.session ip=$AIP (slug=$SLUG)" || ko "no sshgw.session audit for slug=$SLUG"
case "$AIP" in 100.64.0.*|198.18.*|"") ko "audit ip is tunnel/gateway not real client ($AIP)";; *) ok "audit ip is a real client IP ($AIP)";; esac

# --- 7. host-key mismatch → refuse (pin enforced; run while O's key is still valid) ---
echo "== [7] host-key mismatch → session refused =="
# mktemp -u (a path, not a file) like every other keygen here: ssh-keygen must
# create the file itself, and an existing path stops it on an interactive
# "Overwrite (y/n)?" prompt. That left BOGUS_PUB empty, so the pin became '' and
# the route was refused one gate earlier (no collected host key) — the check
# passed without ever exercising the pin.
BOGUS=$(mktemp -u); mklocalkey "$BOGUS"; BOGUS_PUB=$(cat "$BOGUS.pub")
[ -n "$BOGUS_PUB" ] && ok "bogus host key generated (pin will differ, not be empty)" || ko "bogus host key empty — [7] would not test the pin"
AID=$(pgq "select coalesce(max(id),0) from audit_logs")
# The gateway logs one warning per refused upstream verify. Counting that line
# before and after is the only positive evidence that the pin is what refused:
# every other reason the SSH could die (relay down, wg handshake gone, sshpiperd
# stopped) leaves the count unchanged while still producing no SHOULDNOTREACH.
hk_mismatch_count(){ pct exec 102 -- sh -c \
  "journalctl -u sshpiperd --no-pager 2>/dev/null | grep -c 'upstream host key mismatch'" \
  2>/dev/null | tr -d '[:space:]'; }
HKM_BEFORE=$(hk_mismatch_count)
pgx "update vms set ssh_host_key='$BOGUS_PUB' where id=$VM_DB"
PINNED=$(pgq "select count(*) from vms where id=$VM_DB and ssh_host_key='$BOGUS_PUB'")
[ "${PINNED:-0}" = 1 ] && ok "bogus host key stored on the vm row" || ko "bogus host key not stored — the pin was never swapped"
OUT=$(kssh "$OKEY" "$SLUG" 'echo SHOULDNOTREACH')
if echo "$OUT" | grep -q SHOULDNOTREACH; then ko "mismatched host key still connected (pin not enforced)"
else
  sleep 1
  # The refusal has to come from the host-key pin itself. A *different* key still
  # routes (route lookups that pass every gate are granted and not audited) and
  # dies at the gateway's upstream verify, so: no new route_denied row for this
  # slug, and no sshgw.session row (the session never establishes). A route_denied
  # here would mean an earlier gate refused instead — exactly what an empty pin did.
  DENR=$(pgq "select coalesce(string_agg(distinct detail->>'reason',','),'') from audit_logs where id>$AID and action='sshgw.route_denied' and detail->>'slug'='$SLUG'")
  [ -z "$DENR" ] && ok "route granted, refused at the host-key pin (no route_denied)" || ko "refused before the pin (route_denied reason=$DENR)"
  SESS=$(pgq "select count(*) from audit_logs where id>$AID and action='sshgw.session' and detail->>'slug'='$SLUG'")
  [ "${SESS:-0}" = "0" ] && ok "host-key mismatch refused the session (no sshgw.session)" || ko "sshgw.session recorded despite a mismatched pin"
  # Without this the two checks above are also satisfied by an SSH that never
  # reached the upstream verify at all. The gateway's own warning is the proof
  # that the refusal happened at the pin.
  HKM_AFTER=$(hk_mismatch_count)
  [ "${HKM_AFTER:-0}" -gt "${HKM_BEFORE:-0}" ] 2>/dev/null \
    && ok "gateway logged an upstream host-key mismatch (${HKM_BEFORE} → ${HKM_AFTER})" \
    || { ko "no new upstream host-key mismatch in the gateway log (${HKM_BEFORE} → ${HKM_AFTER}) — the SSH died before the pin"; echo "$OUT" | head -c 300; echo; }
fi
pgx "update vms set ssh_host_key=convert_from(decode('$ORIG_HK_B64','base64'),'UTF8') where id=$VM_DB"
RESTORED=$(pgq "select count(*) from vms where id=$VM_DB and ssh_host_key is not null and ssh_host_key<>'$BOGUS_PUB'")
[ "${RESTORED:-0}" = 1 ] && ok "original host key restored on the vm row" || ko "host key not restored — later cases run against a bogus pin"

# --- 4. unregistered key → SSHGW_KEY_UNKNOWN ---
echo "== [4] unregistered key → deny =="
UNREG=$(mktemp -u); mklocalkey "$UNREG"; UNREG_FP=$(fp_of "$UNREG.pub")
kssh "$UNREG" "$SLUG" 'echo X' | grep -q '^X$' && ko "unregistered key routed" || { sleep 1; [ "$(denied SSHGW_KEY_UNKNOWN "$UNREG_FP")" -ge 1 ] 2>/dev/null && ok "unregistered key denied (SSHGW_KEY_UNKNOWN)" || ko "no SSHGW_KEY_UNKNOWN audit for unreg fp"; }

# --- 5. a non-member cannot obtain this VM's key at all ---
# Keys are per (user, VM), so the refusal now happens one step earlier than it
# used to: there is no account-wide key to register and then be turned away with.
echo "== [5] non-member cannot issue this VM's key =="
NM_PW="nm-pw-${TS}!"
read -r NMAT _ _ < <(mk_user "sgw-nm-${TS}@example.com" "$NM_PW" "SGW NonMember")
NM_ST=$(issue_status "$NMAT")
[ "$NM_ST" = 404 ] && ok "non-member issue masked as 404" || ko "non-member issue returned $NM_ST, want 404"

# --- 6. in the group, not on the VM's list → SSHGW_KEY_NOT_MEMBER ---
# The gateway asks the access list, not the group. Somebody the owner invited to
# the group but never added to this VM is refused with the same code as a
# stranger, so the refusal leaks nothing about who is a colleague.
echo "== [6] group member absent from the access list → deny =="
VW_PW="vw-pw-${TS}!"
read -r VWAT VW_ID _ < <(mk_user "sgw-unlisted-${TS}@example.com" "$VW_PW" "SGW Unlisted")
addmember "sgw-unlisted-${TS}@example.com"
# A workspace member can see the VM listed, so the refusal is an honest 403
# rather than the 404 an outsider gets, the same split every VM op draws.
VW_ST=$(issue_status "$VWAT")
[ "$VW_ST" = 403 ] && ok "unlisted member issue refused with 403" || ko "unlisted member issue returned $VW_ST, want 403"
# Other things answer 403 too, so the Problem code is asserted rather than the
# status alone: without it the check would pass on any refusal at all.
code_is WORKSPACE_ROLE_INSUFFICIENT "  refused by the role gate"

# The same person, once listed, reaches the shell — otherwise the denial above
# would also pass if the gateway were simply broken for everyone but the owner.
echo "== [7] once listed, they can issue a key and reach the shell =="
addgrant "$VW_ID" MEMBER
VWKEY=$(mktemp); TMPFILES+=("$VWKEY")
VW_FP=$(issue_key "$VWAT" "$VWKEY")
[ -n "$VW_FP" ] && ok "listed member issued their own key fp=$VW_FP" || ko "listed member could not issue a key"
sleep 1
try_connect PICKLE-LISTED kssh "$VWKEY" "$SLUG" 'echo PICKLE-LISTED' >/dev/null \
  && ok "listed member routed to the VM" || ko "listed member still refused"

# Two people on one VM hold two different keys — the pair is the unit, not the VM.
[ "$VW_FP" != "$OFPR" ] && ok "each member holds their own key for this VM" || ko "two members share one fingerprint"

# --- 7b. grant revoked → the key they already downloaded stops working ---
# The row is deliberately NOT deleted when a grant ends; the gateway is the one
# choke point that refuses it, and this is the check that says so.
echo "== [7b] revoke the grant → their key is refused =="
GRANT_ID=$(pgq "select public_id from resource_access_grants where resource_type='VM' and resource_id=$VM_DB and user_id=(select id from users where email='sgw-unlisted-${TS}@example.com')")
req "revoke the grant" 204 -X DELETE "$BASE/vms/$VM/access/$GRANT_ID" -H "Authorization: Bearer $OAT" || ko "revoke grant"
sleep 1
kssh "$VWKEY" "$SLUG" 'echo X' | grep -q '^X$' && ko "revoked member still routed" || { sleep 1; [ "$(denied SSHGW_KEY_NOT_MEMBER "$VW_FP")" -ge 1 ] 2>/dev/null && ok "revoked member denied (SSHGW_KEY_NOT_MEMBER)" || ko "no SSHGW_KEY_NOT_MEMBER audit after revocation"; }
[ "$(pgq "select count(*) from vm_ssh_keys where fingerprint_sha256='$VW_FP'")" = 1 ] \
  && ok "the key row survives the revocation (the gateway is the gate)" || ko "the key row was deleted on revocation"

# --- 8. password default-deny (ssh_password_enabled=false) → SSHGW_PASSWORD_DISABLED ---
echo "== [8] password default-deny =="
req "reveal password" 200 "$BASE/vms/$VM/password" -H "Authorization: Bearer $OAT" || exit 1
VMPW=$(jq -r .password "$B")
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "password worked while disabled" || { sleep 1; [ "$(denied SSHGW_PASSWORD_DISABLED)" -ge 1 ] 2>/dev/null && ok "password denied by default (SSHGW_PASSWORD_DISABLED)" || ko "no SSHGW_PASSWORD_DISABLED audit"; }

# --- 9. opt-in password enable → password SSH allowed ---
echo "== [9] opt-in password enable → allowed =="
req "enable ssh_password" 200 -X PATCH "$BASE/vms/$VM/settings" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d '{"settings":{"ssh_password_enabled":true}}' || ko "enable settings"
sleep 1
try_connect PICKLE-PW-OK pssh "$VMPW" "$SLUG" 'echo PICKLE-PW-OK' >/dev/null && ok "password SSH allowed after opt-in" || ko "password SSH failed after opt-in"

# --- 10. MEMBER cannot change VM settings → 403 ---
echo "== [10] MEMBER PATCH settings → 403 =="
MB_PW="mb-pw-${TS}!"
read -r MBAT MB_ID _ < <(mk_user "sgw-member-${TS}@example.com" "$MB_PW" "SGW Member")
addmember "sgw-member-${TS}@example.com"
# Listed at the rung that carries access but not editing. Granting first is what
# makes this a test of the rung: an unlisted person is refused one step earlier,
# and the check would pass without the settings gate ever being consulted.
addgrant "$MB_ID" MEMBER
req "member settings forbidden" 403 -X PATCH "$BASE/vms/$VM/settings" -H "Authorization: Bearer $MBAT" -H 'Content-Type: application/json' -d '{"settings":{"ssh_password_enabled":false}}'
code_is WORKSPACE_ROLE_INSUFFICIENT "  refused by the role gate"

# --- 12. EDITOR cannot raise password_reveal_min_role (OWNER-gated) → 403 ---
echo "== [12] EDITOR raise min_role → 403 =="
ED_PW="ed-pw-${TS}!"
read -r EDAT ED_ID _ < <(mk_user "sgw-editor-${TS}@example.com" "$ED_PW" "SGW Editor")
addmember "sgw-editor-${TS}@example.com"
addgrant "$ED_ID" EDITOR
req "editor min_role forbidden" 403 -X PATCH "$BASE/vms/$VM/settings" -H "Authorization: Bearer $EDAT" -H 'Content-Type: application/json' -d '{"settings":{"password_reveal_min_role":"EDITOR"}}'
code_is WORKSPACE_ROLE_INSUFFICIENT "  refused by the role gate"

# --- 13. sudo demands a password inside the VM (sudoers PASSWD override) ---
echo "== [13] sudo -n fails in guest (NOPASSWD overridden) =="
# A marker proves the shell was reached before judging the sudo result, so a
# connection blip can't masquerade as "sudo refused" (a false pass).
OUT=$(try_connect PICKLE-SUDO pssh "$VMPW" "$SLUG" 'echo PICKLE-SUDO; sudo -n true >/dev/null 2>&1; echo RC=$?')
if ! echo "$OUT" | grep -q PICKLE-SUDO; then ko "[13] could not reach VM shell to test sudo"
elif echo "$OUT" | grep -q 'RC=0'; then ko "sudo -n succeeded — zz-pickle PASSWD override missing (template rebuilt?)"
else ok "sudo requires a password (sudo -n refused)"; fi

# --- 15. password regenerate → new password, audited, old fails ---
echo "== [15] password regenerate =="
req "regenerate password" 200 -X POST "$BASE/vms/$VM/password/regenerate" -H "Authorization: Bearer $OAT" || ko "regenerate"
NEWPW=$(jq -r .password "$B")
[ -n "$NEWPW" ] && [ "$NEWPW" != "$VMPW" ] && ok "password changed on regenerate" || ko "regenerate did not change password"
sleep 1
# audit_logs.target_id is the one column that speaks the public id: it is text
# now and records what the API handed out, so this lookup takes $VM, not $VM_DB.
[ "$(pgq "select count(*) from audit_logs where action='vm.password_regenerate' and target_id='$VM'")" -ge 1 ] 2>/dev/null && ok "vm.password_regenerate audited" || ko "no vm.password_regenerate audit"
try_connect PICKLE-NEWPW-OK pssh "$NEWPW" "$SLUG" 'echo PICKLE-NEWPW-OK' >/dev/null && ok "new password works" || ko "new password failed"
# sshd is confirmed up by the line above, so a refused old password here is a
# genuine auth rejection, not a not-ready blip.
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "old password still works after regenerate" || ok "old password rejected"
VMPW="$NEWPW"

# --- 11a. re-issue → the key that worked in [2] stops working ---
# Re-issue is what a user reaches for when a private key leaks, so the old one
# has to die the moment the new one exists.
echo "== [11a] re-issue → the old key is refused =="
req "re-issue key" 200 -X POST "$BASE/vms/$VM/ssh-key/reissue" -H "Authorization: Bearer $OAT" || ko "re-issue key"
NEW_FPR=$(jq -r .key.fingerprint "$B")
NEWKEY=$(mktemp); TMPFILES+=("$NEWKEY"); jq -r .privateKey "$B" > "$NEWKEY"; chmod 600 "$NEWKEY"
[ "$NEW_FPR" != "$OFPR" ] && ok "re-issue produced a different key" || ko "re-issue returned the same fingerprint"
sleep 1
kssh "$OKEY" "$SLUG" 'echo X' | grep -q '^X$' && ko "the superseded key still routed" || { sleep 1; [ "$(denied SSHGW_KEY_UNKNOWN "$OFPR")" -ge 1 ] 2>/dev/null && ok "superseded key denied (SSHGW_KEY_UNKNOWN)" || ko "no SSHGW_KEY_UNKNOWN audit for the superseded key"; }
try_connect PICKLE-REISSUED kssh "$NEWKEY" "$SLUG" 'echo PICKLE-REISSUED' >/dev/null \
  && ok "the re-issued key reaches the shell" || ko "the re-issued key does not work"

# --- 11b. delete key → immediate deny ---
echo "== [11b] delete key → immediate deny =="
req "delete key" 204 -X DELETE "$BASE/vms/$VM/ssh-key" -H "Authorization: Bearer $OAT" || ko "delete key"
kssh "$NEWKEY" "$SLUG" 'echo X' | grep -q '^X$' && ko "deleted key still routed" || { sleep 1; [ "$(denied SSHGW_KEY_UNKNOWN "$NEW_FPR")" -ge 1 ] 2>/dev/null && ok "deleted key denied immediately (SSHGW_KEY_UNKNOWN)" || ko "no SSHGW_KEY_UNKNOWN audit for the deleted key fp"; }
req "status after deletion" 200 "$BASE/vms/$VM/ssh-key" -H "Authorization: Bearer $OAT" || ko "status after deletion"
[ "$(jq -r '.key' "$B")" = null ] && ok "status reports no key after deletion" || ko "status still reports a key"

# --- per-VM gateway block → deny, with its own audit reason ---
echo "== per-VM gateway block → deny =="
pgx "update vms set ssh_gateway_blocked=true where id=$VM_DB"
sleep 1
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "blocked VM still reachable" || { sleep 1; [ "$(denied SSHGW_VM_BLOCKED)" -ge 1 ] 2>/dev/null && ok "per-VM block denies SSH (SSHGW_VM_BLOCKED)" || ko "no SSHGW_VM_BLOCKED audit"; }
pgx "update vms set ssh_gateway_blocked=false where id=$VM_DB"

# --- unknown slug → deny (unique per run so the audit query is unambiguous) ---
echo "== unknown slug → deny =="
BADSLUG="no-such-slug-${TS}"
pssh x "$BADSLUG" 'echo X' | grep -q '^X$' && ko "unknown slug routed somewhere" || { sleep 1; DEN=$(pgq "select count(*) from audit_logs where action='sshgw.route_denied' and detail->>'slug'='$BADSLUG' and detail->>'reason'='SSHGW_ROUTE_NOT_FOUND'"); [ "${DEN:-0}" -ge 1 ] 2>/dev/null && ok "unknown slug denied (SSHGW_ROUTE_NOT_FOUND)" || ko "no SSHGW_ROUTE_NOT_FOUND audit for $BADSLUG"; }

# --- 14. global kill switch → deny (checked first, reveals nothing) ---
echo "== [14] global kill switch → deny =="
ORIG_KILL=$(pgq "select value from settings where key='ssh_gateway_enabled'")
pgx "update settings set value='false'::jsonb where key='ssh_gateway_enabled'"
sleep 1
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "kill switch off but still reachable" || { sleep 1; [ "$(denied SSHGW_GATEWAY_DISABLED)" -ge 1 ] 2>/dev/null && ok "kill switch denies SSH (SSHGW_GATEWAY_DISABLED)" || ko "no SSHGW_GATEWAY_DISABLED audit"; }
pgx "update settings set value='$ORIG_KILL'::jsonb where key='ssh_gateway_enabled'"; ORIG_KILL=""

# --- cleanup: force-delete the VM ---
echo "== cleanup: force-delete VM =="
# The administrator answers a 2FA challenge, so the token cannot be read off the
# login response any more.
if XAT=$(login_token "$BASE" "$SYSADMIN_EMAIL" "$SYSADMIN_PW"); then ok "sysadmin login"; else ko "sysadmin login"; XAT=""; fi
req "force-delete" 202 -X POST "$BASE/admin/vms/$VM/force-delete" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\",\"reason\":\"ssh gateway e2e\"}" || true
DL=$((SECONDS+180)); DC=""; DST=""
while :; do
  DC=$(curl -sS -o "$B" -w '%{http_code}' "$BASE/vms/$VM" -H "Authorization: Bearer $XAT" 2>/dev/null)
  DST=$(jq -r '.status // empty' "$B" 2>/dev/null)
  { [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && break
  [ "$SECONDS" -ge "$DL" ] && break; sleep 10; done
{ [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && { ok "VM deleted (http=$DC${DST:+ status=$DST})"; VM_DELETED=1; } || ko "VM not deleted in 180s (http=$DC status=$DST)"

echo; echo "SSH-GATEWAY E2E: $P passed / $((P+F)) checks"
[ "$F" -eq 0 ] && exit 0 || exit 1
