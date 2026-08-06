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

# ── database access ──────────────────────────────────────────────────────────
# Statements are fed on STDIN, never as `psql -c "…"`. A -c argument travels
# through the second shell `su -c` spawns, and that parse expands $ and eats
# quotes: the word lists below are JSON arrays full of double quotes and would
# terminate the argument early. Nothing re-parses stdin.
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
  "ip_quarantine_hours	24	회수된 IP를 재할당하지 않고 격리하는 시간(시간). 릴레이 에이전트가 보관된 스냅샷을 재적용할 수 있는 기간(24시간)보다 짧게 설정할 수 없습니다."
  "maintenance_message	\"\"	점검 모드 안내 문구. 비우면 기본 안내 문구를 사용합니다."
  "maintenance_mode	false	점검 모드. true면 관리자 계층이 아닌 모든 인증 요청이 503(MAINTENANCE_MODE)으로 거부됩니다. 변경은 15초 이내 반영."
  "memory_usage_warn	0.8	승인 화면 경고 임계값 — 할당 메모리 / 물리 메모리 비율."
  "notification_retention_days	365	알림 보관 기간(일). 기간이 지난 알림은 정리 작업이 삭제합니다. (30~3650)"
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
echo "  reserved: $(jq length <<<"$RESERVED_JSON") entries"
echo "  profanity: $(jq length <<<"$PROFANITY_JSON") entries"

echo "== check the required values"
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
  echo "  contact_email: published empty, on request"
else
  # Deliberately loose — this rejects the mistakes that actually happen (a bare
  # name, a URL, a stray space) without pretending to validate an address.
  case "$CONTACT_EMAIL" in
    *[![:space:]]@*.*) ;;
    *) echo "  PICKLE_CONTACT_EMAIL does not look like an address: $CONTACT_EMAIL" >&2; exit 1 ;;
  esac
  CONTACT_JSON=$(jq -R -c . <<<"$CONTACT_EMAIL")
  echo "  contact_email: $CONTACT_EMAIL"
fi
ROOT_DOMAIN_JSON=$(jq -R -c '[.]' <<<"$ROOT_DOMAIN")
echo "  allowed_root_domains: $ROOT_DOMAIN_JSON"

RUNTIME=(
  "allowed_root_domains	$ROOT_DOMAIN_JSON	VM 신청에서 선택할 수 있는 루트 도메인 목록."
  "contact_email	$CONTACT_JSON	운영 문의 이메일(콘솔 푸터·점검·오류 화면에 표시). 비우면 표시하지 않습니다."
  "profanity_subdomains	$PROFANITY_JSON	서브도메인 금칙어(욕설·사칭) 목록. 관리자가 확장할 수 있습니다."
  "reserved_subdomains	$RESERVED_JSON	신청할 수 없는 예약 서브도메인 목록."
)

# ── write ────────────────────────────────────────────────────────────────────
echo "== write the settings rows"
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
echo "  ${#DEFAULTS[@]} release defaults + ${#RUNTIME[@]} run-time values written"

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
