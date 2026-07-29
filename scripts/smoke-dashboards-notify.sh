#!/bin/bash
# shellcheck disable=SC2015,SC2016  # ok/ko idiom safe; $x in single quotes are jq --arg vars, not shell
# Dashboards-notifications full-journey e2e — run on pve-node as root. Provisions a real dev VM, then walks
# the surface: in-app notifications + mock-mail delivery (dispatcher),
# announcements (scopes + delivery log), expiry pipeline (DB-forced end_date →
# JobRunr `vm-expiry` trigger via the dashboard API → auto-stop → VM_EXPIRED →
# admin period extension → restart), settings editor, ops registries (tasks /
# drift / ip / summaries), audit views, and a conditional failed-task retry.
# Force-deletes the VM at the end (teardown always runs once the VM exists).
#
# Requires: curl, jq, pct (CTID 101 = pickle-api + pickle_dev + JobRunr dash :8000).
# The JobRunr dashboard trigger endpoint (POST /api/recurring-jobs/{id}/trigger)
# is the only non-pickle API this script depends on.
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

CTID="${CTID:-101}"
DASH="http://198.18.1.20:8000"
SPOOL=/var/lib/pickle/mock-mail.log
TS=$(date +%s)-$RANDOM
# e2e account rules: email domain @pusan.ac.kr; the personal-group slug is the
# email local part, so the team-group slug must differ from it (dashteam- vs dash-).
EM="dash-${TS}@pusan.ac.kr"; PW="dash-pass-${TS}!"
ATITLE="e2e all-notice ${TS}"; OTITLE="e2e org-notice ${TS}"

for cmd in curl jq pct; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "missing required command: $cmd (run on pve-node as root)"; exit 2; }
done

seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
pgq(){ pct exec "$CTID" -- su - postgres -c "psql -q -d pickle_dev -tAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
ORGADMIN_EMAIL="orgadmin@pickle.local"; ORGADMIN_PW="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"
SYSADMIN_EMAIL="admin@pickle.local"; SYSADMIN_PW="$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)"
JRU="$(seed_env PICKLE_JOBRUNR_DASH_USER)"; JRP="$(seed_env PICKLE_JOBRUNR_DASH_PASS)"
if [ -z "$ORGADMIN_PW" ] || [ -z "$SYSADMIN_PW" ] || [ -z "$JRU" ] || [ -z "$JRP" ]; then
  echo "FATAL: seed admin / JobRunr dashboard credentials not found in CTID $CTID api.env"; exit 2
fi

B=$(mktemp); trap 'rm -f "$B"' EXIT
P=0; F=0; S=0; N=0
ok(){ N=$((N+1)); echo "PASS  [$N] $1"; P=$((P+1)); }
ko(){ N=$((N+1)); echo "FAIL  [$N] $1"; F=$((F+1)); }
skip(){ N=$((N+1)); echo "SKIP  [$N] $1"; S=$((S+1)); }
req(){ local n="$1" e="$2"; shift 2; local c; c=$(curl -sS -o "$B" -w '%{http_code}' "$@"); [ "$c" = "$e" ] && { ok "$n ($c)"; return 0; } || { ko "$n (want $e got $c)"; head -c 300 "$B"; echo; return 1; }; }
# jqc <check-name> <jq -e program> [--arg k v ...] — asserts on the last response body
jqc(){ local n="$1" prog="$2"; shift 2; jq -e "$@" "$prog" "$B" >/dev/null 2>&1 && ok "$n" || ko "$n ($(head -c 200 "$B" | tr '\n' ' '))"; }
# jr <recurring-job-id> — trigger a JobRunr recurring job via the dashboard API; echoes http code
jr(){ curl -sS -o /dev/null -w '%{http_code}' -u "$JRU:$JRP" -X POST "$DASH/api/recurring-jobs/$1/trigger" 2>/dev/null; }
mail_count(){ pct exec "$CTID" -- sh -c "grep -cF -- '$1' $SPOOL 2>/dev/null" | tr -d '[:space:]'; }
# nudge_dispatcher — trigger the dispatcher and say so when the trigger itself was
# refused. Throwing the code away made a 401 from the dashboard (wrong or rotated
# credentials) look exactly like a successful trigger, and the only symptom was a
# mail poll below timing out for no stated reason.
nudge_dispatcher(){
  local c; c=$(jr notification-dispatcher)
  case "$c" in
    200|204) return 0 ;;
    *) echo "  WARN notification-dispatcher trigger returned ${c:-none} (expected 200/204) — the job was NOT nudged" >&2; return 1 ;;
  esac
}
# poll_mail <fixed-string> <timeout-s> — dispatcher runs every 1min; we also nudge it
poll_mail(){ local dl=$((SECONDS+$2)); nudge_dispatcher || true
  while :; do [ "$(mail_count "$1")" -ge 1 ] 2>/dev/null && return 0
    [ "$SECONDS" -ge "$dl" ] && return 1; sleep 5; done; }
# tokens expire in 15min and the run spans several long polls — refresh all three
login(){ curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" && jq -r '.accessToken // empty' "$B"; }
fresh_tokens(){
  SAT=$(login "$EM" "$PW"); AAT=$(login "$ORGADMIN_EMAIL" "$ORGADMIN_PW"); XAT=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
  { [ -n "$SAT" ] && [ -n "$AAT" ] && [ -n "$XAT" ]; } || { ko "token refresh (user/orgadmin/sysadmin login)"; return 1; }
}

SAT=""; AAT=""; XAT=""; GID=""; OID=""; RID=""; VM=""; VNAME=""; VIP=""

# ── 1. setup: user account, request, approval ──
phase_setup(){
  echo "== setup: user + request + approve =="
  req "signup" 202 -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' -d "{\"email\":\"$EM\",\"password\":\"$PW\",\"name\":\"Dash e2e\",\"consents\":$CONSENTS_JSON}" || return 1
  sleep 2
  local tok; tok=$(pct exec "$CTID" -- sh -c "grep -o 'token=[A-Za-z0-9_-]*' $SPOOL 2>/dev/null | tail -1 | cut -d= -f2")
  req "verify-email" 200 -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' -d "{\"token\":\"$tok\"}" || return 1
  req "user login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$EM\",\"password\":\"$PW\"}" || return 1
  SAT=$(jq -r .accessToken "$B")
  req "create group" 201 -X POST "$BASE/groups" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"name\":\"dash e2e\",\"slug\":\"dashteam-${TS}\",\"kind\":\"TEAM\"}" || return 1
  GID=$(jq -r .id "$B")
  AAT=$(login "$ORGADMIN_EMAIL" "$ORGADMIN_PW")
  { [ -n "$AAT" ] && ok "orgadmin login (org lookup)"; } || { ko "orgadmin login (org lookup)"; return 1; }
  # the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
  req "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $AAT" || return 1; OID=$(jq -r '.[0].id' "$B")
  req "templates" 200 "$BASE/templates" -H "Authorization: Bearer $SAT" || return 1
  TID=$(jq -r '.[0].id // empty' "$B")
  if [ -z "$TID" ]; then
    ko "no ACTIVE OS image to request with"
    return 1
  fi
  # templates carry only the OS + disk floor now; the spec axis is vm-flavors and
  # POST /vm-requests requires the chosen flavorId ('basic', else first ACTIVE).
  req "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $SAT" || return 1
  local sel='(map(select(.name=="basic"))[0] // .[0])'
  FID=$(jq -r "$sel.id // empty" "$B"); VC=$(jq -r "$sel.vcpu // empty" "$B"); MM=$(jq -r "$sel.memoryMb // empty" "$B"); DG=$(jq -r "$sel.diskGb // empty" "$B")
  { [ -n "$FID" ] && ok "flavor id=$FID (${VC}c/${MM}MB/${DG}GB)"; } || { ko "no ACTIVE vm-flavor"; return 1; }
  req "vm-request" 201 -X POST "$BASE/vm-requests" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"groupId\":$GID,\"orgId\":$OID,\"templateId\":$TID,\"flavorId\":$FID,\"purpose\":\"dashboards e2e\",\"courseOrProject\":null,\"specReason\":null,\"extraNote\":null,\"reqVcpu\":$VC,\"reqMemoryMb\":$MM,\"reqDiskGb\":$DG,\"reqStartDate\":null,\"reqEndDate\":null,\"desiredSubdomain\":null,\"rootDomain\":null}" || return 1
  RID=$(jq -r .id "$B")
  # AAT from the org-lookup login above is seconds old — reuse it
  # submission notification is created synchronously with the request
  curl -sS -o "$B" "$BASE/notifications?size=50" -H "Authorization: Bearer $AAT"
  jqc "orgadmin inbox has 신청 접수 (request.submitted → /admin/requests/$RID)" \
    '[.content[] | select(.event=="request.submitted" and .linkPath==$l)] | length >= 1' --arg l "/admin/requests/$RID"
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $AAT"
  jqc "orgadmin unread-count >= 1" '.unreadCount >= 1'
  req "approve" 200 -X POST "$BASE/admin/vm-requests/$RID/approve" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"grantedVcpu\":$VC,\"grantedMemoryMb\":$MM,\"grantedDiskGb\":$DG,\"grantedTemplateId\":$TID,\"grantedStartDate\":null,\"grantedEndDate\":null,\"nodeId\":null,\"comment\":\"dash e2e\"}" || return 1
  req "vm list" 200 "$BASE/vms?groupId=$GID" -H "Authorization: Bearer $SAT" || return 1
  VM=$(jq -r '.content[0].id // empty' "$B"); VNAME=$(jq -r '.content[0].name // empty' "$B")
  [ -n "$VM" ] && ok "vm id=$VM name=$VNAME" || { ko "vm id (empty list)"; return 1; }
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
  poll_mail "to=$EM subject=[Pickle] VM 신청 승인" 120 \
    && ok "mock-mail spool has 승인 메일 (to=$EM)" || ko "no 승인 메일 in spool within 120s (to=$EM)"
}

# ── 3. announcements ──
phase_announcements(){
  echo "== announcements =="
  req "sysadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$SYSADMIN_EMAIL\",\"password\":\"$SYSADMIN_PW\"}" || return 1
  XAT=$(jq -r .accessToken "$B")
  req "ALL announcement (sys)" 201 -X POST "$BASE/admin/announcements" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' \
    -d "{\"title\":\"$ATITLE\",\"body\":\"e2e 전체 공지 본문\",\"scope\":\"ALL\",\"orgId\":null,\"groupId\":null}" \
    && jqc "ALL recipientCount >= 1" '.recipientCount >= 1'
  curl -sS -o "$B" "$BASE/notifications/unread-count" -H "Authorization: Bearer $SAT"
  jqc "user unread-count bumped by ALL announcement" '.unreadCount >= 1'
  curl -sS -o "$B" "$BASE/notifications?size=50" -H "Authorization: Bearer $SAT"
  jqc "user inbox shows ALL announcement title" \
    '[.content[] | select(.event=="announcement" and .title==$t)] | length >= 1' --arg t "$ATITLE"
  req "ORG announcement (orgadmin, own org)" 201 -X POST "$BASE/admin/announcements" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' \
    -d "{\"title\":\"$OTITLE\",\"body\":\"e2e 기관 공지 본문\",\"scope\":\"ORG\",\"orgId\":null,\"groupId\":null}"
  req "ALL by ORG_ADMIN -> 403" 403 -X POST "$BASE/admin/announcements" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' \
    -d "{\"title\":\"$ATITLE-forbidden\",\"body\":\"x\",\"scope\":\"ALL\",\"orgId\":null,\"groupId\":null}"
  # delivery log (sys): the dispatcher must mark this run's announcement mail SENT
  local dl=$((SECONDS+120)) sent=0
  nudge_dispatcher || true
  while :; do
    curl -sS -o "$B" "$BASE/admin/notifications?event=announcement&email=$EM&size=50" -H "Authorization: Bearer $XAT"
    sent=$(jq -r --arg t "$ATITLE" '[.content[] | select(.title==$t and .status=="SENT")] | length' "$B" 2>/dev/null)
    [ "${sent:-0}" -ge 1 ] 2>/dev/null && break; [ "$SECONDS" -ge "$dl" ] && break; sleep 5; done
  [ "${sent:-0}" -ge 1 ] 2>/dev/null && ok "delivery log SENT for ALL announcement (to=$EM)" || ko "no SENT delivery-log row for ALL announcement within 120s"
  poll_mail "to=$EM subject=[Pickle] $ATITLE" 90 \
    && ok "mock-mail spool has announcement mail" || ko "no announcement mail in spool within 90s"
}

# ── 4. expiry: DB-forced end_date → vm-expiry job → auto-stop → extend → restart ──
phase_expiry(){
  echo "== expiry pipeline =="
  local upd; upd=$(pgq "update vms set end_date=((now() at time zone 'Asia/Seoul')::date - 1), expiry_stopped_at=null, last_expiry_notice_stage=null where id=$VM returning id")
  [ "$upd" = "$VM" ] && ok "DB: end_date=yesterday(KST), expiry markers cleared" || { ko "DB end_date update (got '$upd')"; return 1; }
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
  jqc "/admin/vms?expired=true contains the VM" '[.content[] | select(.id==($v|tonumber))] | length == 1' --arg v "$VM"
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
    && jqc "smoke VM ip $VIP allocated" '[.content[] | select(.ip==$ip and .vmId==($v|tonumber))] | length == 1' --arg ip "$VIP" --arg v "$VM"
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
  req "audit foreign orgId by ORG_ADMIN -> 404" 404 "$BASE/admin/audit?orgId=999999" -H "Authorization: Bearer $AAT"
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
      req "retry nonexistent task -> 404" 404 -X POST "$BASE/admin/tasks/999999999/retry" -H "Authorization: Bearer $XAT"
    fi
  fi
}

# ── 9. teardown — runs whenever a VM was created ──
phase_teardown(){
  echo "== teardown =="
  [ -z "$VM" ] && { echo "      no VM created; nothing to tear down"; return 0; }
  XAT=$(login "$SYSADMIN_EMAIL" "$SYSADMIN_PW")
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
