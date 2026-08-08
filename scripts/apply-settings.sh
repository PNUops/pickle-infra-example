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
  "banner_message	\"\"	전역 공지 배너 문구(점검 모드와 독립 — 콘솔 상단 배너). 비우면 배너를 표시하지 않습니다."
  "custom_domain_limit_per_hour	20	사용자별 커스텀 도메인 연결 허용 횟수(시간당). 커스텀 도메인은 이름마다 인증서를 따로 발급받고 그 발급이 플랫폼 공용 계정 한도를 쓰므로, 한 사용자가 그 한도를 소진해 다른 사용자의 발급까지 막는 것을 방지합니다. 플랫폼 서브도메인은 와일드카드 인증서 한 장으로 덮여 발급이 없으므로 이 제한과 무관합니다."
  "ip_quarantine_hours	24	회수된 IP를 재할당하지 않고 격리하는 시간(시간). 릴레이 에이전트가 보관된 스냅샷을 재적용할 수 있는 기간(24시간)보다 짧게 설정할 수 없습니다."
  "maintenance_message	\"\"	점검 모드 안내 문구. 비우면 기본 안내 문구를 사용합니다."
  "maintenance_mode	false	점검 모드. true면 관리자 계층이 아닌 모든 인증 요청이 503(MAINTENANCE_MODE)으로 거부됩니다. 변경은 15초 이내 반영."
  "memory_usage_warn	0.8	승인 화면 경고 임계값 — 할당 메모리 / 물리 메모리 비율."
  "notification_retention_days	365	알림 보관 기간(일). 기간이 지난 알림은 정리 작업이 삭제합니다. (30~3650)"
  "platform_subdomain_reserve_days	30	플랫폼 서브도메인을 해제한 뒤 그 이름을 예약해 두는 기간(일). 예약 중에는 같은 VM에서 다시 연결할 수 있고 다른 사용자는 가져갈 수 없습니다. 기간이 지나면 정리 작업이 이름을 풀어 줍니다. 0이면 해제 즉시 풀립니다."
  "platform_subdomains_per_vm	3	VM 하나에 연결할 수 있는 플랫폼 서브도메인 개수. 예약 중인 이름은 세지 않습니다. 커스텀 도메인은 이 제한과 무관합니다. 0이면 플랫폼 서브도메인 연결을 막습니다."
  "port_forward_alloc_limit_per_hour	20	사용자별 포트 포워딩 생성 허용 횟수(시간당)."
  "port_forward_band_alert_percent	80	릴레이 공개 포트 대역 사용률 경고 임계값(%). 도달 시 시스템 관리자에게 알림을 보냅니다."
  "port_forward_suspend_conns_per_min	6000	매핑별 분당 신규 연결 수 자동 정지 임계값. 초과 시 해당 매핑을 자동 SUSPENDED 처리합니다."
  "port_forward_suspend_mbytes_per_min	1000	매핑별 분당 전송량(MB) 자동 정지 임계값. 초과 시 해당 매핑을 자동 SUSPENDED 처리합니다."
  "port_forwarding_enabled	false	포트 포워딩(릴레이 공개 포트) 기능 스위치. false면 신규 생성이 차단됩니다 (기존 매핑은 유지)."
  "ssh_gateway_enabled	false	SSH 게이트웨이 전체 활성화 (킬 스위치). false면 모든 SSH 접속이 차단됩니다."
  "vcpu_overcommit_warn	3.0	승인 화면 경고 임계값 — 할당 vCPU / 물리 스레드 비율."
  "vm_delete_grace_hours	168	본인 삭제 접수 후 파기까지의 유예 시간(시간). 유예는 관리자 복구용 안전망."
  "vm_expiry_autostop_enabled	true	사용 기간(end_date, 포함)이 지난 VM을 매시간 자동 정지할지 여부. 끄면 예고 알림만 발송됩니다."
  "vm_expiry_notice_days	[14,7,1]	VM 사용 종료 사전 알림 시점(D-일). 내림차순 단계 최대 5개, 각 1~90. D-1 알림은 HIGH 중요도로 표시됩니다."
  "web_terminal_enabled	false	웹 터미널(브라우저 xterm.js) 전역 킬 스위치. false면 티켓 발급·재교환이 모두 거부되고, 진행 중이던 세션은 다음 60초 재검증에서 종료됩니다. 우선순위: 킬 스위치 > per-VM 차단 > 멤버십."
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
  "allowed_root_domains	$ROOT_DOMAIN_JSON	VM 신청에서 선택할 수 있는 루트 도메인 목록."
  "contact_email	$CONTACT_JSON	운영 문의 이메일(콘솔 푸터·점검·오류 화면에 표시). 비우면 표시하지 않습니다."
  "profanity_subdomains	$PROFANITY_JSON	서브도메인 금칙어(욕설·사칭) 목록. 관리자가 확장할 수 있습니다."
  "reserved_subdomains	$RESERVED_JSON	신청할 수 없는 예약 서브도메인 목록."
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
