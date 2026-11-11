#!/usr/bin/env bash
# Puts the platform's runtime settings rows into the application database.
#
# Why this is a script and not a migration: a settings row is a value the
# operator owns and changes from the admin console. A migration that seeds one
# states a fact that stops being true the moment anybody edits it, and the
# database silently keeps the edit while the migration keeps claiming otherwise.
# That is not hypothetical here — every seeded value that mattered had drifted
# by the time this script was written:
#
#   allowed_root_domains  seeded with one root domain, live carried another,
#                         and the real one was a third
#   ssh_gateway_enabled   seeded false, live true
#   web_terminal_enabled  seeded false, live true
#   reserved_subdomains   seeded with 7 entries, live carried 416
#   profanity_subdomains  seeded with 6 entries, live carried 43
#
# The repair mechanism made it worse rather than better: correcting a seeded
# value means another migration, so one setting that no longer exists at all
# accumulated four (seed, seed again, adjust, drop). Rows belong here, next to
# the deployment they describe; the schema side (the table, its primary key,
# its constraints) stays in the migrations.
#
# Ownership, and what a re-run does:
#   value        the OPERATOR owns it. A key that already has a row is never
#                overwritten, so a threshold tuned in the admin console or a
#                kill switch flipped during an incident survives every re-run.
#                Only missing keys are inserted.
#   description  the RELEASE owns it. This is the help text the admin console
#                shows beside the field, so a re-run refreshes it in place —
#                otherwise a deployment that bootstrapped months ago keeps
#                explaining behaviour the code no longer has.
#
# Everything is written in one transaction, so a failure part way leaves the
# database as it was.
#
# Usage:
#   PICKLE_CONTACT_EMAIL=<address> bash scripts/apply-settings.sh
#
# Environment (required first, then defaults for this environment):
#   PICKLE_CONTACT_EMAIL   REQUIRED. Operations contact shown in the console
#                          footer and on the maintenance and error screens, and
#                          named as the contact point by both legal documents.
#                          Never defaulted: a wrong address here is worse than
#                          none, and an address does not belong in a repository.
#                          Left unset the script asks for it at the terminal, and
#                          refuses only when there is no terminal to ask at, so
#                          an unattended run still cannot invent one. Answer
#                          `none` to publish it empty on purpose (the console
#                          then shows no contact).
#   PICKLE_APP_CTID        101      container running PostgreSQL + the api
#   PICKLE_DB              pickle_dev
#   PICKLE_ROOT_DOMAIN     pusan.dev   root domain offered in the request form.
#                          Same variable the inventory script reads, so the two
#                          cannot disagree about which domain this deployment
#                          publishes under.
#   PICKLE_DATA_DIR        <repo>/data   holds the curated word lists
set -euo pipefail

CTID="${PICKLE_APP_CTID:-101}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
DB="${PICKLE_DB:-pickle_dev}"
ROOT_DOMAIN="${PICKLE_ROOT_DOMAIN:-pusan.dev}"
CONTACT_EMAIL="${PICKLE_CONTACT_EMAIL:-}"
DATA_DIR="${PICKLE_DATA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data}"

# The database name is the one value that rides INSIDE the command string the
# second shell below re-parses, so it is pinned to bare-word characters here.
# This also turns a plain typo ("pickle dev") into a readable refusal instead
# of an incomprehensible psql failure.
case "$DB" in
  ''|*[!a-zA-Z0-9_]*)
    echo "PICKLE_DB must be a plain database name ([A-Za-z0-9_]), got: '$DB'" >&2
    exit 1 ;;
esac

# ── database access ──────────────────────────────────────────────────────────
# Statements are fed on STDIN, never as `psql -c "…"`. A -c argument travels
# through the second shell `su -c` spawns, and that parse expands $ and eats
# quotes: the word lists below are JSON arrays full of double quotes and would
# terminate the argument early. Nothing re-parses stdin; the database name is
# the only piece spliced into that command string, and it is validated to
# bare-word characters above.
pgq() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'query failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
pgtx() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'write failed, nothing committed:\n%s\n' "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
pgshow() {
  pct exec "$CTID" -- su - postgres -c \
    "psql -q -X -v ON_ERROR_STOP=1 -d $DB -f -" <<<"$1"
}
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

# ── defaults the release owns ────────────────────────────────────────────────
# key <TAB> default value (jsonb literal) <TAB> admin-console help text.
# Kill switches default OFF: a fresh deployment has not yet proven the gateway,
# the web terminal or the relay works, and a switch that defaults on would offer
# users a path that dead-ends.
DEFAULTS=(
  "gpu_unattached_review_hours	12	GPU를 VM에 연결하지 않은 상태가 이 시간 이상 지속되면 검토 대상으로 올려요(시간). 자동으로 회수하지 않아요."
  "gpu_low_util_window_hours	12	GPU 저사용 검토에 쓸 최근 이용률 기간이에요(시간). 유효한 표본이 부족하면 저사용으로 판단하지 않아요."
  "gpu_low_util_threshold_percent	5	판단 기간의 평균 GPU 이용률이 이 값 미만이면 검토 대상으로 올려요(%). 실제 회수는 관리자가 결정해요."
  "gpu_low_util_snooze_hours	12	관리자가 GPU 유지를 결정한 뒤 다시 검토할 때까지 기다리는 시간이에요(시간)."
  "gpu_lease_notice_hours	[24,1]	GPU 임대 종료를 미리 알릴 시점이에요(시간 전). 승인한 임대 기간보다 짧은 단계만 적용하며 만료 시각은 바꾸지 않아요."
  "banner_message	\"\"	점검 모드와 별개인 콘솔 상단 공지 배너예요. 비우면 배너를 표시하지 않아요."
  "bulk_provision_concurrency	4	여러 명 몫으로 승인된 VM 신청의 노드별 동시 생성 수예요(1~8). 상한에 닿은 노드는 건너뛰며 일반 노드가 모두 상한이면 GPU 노드도 사용할 수 있어요. 한 명 몫 신청으로 만드는 VM은 세지 않아요."
  "custom_domain_limit_per_hour	20	사용자별 커스텀 도메인 연결 요청의 시간당 상한이에요. 플랫폼 서브도메인 연결은 이 제한에 포함하지 않아요."
  "ip_quarantine_hours	24	회수한 IP를 다시 할당하지 않는 격리 시간이에요(시간). 릴레이 스냅샷의 부팅 시 재적용 기간인 24시간보다 짧게 설정할 수 없어요."
  "llm_usage_retention_days	0	LLM 원본 사용량 이벤트 보관 기간이에요(일). 0은 무기한이고, 기간을 정할 때는 90~3650일로 입력해요. 일별 집계는 남지만 원본이 필요한 지연과 시간대 통계의 조회 기간은 줄어들어요. 삭제된 원본은 기간을 늘려도 돌아오지 않아요."
  "maintenance_message	\"\"	점검 모드 안내예요. 비우면 기본 안내를 사용해요."
  "maintenance_mode	false	점검 모드예요. true면 기관과 시스템의 관리자 계층 외 요청을 503(MAINTENANCE_MODE)으로 거절하며 로그인 등 예외 경로는 유지해요. 상태 캐시의 유효시간은 15초이며 이미 처리 중인 요청은 중단하지 않아요."
  "memory_usage_warn	0.8	승인 화면에서 할당 메모리와 물리 메모리의 비율을 경고하는 기준이에요."
  "notification_retention_days	365	오래된 알림을 정리하는 기준이에요(30~3650일). 발송 대기나 처리 중인 알림은 지우지 않고 전달 결과 기록도 남겨요."
  "platform_subdomain_reserve_days	30	해제한 플랫폼 이름을 예약해 두는 기간이에요(일). VM에 연결했던 이름은 같은 VM에서 다시 연결할 수 있고, 별도로 발급한 도메인은 같은 워크스페이스에서 되살릴 수 있어요. 기한이 지나면 이름을 정리할 대상이 되며 0이면 해제 뒤 바로 정리 대상이 돼요."
  "platform_subdomains_per_vm	3	VM 하나에 연결할 수 있는 플랫폼 서브도메인 수예요. 예약 중인 이름과 커스텀 도메인은 제외하며 0이면 새 플랫폼 서브도메인 연결을 막아요."
  "port_forward_alloc_limit_per_hour	20	사용자별 포트포워딩 생성 허용 횟수예요(시간당)."
  "port_forward_band_alert_percent	80	릴레이 공개 포트 대역 사용률의 경고 기준이에요(%). 도달하면 시스템 관리자에게 알림을 접수해요."
  "port_forward_suspend_conns_per_min	6000	매핑별 분당 새 연결 수의 자동 정지 임계값이에요. 초과하면 ACTIVE 매핑을 SUSPENDED로 바꿔요."
  "port_forward_suspend_mbytes_per_min	1000	매핑별 분당 전송량(MiB)의 자동 정지 임계값이에요. 초과하면 ACTIVE 매핑을 SUSPENDED로 바꿔요."
  "port_forwarding_enabled	false	포트포워딩(릴레이 공개 포트)의 신규 생성 스위치예요. false면 새 매핑 생성을 차단하고 기존 매핑은 유지해요."
  "ssh_gateway_enabled	false	SSH 게이트웨이의 전역 스위치예요. false면 새 게이트웨이 접속을 차단하며 기존 SSH 연결은 종료하지 않아요."
  "vcpu_overcommit_warn	3.0	승인 화면에서 할당 vCPU와 물리 스레드의 비율을 경고하는 기준이에요."
  "vm_delete_grace_hours	168	본인 삭제를 접수한 뒤 파기까지 기다릴 시간이에요(시간). 관리자만 이 유예 기간에 취소할 수 있어요."
  "vm_expiry_autostop_enabled	true	사용 기간(end_date, 포함)이 지난 VM의 매시간 자동 종료 요청을 정하는 스위치예요. 끄면 자동 종료를 요청하지 않고 예고 알림만 처리해요."
  "vm_expiry_notice_days	[14,7,1]	VM 사용 종료 사전 알림 시점이에요(D-일). 중복 없이 1~90일을 최대 5개 지정하며 D-1 알림은 중요하게 표시해요."
  "web_terminal_enabled	false	웹 터미널의 전역 스위치예요. false면 새 티켓 발급과 재교환을 거절해요. 열린 세션도 다음 정상 권한 재확인에서 닫혀요. 기본 확인 주기는 60초이며 통신 실패로 늦어질 수 있어요. VM별 차단과 접근 권한도 별도로 확인하며 VM 설정으로 전역 차단을 우회할 수 없어요."
)

# ── values supplied at run time ──────────────────────────────────────────────
echo "== read the curated word lists"
# These are operator-curated and grow over time — 416 reserved names and 43
# blocked ones at the time of writing, against 7 and 6 in the migration that
# used to seed them. Keeping them as files means an addition is a one-line diff
# a reviewer can read, not a JSON blob inside a SQL statement.
list_to_json() {
  local file="$1"
  [ -s "$file" ] || { echo "  MISSING or empty: $file" >&2; return 1; }
  # One entry per line; blank lines and # comments ignored. jq does the quoting,
  # so an entry with a character that matters to JSON cannot break the array.
  jq -R -s -c 'split("\n") | map(select(length > 0 and (startswith("#") | not)))' <"$file"
}
RESERVED_JSON=$(list_to_json "$DATA_DIR/reserved-subdomains.txt")
PROFANITY_JSON=$(list_to_json "$DATA_DIR/profanity-subdomains.txt")

# The admin API rejects either list above this many entries. Writing a longer one
# straight into the database succeeds and then locks the key out of the console
# permanently: every edit, including one that would shorten the list back, fails
# validation. So the limit is enforced here, where the failure is a refusal
# rather than a trap.
LIST_MAX=500
for pair in "reserved:$RESERVED_JSON" "profanity:$PROFANITY_JSON"; do
  label=${pair%%:*}; json=${pair#*:}
  n=$(jq length <<<"$json")
  echo "  $label: $n entries"
  [ "$n" -le "$LIST_MAX" ] || {
    echo "  the $label list has $n entries, more than the $LIST_MAX the admin API accepts." >&2
    echo "  Written as is, the key becomes uneditable from the console forever." >&2
    exit 1
  }
done

echo "== check the required values"
# Leading/trailing whitespace is the paste artifact that actually happens, and
# the api's validator matches the whole string — an untrimmed address would be
# published here and then refused by the console on every later edit.
trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  printf '%s' "${s%"${s##*[![:space:]]}"}"
}
CONTACT_EMAIL=$(trim "$CONTACT_EMAIL")
if [ -z "$CONTACT_EMAIL" ] && [ -r /dev/tty ]; then
  # Asked rather than refused when a human is running this: the address is a
  # decision, not configuration somebody forgot to export, and a bootstrap that
  # stops to ask is friendlier than one that stops to complain. Read from the
  # terminal rather than stdin, which the caller may have redirected.
  echo "  Operations contact address. Shown in the console footer and on the"
  echo "  maintenance and error screens, and named as the contact point by both"
  echo "  legal documents. Enter 'none' to publish it empty on purpose."
  printf '  contact_email: '
  read -r CONTACT_EMAIL </dev/tty || CONTACT_EMAIL=
  CONTACT_EMAIL=$(trim "$CONTACT_EMAIL")
fi
if [ -z "$CONTACT_EMAIL" ]; then
  echo "  PICKLE_CONTACT_EMAIL is not set and there is no terminal to ask at." >&2
  echo "  Both legal documents name a contact point and the console footer" >&2
  echo "  shows it; publishing an empty one silently makes those a dead end." >&2
  echo "  Set a real address, or pass 'none' to publish it empty on purpose." >&2
  exit 1
fi
if [ "$CONTACT_EMAIL" = "none" ]; then
  CONTACT_JSON='""'
  echo "  contact_email (this run's value): empty, on request"
else
  # The same check the api's settings editor runs — its pragmatic pattern
  # ([^@\s]+@[^@\s]+\.[^@\s]+, whole string) and its 254-char cap. Anything
  # looser publishes an address the console then refuses to ever save again.
  if [ "${#CONTACT_EMAIL}" -gt 254 ] || \
     ! printf '%s' "$CONTACT_EMAIL" | grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; then
    echo "  PICKLE_CONTACT_EMAIL is not an address the api accepts: '$CONTACT_EMAIL'" >&2
    exit 1
  fi
  CONTACT_JSON=$(jq -R -c . <<<"$CONTACT_EMAIL")
  echo "  contact_email (this run's value): $CONTACT_EMAIL"
fi
# The same shape the api enforces on allowed_root_domains entries: lowercase
# dot-separated labels, 63 chars at most. A root that fails it would sit in the
# row, every publish under it would be refused, and the console could never
# save an edit to the list — the email is checked, so this must be too.
if [ "${#ROOT_DOMAIN}" -gt 63 ] || \
   ! printf '%s' "$ROOT_DOMAIN" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'; then
  echo "  PICKLE_ROOT_DOMAIN is not a hostname the api accepts: '$ROOT_DOMAIN'" >&2
  echo "  (lowercase letters, digits, hyphens and dots; 63 chars max)" >&2
  exit 1
fi
ROOT_DOMAIN_JSON=$(jq -R -c '[.]' <<<"$ROOT_DOMAIN")
echo "  allowed_root_domains (this run's value): $ROOT_DOMAIN_JSON"

RUNTIME=(
  "allowed_root_domains	$ROOT_DOMAIN_JSON	발급을 허용할 루트 도메인 목록이에요."
  "contact_email	$CONTACT_JSON	운영 문의 이메일이에요. 콘솔 푸터와 점검 화면, 오류 화면에 표시하며 비우면 숨겨요."
  "profanity_subdomains	$PROFANITY_JSON	욕설이나 사칭을 막는 서브도메인 금칙어 목록이에요. 시스템 관리자가 추가할 수 있어요."
  "reserved_subdomains	$RESERVED_JSON	신청할 수 없는 예약 서브도메인 목록이에요."
)

# ── write ────────────────────────────────────────────────────────────────────
echo "== write the settings rows"
# Counted before and after, because "wrote nothing" and "wrote everything" look
# identical otherwise. They are not the same event: a run that inserts no keys
# means somebody else already owns every row, and on a real deployment that
# somebody is usually the api's development seeder, which fills the table with
# development values the moment it first starts against an empty database. This
# script never overwrites a value, so it would report success while the curated
# lists it was run to install were silently discarded.
before=$(pgq 'select count(*) from settings;')
sql=""
for row in "${DEFAULTS[@]}" "${RUNTIME[@]}"; do
  IFS=$'\t' read -r key value description <<<"$row"
  k=$(sql_escape "$key"); v=$(sql_escape "$value"); d=$(sql_escape "$description")
  # Insert a missing key whole; for a key that is already there refresh only the
  # help text and leave the operator's value alone. updated_at tracks the value,
  # so a description refresh does not touch it.
  sql+="insert into settings (key, value, description, updated_at)
        values ('$k', '$v'::jsonb, '$d', now())
        on conflict (key) do update set description = excluded.description;
"
done
pgtx "$sql" >/dev/null
after=$(pgq 'select count(*) from settings;')
expected=$(( ${#DEFAULTS[@]} + ${#RUNTIME[@]} ))
inserted=$(( after - before ))
echo "  $inserted key(s) inserted, $before already present and left alone"
if [ "$inserted" -lt "$expected" ]; then
  echo
  echo "  NOTE: $(( expected - inserted )) of the $expected keys already had a row, so the"
  echo "  values in this run were NOT applied to them. Values belong to whoever"
  echo "  wrote them first and this script does not take them over."
  echo "  On a freshly rebuilt database that is usually the api's development"
  echo "  seeder, which fills the table at its first start with development"
  echo "  values: seven reserved names instead of the curated list, an empty"
  echo "  contact address, and whatever root domain it was compiled with."
  echo "  If that is what happened, clear the table and re-run this script"
  echo "  before anything else writes to it."
fi
# The two values typed for THIS run are named individually: the count above
# says how many keys were skipped, but not which. Echoing the input back as if
# written is how a typo survives a re-run — the screen shows the corrected
# address while the row keeps the old one — so what is reported here is read
# back from the database, not from the input.
report_value() { # key  this-run-json
  local key="$1" val="$2" db
  db=$(pgq "select value::text from settings where key = '$(sql_escape "$key")';")
  if [ "$db" = "$val" ]; then
    echo "  $key: $db (this run's value is what the database now holds)"
  else
    echo "  $key: an earlier row already holds $db —"
    echo "    this run's $val was NOT applied. Values belong to whoever wrote"
    echo "    them first; change it in the admin console if it is wrong."
  fi
}
report_value contact_email "$CONTACT_JSON"
report_value allowed_root_domains "$ROOT_DOMAIN_JSON"

# ── verify ───────────────────────────────────────────────────────────────────
echo "== verify"
# A key the code reads but no row carries is invisible in the admin console and
# answers 404 on edit, so the count is worth stating rather than assuming.
echo "  rows: $(pgq 'select count(*) from settings;')"
pgshow "select key,
               left(value::text, 48) as value,
               to_char(updated_at at time zone 'Asia/Seoul', 'MM-DD HH24:MI') as updated
          from settings
         order by key;"
