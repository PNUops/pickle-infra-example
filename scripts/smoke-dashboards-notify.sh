#!/bin/bash
# shellcheck disable=SC2015,SC2016  # ok/ko idiom safe; $x in single quotes are jq --arg vars, not shell
# Dashboards-notifications full-journey e2e — run on pve-node as root. Provisions a real dev VM, then walks
# the surface: in-app notifications + mail delivery (dispatcher; read back from
# the notifications table, since dev sends real mail),
# announcements (workspace scope + delivery log), expiry pipeline (DB-forced end_date →
# JobRunr `vm-expiry` trigger via the dashboard API → auto-stop → VM_EXPIRED →
# admin period extension → restart), settings editor, ops registries (tasks /
# drift / ip / summaries), audit views, and a conditional failed-task retry.
# Force-deletes the VM at the end (teardown always runs once the VM exists).
#
# No announcement leaves this run's own workspace. dev sends real mail, and an
# ALL or ORG announcement would mail every active account or every member of
# the seed organisation, so both announcements posted here are WORKSPACE-scoped
# to the workspace this run creates, whose only member is its scratch user. The
# one ALL request left is the ORG_ADMIN one, refused with 403 before any row is
# written.
#
# Requires: curl, jq, python3 with bcrypt (for the scratch user's password hash),
# pct (CTID 101 = pickle-api + pickle_dev + JobRunr dash :8000).
# The JobRunr dashboard trigger endpoint (POST /api/recurring-jobs/{id}/trigger)
# is the only non-pickle API this script depends on.
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
CTID="${CTID:-101}"
# The id every "this does not exist" case is asked for. Public ids are UUIDs, so
# a made-up decimal would fail on the shape (400) before reaching the lookup.
NO_SUCH_ID="00000000-0000-0000-0000-000000000000"
DASH="http://198.18.1.20:8000"
TS=$(date +%s)-$RANDOM
# e2e account rules: email domain @example.com; the personal-group slug is the
# email local part, so the team-group slug must differ from it (dashteam- vs dash-).
EM="dash-${TS}@example.com"; PW="dash-pass-${TS}!"
ATITLE="e2e sys-notice ${TS}"; OTITLE="e2e org-admin-notice ${TS}"

for cmd in curl jq pct; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "missing required command: $cmd (run on pve-node as root)"; exit 2; }
done

seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
# shellcheck source=scripts/lib/auth.sh
. "$(dirname "$0")/lib/auth.sh"
pgq(){ pct exec "$CTID" -- su - postgres -c "psql -q -d pickle_dev -tAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
# mk_verified_user (lib/auth.sh) writes the scratch user through pgq and this.
# pgx feeds the statement on stdin: `su -c` re-parses its command string, and a
# statement passed there loses anything that shell expands.
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
JRU="$(seed_env PICKLE_JOBRUNR_DASH_USER)"; JRP="$(seed_env PICKLE_JOBRUNR_DASH_PASS)"
if [ -z "$ORGADMIN_PW" ] || [ -z "$SYSADMIN_PW" ] || [ -z "$JRU" ] || [ -z "$JRP" ]; then
  echo "FATAL: seed admin / JobRunr dashboard credentials not found in CTID $CTID api.env"; exit 2
fi

B=$(mktemp)
# What the run changed outside its own rows, put back on every exit. The
# settings phase writes vm_expiry_notice_days; SETTING_SAVED says whether the
# previous value was read, and SETTING_ORIG holds it ('' = there was no row).
SETTING_SAVED=0; SETTING_ORIG=""
on_exit(){
  local rc=$? cleanup_failed=""
  if [ "$SETTING_SAVED" = 1 ]; then
    if [ -n "$SETTING_ORIG" ]; then
      pgx "update settings set value = '$SETTING_ORIG'::jsonb where key = 'vm_expiry_notice_days'" \
        && echo "-- cleanup: vm_expiry_notice_days restored to $SETTING_ORIG --" \
        || { echo "-- cleanup: vm_expiry_notice_days NOT restored (was $SETTING_ORIG) --" >&2; rc=1; cleanup_failed+=" vm_expiry_notice_days not restored (was $SETTING_ORIG);"; }
    else
      pgx "delete from settings where key = 'vm_expiry_notice_days'" \
        && echo "-- cleanup: vm_expiry_notice_days row removed again (there was none) --" \
        || { echo "-- cleanup: vm_expiry_notice_days row NOT removed --" >&2; rc=1; cleanup_failed+=" vm_expiry_notice_days row not removed;"; }
    fi
  fi
  # After the teardown, which force-deletes the VM with the administrator's
  # token: the scratch user is closed last.
  if disable_scratch_user "$EM"; then
    echo "-- cleanup: scratch user $EM disabled --"
  else
    echo "-- cleanup: scratch user $EM NOT disabled --" >&2; rc=1
    cleanup_failed+=" scratch user $EM not disabled;"
  fi
  rm -f "$B"
  # The summary is printed before this trap runs, so a cleanup failure would sit
  # above it and scroll past; it is repeated as the very last line instead.
  [ -z "$cleanup_failed" ] || echo "CLEANUP FAILED:$cleanup_failed"
  exit "$rc"
}
trap on_exit EXIT
P=0; F=0; S=0; N=0
ok(){ N=$((N+1)); echo "PASS  [$N] $1"; P=$((P+1)); }
ko(){ N=$((N+1)); echo "FAIL  [$N] $1"; F=$((F+1)); }
skip(){ N=$((N+1)); echo "SKIP  [$N] $1"; S=$((S+1)); }
req(){ local n="$1" e="$2"; shift 2; local c; c=$(curl -sS -o "$B" -w '%{http_code}' "$@"); [ "$c" = "$e" ] && { ok "$n ($c)"; return 0; } || { ko "$n (want $e got $c)"; head -c 300 "$B"; echo; return 1; }; }
# jqc <check-name> <jq -e program> [--arg k v ...] — asserts on the last response body
jqc(){ local n="$1" prog="$2"; shift 2; jq -e "$@" "$prog" "$B" >/dev/null 2>&1 && ok "$n" || ko "$n ($(head -c 200 "$B" | tr '\n' ' '))"; }
# jr <recurring-job-id> — trigger a JobRunr recurring job via the dashboard API; echoes http code
jr(){ curl -sS -o /dev/null -w '%{http_code}' -u "$JRU:$JRP" -X POST "$DASH/api/recurring-jobs/$1/trigger" 2>/dev/null; }
# sent_count <sql condition on n> — this run's user's notifications rows matching
# the condition that the dispatcher marked SENT. The dispatcher writes SENT (and
# sent_at) only after the mail sender returned without error; a failed send
# leaves the row PENDING for a retry or parks it FAILED. dev runs the production
# mail profile, so the mock-mail spool these checks used to grep stays empty and
# this row is the delivery evidence left to read.
sent_count(){ pgq "select count(*) from notifications n join users u on u.id = n.user_id where u.email = '$EM' and n.status = 'SENT' and n.sent_at is not null and $1"; }
# sent_states <sql condition on n> — the status of every matching row, SENT or
# not, so a failure says whether the row was missing, still waiting, or failed.
sent_states(){ pgq "select coalesce(string_agg(n.status::text, ','), 'none') from notifications n join users u on u.id = n.user_id where u.email = '$EM' and $1"; }
# nudge_dispatcher — trigger the dispatcher and say so when the trigger itself was
# refused. Throwing the code away made a 401 from the dashboard (wrong or rotated
# credentials) look exactly like a successful trigger, and the only symptom was a
# delivery poll below timing out for no stated reason.
nudge_dispatcher(){
  local c; c=$(jr notification-dispatcher)
  case "$c" in
    200|204) return 0 ;;
    *) echo "  WARN notification-dispatcher trigger returned ${c:-none} (expected 200/204) — the job was NOT nudged" >&2; return 1 ;;
  esac
}
# poll_sent <sql condition on n> <timeout-s> — dispatcher runs every 1min; we also nudge it
poll_sent(){ local dl=$((SECONDS+$2)); nudge_dispatcher || true
  while :; do [ "$(sent_count "$1")" -ge 1 ] 2>/dev/null && return 0
    [ "$SECONDS" -ge "$dl" ] && return 1; sleep 5; done; }
# tokens expire in 15min and the run spans several long polls — refresh all three
login(){ curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" && jq -r '.accessToken // empty' "$B"; }
fresh_tokens(){
  # The first two are org tier and below, where enforcement does not reach, so
  # they keep the direct login. Only the administrator has a challenge to answer.
  SAT=$(login "$EM" "$PW"); AAT=$(login "$ORGADMIN_EMAIL" "$ORGADMIN_PW")
  XAT=$(login_token "$BASE" "$SYSADMIN_EMAIL" "$SYSADMIN_PW") || XAT=""
  { [ -n "$SAT" ] && [ -n "$AAT" ] && [ -n "$XAT" ]; } || { ko "token refresh (user/orgadmin/sysadmin login)"; return 1; }
}

SAT=""; AAT=""; XAT=""; GID=""; OID=""; RID=""; VM=""; VM_DB=""; VNAME=""; VIP=""

# ── 1. setup: user account, request, approval ──
phase_setup(){
  echo "== setup: user + request + approve =="
  # The user is written straight into the database rather than signed up.
  # Signup hands out its verification token only by mail, and dev runs the
  # production mail profile: the token goes to a real mailbox and the mock-mail
  # spool this phase used to read stays empty, so every run stopped here.
  local made
  if ! made=$(mk_verified_user "$BASE" "$EM" "$PW" "Dash e2e"); then
    ko "scratch user created and signed in"
    return 1
  fi
  SAT=${made%% *}
  ok "scratch user created and signed in"
  req "create workspace" 201 -X POST "$BASE/workspaces" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"name\":\"dash e2e\",\"kind\":\"PROJECT\"}" || return 1
  GID=$(jq -r .id "$B")
  AAT=$(login "$ORGADMIN_EMAIL" "$ORGADMIN_PW")
  { [ -n "$AAT" ] && ok "orgadmin login (org lookup)"; } || { ko "orgadmin login (org lookup)"; return 1; }
  # the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
  req "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $AAT" || return 1
  # The organisation is the seeded test one by name (lib/auth.sh smoke_org_id),
  # never "the first in the list": its administrators are who the request mails.
  if OID=$(smoke_org_id); then ok "request org = seeded test org ($OID)"; else ko "seeded test org not found"; return 1; fi
  req "os-images" 200 "$BASE/os-images" -H "Authorization: Bearer $SAT" || return 1
  TID=$(jq -r '.[0].id // empty' "$B")
  if [ -z "$TID" ]; then
    ko "no ACTIVE OS image to request with"
    return 1
  fi
  # os-images carry only the OS + disk floor; the spec axis is vm-flavors and
  # POST /requests requires the chosen flavorId ('basic', else first ACTIVE).
  req "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $SAT" || return 1
  local sel='(map(select(.name=="basic"))[0] // .[0])'
  FID=$(jq -r "$sel.id // empty" "$B"); VC=$(jq -r "$sel.vcpu // empty" "$B"); MM=$(jq -r "$sel.memoryMb // empty" "$B"); DG=$(jq -r "$sel.diskGb // empty" "$B")
  { [ -n "$FID" ] && ok "flavor id=$FID (${VC}c/${MM}MB/${DG}GB)"; } || { ko "no ACTIVE vm-flavor"; return 1; }
  # Every id the API speaks is a UUID, so each one goes into the payload quoted;
  # only the spec numbers (vcpu/memory/disk) stay bare.
  req "vm-request" 201 -X POST "$BASE/requests" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"type\":\"VM\",\"displayName\":\"대시보드 e2e $TS\",\"workspaceId\":\"$GID\",\"orgId\":\"$OID\",\"purpose\":\"dashboards e2e\",\"courseOrProject\":null,\"extraNote\":null,\"reqStartDate\":null,\"reqEndDate\":null,\"reqIndefinite\":true,\"vm\":{\"imageId\":\"$TID\",\"flavorId\":\"$FID\",\"reqVcpu\":$VC,\"reqMemoryMb\":$MM,\"reqDiskGb\":$DG,\"specReason\":null}}" || return 1
  RID=$(jq -r .id "$B")
  # AAT from the org-lookup login above is seconds old — reuse it
  # submission notification is created synchronously with the request
  curl -sS -o "$B" "$BASE/notifications?size=50" -H "Authorization: Bearer $AAT"
  jqc "orgadmin inbox has 신청 접수 (request.submitted → /admin/requests/$RID)" \
    '[.content[] | select(.event=="request.submitted" and .linkPath==$l)] | length >= 1' --arg l "/admin/requests/$RID"
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $AAT"
  jqc "orgadmin unread-count >= 1" '.unreadCount >= 1'
  req "approve" 200 -X POST "$BASE/admin/requests/$RID/approve" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"grantedStartDate\":null,\"grantedEndDate\":null,\"comment\":\"dash e2e\",\"vm\":{\"grantedVcpu\":$VC,\"grantedMemoryMb\":$MM,\"grantedDiskGb\":$DG,\"grantedImageId\":\"$TID\",\"nodeId\":null}}" || return 1
  req "vm list" 200 "$BASE/vms?workspaceId=$GID" -H "Authorization: Bearer $SAT" || return 1
  VM=$(jq -r '.content[0].id // empty' "$B"); VNAME=$(jq -r '.content[0].name // empty' "$B")
  [ -n "$VM" ] && ok "vm id=$VM name=$VNAME" || { ko "vm id (empty list)"; return 1; }
  # Two ids for one VM, and they are not interchangeable: VM is the UUID the API
  # speaks (URLs, JSON assertions), VM_DB is the internal key the direct
  # statements below run on. Resolve it once, here, and fail loudly — an empty
  # value would only surface later as a bare `where id=` syntax error.
  VM_DB=$(pgq "select id from vms where public_id='$VM'")
  [ -n "$VM_DB" ] && ok "internal vm id resolved ($VM_DB)" || { ko "no vms row for public_id $VM"; return 1; }

  # The type-agnostic inventory must show the same VM the per-type list does:
  # the console dashboard and the workspace inventory read this one, so a
  # divergence here is a screen that disagrees with the VM list.
  req "resource inventory" 200 "$BASE/resources?workspaceId=$GID" \
    -H "Authorization: Bearer $SAT" || return 1
  local rid rtype
  rid=$(jq -r --arg id "$VM" '.content[] | select(.id == $id) | .id // empty' "$B")
  rtype=$(jq -r --arg id "$VM" '.content[] | select(.id == $id) | .type // empty' "$B")
  if [ "$rid" = "$VM" ] && [ "$rtype" = "VM" ]; then
    ok "resource inventory carries vm $VM as type VM"
  else
    ko "resource inventory missing vm $VM (got id='$rid' type='$rtype')"
    return 1
  fi
}

phase_provision(){
  echo "== poll RUNNING (<=15m) =="
  local dl=$((SECONDS+900)) st=""
  while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $SAT"; st=$(jq -r '.status // empty' "$B"); VIP=$(jq -r '.ipAddress // empty' "$B")
    [ "$st" = "RUNNING" ] && break
    { [ "$st" = "ERROR" ] || [ "$st" = "NEEDS_ADMIN" ]; } && { ko "provision parked in $st"; return 1; }
    [ "$SECONDS" -ge "$dl" ] && { ko "not RUNNING in 15m (last=$st)"; return 1; }; sleep 10; done
  ok "VM RUNNING ip=$VIP"
}

# ── 2. notifications: inbox, read state, mail delivery ──
phase_notifications(){
  echo "== notifications =="
  curl -sS -o "$B" "$BASE/notifications?size=50" -H "Authorization: Bearer $SAT"
  jqc "user inbox has 승인 알림 (request.approved → /console/requests/$RID)" \
    '[.content[] | select(.event=="request.approved" and .linkPath==$l)] | length >= 1' --arg l "/console/requests/$RID"
  local nid uc1 uc2
  nid=$(jq -r '[.content[] | select(.event=="request.approved")][0].id // empty' "$B")
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $SAT"; uc1=$(jq -r '.unreadCount // 0' "$B")
  req "mark-read (id=$nid)" 200 -X POST "$BASE/notifications/$nid/read" -H "Authorization: Bearer $SAT" \
    && jqc "mark-read sets readAt" '.readAt != null'
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $SAT"; uc2=$(jq -r '.unreadCount // 0' "$B")
  [ "$uc2" -lt "$uc1" ] 2>/dev/null && ok "unread-count dropped ($uc1 -> $uc2)" || ko "unread-count did not drop ($uc1 -> $uc2)"
  req "read-all" 200 -X POST "$BASE/notifications/read-all" -H "Authorization: Bearer $SAT"
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $SAT"
  jqc "unread-count zero after read-all" '.unreadCount == 0'
  # SENT means the mail server accepted the approval mail. It says nothing about
  # the mail's content, and nothing about delivery beyond that server.
  local approved="n.event = 'request.approved' and n.link_path = '/console/requests/$RID'"
  poll_sent "$approved" 120 \
    && ok "dispatcher sent 승인 메일 (notifications SENT, to=$EM)" \
    || ko "승인 메일 not SENT within 120s (to=$EM; rows: $(sent_states "$approved"))"
}

# ── 3. announcements ──
phase_announcements(){
  echo "== announcements =="
  # The administrator is 2FA-enrolled, so the login answers with a challenge and
  # a status assertion alone would read as a pass with no token behind it.
  if ! XAT=$(login_token "$BASE" "$SYSADMIN_EMAIL" "$SYSADMIN_PW"); then ko "sysadmin login"; return 1; fi
  ok "sysadmin login"
  # WORKSPACE scope, aimed at this run's workspace: the scratch user is its only
  # member, so exactly one person is told. That is checked against the database
  # before anything is posted, and recipientCount == 1 is asserted afterwards,
  # not >= 1, so a scope that ever widened fails here instead of mailing people.
  local reach
  reach=$(pgq "select count(*) from workspace_members m join workspaces w on w.id = m.workspace_id join users u on u.id = m.user_id where w.public_id = '$GID' and u.status = 'ACTIVE'")
  if [ "$reach" != 1 ]; then
    ko "announcement would reach ${reach:-unknown} active members of workspace $GID, not 1; nothing posted"
    return 1
  fi
  ok "announcement target workspace has exactly 1 active member"
  req "WORKSPACE announcement (sys)" 201 -X POST "$BASE/admin/announcements" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' \
    -d "{\"title\":\"$ATITLE\",\"body\":\"e2e 워크스페이스 공지 본문\",\"scope\":\"WORKSPACE\",\"orgId\":null,\"workspaceId\":\"$GID\"}" \
    && jqc "sys announcement recipientCount == 1" '.recipientCount == 1'
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $SAT"
  jqc "user unread-count bumped by the announcement" '.unreadCount >= 1'
  curl -sS -o "$B" "$BASE/notifications?size=50" -H "Authorization: Bearer $SAT"
  jqc "user inbox shows the announcement title" \
    '[.content[] | select(.event=="announcement" and .title==$t)] | length >= 1' --arg t "$ATITLE"
  # An org administrator may announce to a workspace that holds resources in
  # their organisation, which this one does while its VM exists.
  req "WORKSPACE announcement (orgadmin, own org's workspace)" 201 -X POST "$BASE/admin/announcements" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' \
    -d "{\"title\":\"$OTITLE\",\"body\":\"e2e 워크스페이스 공지 본문\",\"scope\":\"WORKSPACE\",\"orgId\":null,\"workspaceId\":\"$GID\"}" \
    && jqc "orgadmin announcement recipientCount == 1" '.recipientCount == 1'
  # The scope gate throws before the send budget is spent or a row is written.
  req "ALL by ORG_ADMIN -> 403" 403 -X POST "$BASE/admin/announcements" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' \
    -d "{\"title\":\"$ATITLE-forbidden\",\"body\":\"x\",\"scope\":\"ALL\",\"orgId\":null,\"workspaceId\":null}"
  # delivery log (sys): the dispatcher must mark this run's announcement mail SENT.
  # This reads the same notifications row a direct SQL check would, so it is the
  # only delivery check here; on failure the row's own status is printed.
  local dl=$((SECONDS+120)) sent=0
  nudge_dispatcher || true
  while :; do
    curl -sS -o "$B" "$BASE/admin/notifications?event=announcement&email=$EM&size=50" -H "Authorization: Bearer $XAT"
    sent=$(jq -r --arg t "$ATITLE" '[.content[] | select(.title==$t and .status=="SENT")] | length' "$B" 2>/dev/null)
    [ "${sent:-0}" -ge 1 ] 2>/dev/null && break; [ "$SECONDS" -ge "$dl" ] && break; sleep 5; done
  [ "${sent:-0}" -ge 1 ] 2>/dev/null && ok "delivery log SENT for workspace announcement (to=$EM)" \
    || ko "no SENT delivery-log row for workspace announcement within 120s (rows: $(sent_states "n.event = 'announcement' and n.title = '$ATITLE'"))"
}

# ── 4. expiry: DB-forced end_date → vm-expiry job → auto-stop → extend → restart ──
phase_expiry(){
  echo "== expiry pipeline =="
  local upd; upd=$(pgq "update vms set end_date=((now() at time zone 'Asia/Seoul')::date - 1), expiry_stopped_at=null, last_expiry_notice_stage=null where id=$VM_DB returning id")
  [ "$upd" = "$VM_DB" ] && ok "DB: end_date=yesterday(KST), expiry markers cleared" || { ko "DB end_date update (got '$upd')"; return 1; }
  local c; c=$(jr vm-expiry)
  case "$c" in 200|204) ok "JobRunr vm-expiry triggered via dashboard ($c)";; *) ko "vm-expiry dashboard trigger (got $c) — check POST $DASH/api/recurring-jobs/vm-expiry/trigger";; esac
  # auto-stop: ACPI with force-stop fallback inside the job; poll marker + status
  local dl=$((SECONDS+420)) st="" esa=""
  while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $SAT"
    st=$(jq -r '.status // empty' "$B"); esa=$(jq -r '.expiryStoppedAt // empty' "$B")
    [ "$st" = "STOPPED" ] && [ -n "$esa" ] && break
    [ "$SECONDS" -ge "$dl" ] && break; sleep 10; done
  { [ "$st" = "STOPPED" ] && [ -n "$esa" ]; } && ok "expiry auto-stop -> STOPPED, expiryStoppedAt=$esa" || { ko "expiry auto-stop (status=$st expiryStoppedAt=${esa:-null} after 420s)"; return 1; }
  curl -sS -o "$B" "$BASE/admin/vms?expired=true&size=100" -H "Authorization: Bearer $AAT"
  jqc "/admin/vms?expired=true contains the VM" '[.content[] | select(.id==$v)] | length == 1' --arg v "$VM"
  req "user start on expired VM -> 409" 409 -X POST "$BASE/vms/$VM/start" -H "Authorization: Bearer $SAT" \
    && jqc "409 code=VM_EXPIRED" '.code == "VM_EXPIRED"'
  curl -sS -o "$B" "$BASE/notifications?size=50" -H "Authorization: Bearer $SAT"
  jqc "user inbox has 만료 정지 알림 (vm.expiry.stopped, HIGH)" \
    '[.content[] | select(.event=="vm.expiry.stopped" and .importance=="HIGH" and .linkPath==$l)] | length >= 1' --arg l "/console/vms/$VM"
  local nd; nd=$(TZ='Asia/Seoul' date -d '+30 days' +%F)
  req "admin PATCH period endDate=$nd" 200 -X PATCH "$BASE/admin/vms/$VM/period" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"endDate\":\"$nd\"}" \
    && jqc "period update clears expiryStoppedAt" '.expiryStoppedAt == null and .endDate == $d' --arg d "$nd"
  req "user start after extension -> 202" 202 -X POST "$BASE/vms/$VM/start" -H "Authorization: Bearer $SAT" || return 0
  local dl2=$((SECONDS+300)) st2=""
  while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $SAT"; st2=$(jq -r '.status // empty' "$B")
    [ "$st2" = "RUNNING" ] && break; [ "$SECONDS" -ge "$dl2" ] && break; sleep 10; done
  [ "$st2" = "RUNNING" ] && ok "VM RUNNING again after extension" || ko "VM not RUNNING within 300s after restart (last=$st2)"
}

# ── 5. settings (sys) — never touches ssh_gateway_enabled ──
phase_settings(){
  echo "== settings =="
  req "settings list" 200 "$BASE/admin/settings" -H "Authorization: Bearer $XAT" \
    && jqc "vm_expiry_notice_days present, editable" '[.[] | select(.key=="vm_expiry_notice_days" and .editable==true)] | length == 1'
  # Read the current value first; on_exit puts it back whatever happens below.
  SETTING_ORIG=$(pgq "select value::text from settings where key = 'vm_expiry_notice_days'")
  local present; present=$(pgq "select count(*) from settings where key = 'vm_expiry_notice_days'")
  if [ "$present" = 1 ] && [ -n "$SETTING_ORIG" ]; then
    SETTING_SAVED=1; ok "vm_expiry_notice_days saved before the edit ($SETTING_ORIG)"
  elif [ "$present" = 0 ]; then
    SETTING_ORIG=""; SETTING_SAVED=1; ok "vm_expiry_notice_days has no row before the edit"
  else
    ko "could not read vm_expiry_notice_days before the edit; not editing it"; return 1
  fi
  req "PUT vm_expiry_notice_days [14,7,1]" 200 -X PUT "$BASE/admin/settings/vm_expiry_notice_days" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d '{"value":[14,7,1]}' \
    && jqc "round-trip value [14,7,1]" '.value == [14,7,1]'
  req "PUT unknown key -> 404" 404 -X PUT "$BASE/admin/settings/smoke_e2e_no_such_key" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d '{"value":1}'
  req "PUT invalid value [0] -> 422" 422 -X PUT "$BASE/admin/settings/vm_expiry_notice_days" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d '{"value":[0]}'
}

# ── 6. tasks / drift / ip / summaries ──
phase_ops(){
  echo "== ops registries + summaries =="
  req "GET /admin/tasks (sys)" 200 "$BASE/admin/tasks?size=5" -H "Authorization: Bearer $XAT" \
    && jqc "tasks page shape" '(.content | type == "array") and has("totalElements")'
  req "GET /admin/drift-findings (sys)" 200 "$BASE/admin/drift-findings?size=5" -H "Authorization: Bearer $XAT"
  req "GET /admin/ip-allocations (sys)" 200 "$BASE/admin/ip-allocations?status=ALLOCATED&size=100" -H "Authorization: Bearer $XAT" \
    && jqc "smoke VM ip $VIP allocated" '[.content[] | select(.ip==$ip and .vmId==$v)] | length == 1' --arg ip "$VIP" --arg v "$VM"
  req "GET /admin/summary (orgadmin)" 200 "$BASE/admin/summary" -H "Authorization: Bearer $AAT" \
    && jqc "summary has pendingRequestCount" 'has("pendingRequestCount")'
  req "GET /admin/system-summary (sys)" 200 "$BASE/admin/system-summary" -H "Authorization: Bearer $XAT" \
    && jqc "system-summary nodes non-empty" '.nodes | length >= 1'
  req "system-summary by ORG_ADMIN -> 403" 403 "$BASE/admin/system-summary" -H "Authorization: Bearer $AAT"
  req "tasks by ORG_ADMIN -> 403" 403 "$BASE/admin/tasks" -H "Authorization: Bearer $AAT"
}

# ── 7. audit ──
phase_audit(){
  echo "== audit =="
  req "GET /me/activity (user)" 200 "$BASE/me/activity?action=auth.login&size=20" -H "Authorization: Bearer $SAT" \
    && jqc "activity has auth.login row with ip" '[.content[] | select(.action=="auth.login" and .ip != null)] | length >= 1'
  req "GET /admin/audit (orgadmin)" 200 "$BASE/admin/audit?size=5" -H "Authorization: Bearer $AAT" \
    && jqc "audit rows present" '.content | length >= 1'
  # A well-formed id that belongs to nobody. It has to parse as a UUID or the
  # server answers 400 for the shape and the scoping rule is never consulted.
  req "audit foreign orgId by ORG_ADMIN -> 404" 404 "$BASE/admin/audit?orgId=$NO_SUCH_ID" -H "Authorization: Bearer $AAT"
}

# ── 8. failed-job recovery — conditional (never fabricates failures on dev) ──
phase_recovery(){
  echo "== failed-job recovery (conditional) =="
  curl -sS -o "$B" "$BASE/admin/tasks?status=NEEDS_ADMIN&size=1" -H "Authorization: Bearer $XAT"
  local na done_id
  na=$(jq -r '.content[0].taskId // empty' "$B" 2>/dev/null)
  if [ -n "$na" ]; then
    req "retry NEEDS_ADMIN task $na -> 202" 202 -X POST "$BASE/admin/tasks/$na/retry" -H "Authorization: Bearer $XAT"
  else
    skip "no NEEDS_ADMIN task on dev — positive retry path not exercisable (by design: failures are not fabricated)"
    curl -sS -o "$B" "$BASE/admin/tasks?status=DONE&size=1" -H "Authorization: Bearer $XAT"
    done_id=$(jq -r '.content[0].taskId // empty' "$B" 2>/dev/null)
    if [ -n "$done_id" ]; then
      req "retry DONE task $done_id -> 409" 409 -X POST "$BASE/admin/tasks/$done_id/retry" -H "Authorization: Bearer $XAT"
    else
      req "retry nonexistent task -> 404" 404 -X POST "$BASE/admin/tasks/$NO_SUCH_ID/retry" -H "Authorization: Bearer $XAT"
    fi
  fi
}

# ── 9. teardown — runs whenever a VM was created ──
phase_teardown(){
  echo "== teardown =="
  [ -z "$VM" ] && { echo "      no VM created; nothing to tear down"; return 0; }
  # This is the call that actually removes the guest, so an empty token here
  # leaves it running on the host.
  if ! XAT=$(login_token "$BASE" "$SYSADMIN_EMAIL" "$SYSADMIN_PW"); then
    ko "sysadmin login (teardown)"
    return 1
  fi
  req "force-delete" 202 -X POST "$BASE/admin/vms/$VM/force-delete" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\",\"reason\":\"dash e2e cleanup\"}" || return 1
  local dl=$((SECONDS+300)) dc="" dst=""
  while :; do
    dc=$(curl -sS -o "$B" -w '%{http_code}' "$BASE/vms/$VM" -H "Authorization: Bearer $XAT" 2>/dev/null)
    dst=$(jq -r '.status // empty' "$B" 2>/dev/null)
    { [ "$dc" = "404" ] || [ "$dst" = "DELETED" ]; } && break
    [ "$SECONDS" -ge "$dl" ] && break; sleep 10; done
  { [ "$dc" = "404" ] || [ "$dst" = "DELETED" ]; } && ok "VM deleted (http=$dc${dst:+ status=$dst})" || ko "VM not deleted in 300s (http=$dc status=$dst)"
}

echo "== Dashboards-notifications e2e against $BASE (CTID $CTID, run tag $TS) =="
if phase_setup; then
  if phase_provision; then
    fresh_tokens && {   # provisioning may have burned most of the 15-min token TTL
      phase_notifications
      phase_announcements
      phase_expiry
    }
  fi
  # independent of the VM lifecycle — run these even if provisioning failed
  fresh_tokens && {
    phase_settings
    phase_ops
    phase_audit
    phase_recovery
  }
fi
phase_teardown

echo; echo "DASHBOARDS-NOTIFY E2E: $P passed / $((P+F)) checks$([ "$S" -gt 0 ] && echo " ($S skipped)")"
[ "$F" -eq 0 ] && exit 0 || exit 1
