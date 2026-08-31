#!/bin/bash
# shellcheck disable=SC2015  # ok/ko branches return deterministically
# LLM API key e2e against the deployed development environment.
#
# Run on the platform host as root after the api, console and llm-gateway are
# deployed. The cycle uses the existing ORG_ADMIN inside its managed
# organisation, leaves a retained audit trail, and then exercises request ->
# typed approval context -> approval -> token issue -> one real chat call ->
# limit replacement -> suspend -> resume -> revoke.
# This lifecycle deliberately keeps creditLimit at 0.00 so it exercises the
# TOKEN axis without requiring a registered OpenRouter business account.
# Positive CREDIT first binding and cross-org account isolation require a real
# account and binding ON, followed by a separate smoke that is not implemented
# yet. They are outside this script's coverage.
set -uo pipefail

BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
LLM_BASE="${LLM_BASE:-https://llm.pcl.kr/v1}"
LLM_SMOKE_MODEL="${LLM_SMOKE_MODEL:-pickle-general}"
CTID="${CTID:-101}"
SYNC_TIMEOUT="${LLM_SYNC_TIMEOUT:-90}"
TS="$(date +%s)-$RANDOM"
BODY="$(mktemp)"

P=0
F=0
REQUESTER_TOKEN=""
REQUESTER_PASSWORD=""
REAUTH_TOKEN=""
KEY_ID=""
KEY_REVOKED=0

ok() { echo "PASS  $1"; P=$((P + 1)); }
ko() { echo "FAIL  $1"; F=$((F + 1)); }

seed_env() {
  pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-" 2>/dev/null
}

request() {
  local name="$1" expected="$2" sensitive=0 code
  if [ "${3:-}" = "--sensitive" ]; then
    sensitive=1
    shift 3
  else
    shift 2
  fi
  code=$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 30 "$@") || code=000
  if [ "$code" = "$expected" ]; then
    ok "$name ($code)"
    return 0
  fi
  ko "$name (want $expected got $code)"
  if [ "$sensitive" = 1 ]; then
    echo "response body withheld (contains one-time credential material)"
  else
    head -c 300 "$BODY"
    echo
  fi
  return 1
}

login() {
  curl -sS --max-time 30 -X POST "$BASE/auth/login" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg email "$1" --arg password "$2" \
      '{email:$email,password:$password}')" | jq -r '.accessToken // empty'
}

reauth() {
  curl -sS --max-time 30 -X POST "$BASE/auth/reverify" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg password "$2" '{password:$password}')" \
    | jq -r '.reauthToken // empty'
}

wait_gateway() {
  local name="$1" expected_http="$2" expected_error="$3" deadline code error_code
  deadline=$((SECONDS + SYNC_TIMEOUT))
  while :; do
    code=$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 20 \
      "$LLM_BASE/models" -H "Authorization: Bearer $LLM_TOKEN") || code=000
    error_code=$(jq -r '.error.code // empty' "$BODY" 2>/dev/null)
    if [ "$code" = "$expected_http" ] \
      && { [ -z "$expected_error" ] || [ "$error_code" = "$expected_error" ]; }; then
      ok "$name ($code${error_code:+ $error_code})"
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      ko "$name (last=$code${error_code:+ $error_code})"
      return 1
    fi
    sleep 3
  done
}

cleanup() {
  local rc=$?
  if [ -n "$KEY_ID" ] && [ "$KEY_REVOKED" != 1 ] && [ -n "$REQUESTER_TOKEN" ]; then
    [ -n "$REAUTH_TOKEN" ] || REAUTH_TOKEN=$(reauth "$REQUESTER_TOKEN" "$REQUESTER_PASSWORD")
    if [ -n "$REAUTH_TOKEN" ]; then
      curl -sS -o /dev/null --max-time 20 -X POST "$BASE/llm-keys/$KEY_ID/revoke" \
        -H "Authorization: Bearer $REQUESTER_TOKEN" -H "X-Reauth-Token: $REAUTH_TOKEN" || true
    fi
  fi
  rm -f "$BODY"
  exit "$rc"
}
trap cleanup EXIT

for command in curl jq pct; do
  command -v "$command" >/dev/null 2>&1 || { echo "missing command: $command" >&2; exit 1; }
done

REQUESTER_EMAIL="$(seed_env PICKLE_SEED_ORGADMIN_EMAIL)"
REQUESTER_EMAIL="${REQUESTER_EMAIL:-orgadmin@pnuops.com}"
REQUESTER_PASSWORD="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"
[ -n "$REQUESTER_PASSWORD" ] || { echo 'missing ORG_ADMIN password in api.env' >&2; exit 1; }
REQUESTER_TOKEN="$(login "$REQUESTER_EMAIL" "$REQUESTER_PASSWORD")"
[ -n "$REQUESTER_TOKEN" ] && ok 'ORG_ADMIN requester and approver login' \
  || { ko 'ORG_ADMIN requester and approver login'; exit 1; }
APPROVER_TOKEN="$REQUESTER_TOKEN"

request 'create workspace' 201 -X POST "$BASE/workspaces" \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg name "LLM smoke $TS" '{kind:"TEAM",name:$name}')" || exit 1
WORKSPACE_ID=$(jq -r '.id // empty' "$BODY")

request 'read requester organisation roles' 200 "$BASE/me" \
  -H "Authorization: Bearer $REQUESTER_TOKEN" || exit 1
ORG_ID=$(jq -r '[.managedOrgs[]? | select(.role == "ORG_ADMIN")][0].orgId // empty' "$BODY")
[ -n "$WORKSPACE_ID" ] && [ -n "$ORG_ID" ] \
  || { ko 'workspace and organisation ids'; exit 1; }

REQUEST_BODY=$(jq -nc --arg workspace "$WORKSPACE_ID" --arg org "$ORG_ID" \
  --arg name "LLM smoke $TS" \
  '{type:"LLM_API_KEY",displayName:$name,workspaceId:$workspace,orgId:$org,
    purpose:"배포 후 LLM API lifecycle smoke",courseOrProject:null,extraNote:null,
    reqStartDate:null,reqEndDate:null,
    llmKey:{usagePlan:"1-token 실제 호출과 상태 전이 검증",reqRpm:20,reqTpm:1000,
      reqDailyTokens:10000}}')
request 'create LLM key request' 201 -X POST "$BASE/requests" \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H 'Content-Type: application/json' \
  -d "$REQUEST_BODY" || exit 1
REQUEST_ID=$(jq -r '.id // empty' "$BODY")

request 'admin list filters LLM requests' 200 \
  "$BASE/admin/requests?type=LLM_API_KEY&orgId=$ORG_ID" \
  -H "Authorization: Bearer $APPROVER_TOKEN" || exit 1
jq -e --arg id "$REQUEST_ID" '.content | any(.id == $id and .type == "LLM_API_KEY")' \
  "$BODY" >/dev/null && ok 'request appears in typed admin queue' \
  || { ko 'request appears in typed admin queue'; exit 1; }

request 'typed approval context' 200 "$BASE/admin/requests/$REQUEST_ID/context" \
  -H "Authorization: Bearer $APPROVER_TOKEN" || exit 1
jq -e '.type == "LLM_API_KEY" and .llmKey != null and .vm == null' "$BODY" >/dev/null \
  && ok 'approval context selects only llmKey' \
  || { ko 'approval context selects only llmKey'; exit 1; }

APPROVAL_BODY='{"grantedStartDate":null,"grantedEndDate":null,"comment":"LLM lifecycle smoke","llmKey":{"grantedRpm":20,"grantedTpm":1000,"grantedConcurrency":2,"grantedDailyTokens":10000,"grantedCreditLimit":0.00,"grantedCreditLimitReset":null}}'
request 'approve LLM request' 200 -X POST "$BASE/admin/requests/$REQUEST_ID/approve" \
  -H "Authorization: Bearer $APPROVER_TOKEN" -H 'Content-Type: application/json' \
  -d "$APPROVAL_BODY" || exit 1

request 'find pending key in admin list' 200 \
  "$BASE/admin/llm/keys?orgId=$ORG_ID&workspaceId=$WORKSPACE_ID&status=PENDING&query=LLM%20smoke" \
  -H "Authorization: Bearer $APPROVER_TOKEN" || exit 1
KEY_ID=$(jq -r '.content[0].id // empty' "$BODY")
[ -n "$KEY_ID" ] && ok 'pending key materialized' || { ko 'pending key materialized'; exit 1; }
jq -e '.content[0] | (.creditLimit == 0 and .creditLimitReset == null
  and .creditAxisConnected == false)' "$BODY" >/dev/null \
  && ok 'pending key is TOKEN-only' \
  || { ko 'pending key is TOKEN-only'; exit 1; }
jq -e '.content[0] | (has("token") or has("tokenHash") or has("tokenPrefix")) | not' \
  "$BODY" >/dev/null && ok 'admin list contains no key secret' \
  || { ko 'admin list contains no key secret'; exit 1; }

REAUTH_TOKEN="$(reauth "$REQUESTER_TOKEN" "$REQUESTER_PASSWORD")"
[ -n "$REAUTH_TOKEN" ] && ok 'requester re-authentication' \
  || { ko 'requester re-authentication'; exit 1; }
request 'issue plaintext key once' 200 --sensitive -X POST "$BASE/llm-keys/$KEY_ID/token" \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H "X-Reauth-Token: $REAUTH_TOKEN" || exit 1
LLM_TOKEN=$(jq -r '.token // empty' "$BODY")
[ -n "$LLM_TOKEN" ] && ok 'plaintext key received without logging it' \
  || { ko 'plaintext key received'; exit 1; }

wait_gateway 'issued key reaches gateway' 200 '' || exit 1
jq -e --arg model "$LLM_SMOKE_MODEL" '.data | any(.id == $model)' "$BODY" >/dev/null \
  && ok "TOKEN smoke model is available ($LLM_SMOKE_MODEL)" \
  || { ko "TOKEN smoke model is available ($LLM_SMOKE_MODEL)"; exit 1; }
MODEL="$LLM_SMOKE_MODEL"
CHAT_BODY=$(jq -nc --arg model "$MODEL" \
  '{model:$model,messages:[{role:"user",content:"Reply with OK."}],max_tokens:1}')
request 'one real chat completion' 200 -X POST "$LLM_BASE/chat/completions" \
  -H "Authorization: Bearer $LLM_TOKEN" -H 'Content-Type: application/json' \
  -d "$CHAT_BODY" || exit 1

LIMIT_BODY='{"rpm":30,"tpm":2000,"concurrency":2,"dailyTokens":20000,"creditLimit":0.00,"creditLimitReset":null}'
request 'replace all six limits' 200 -X PUT "$BASE/admin/llm/keys/$KEY_ID/limits" \
  -H "Authorization: Bearer $APPROVER_TOKEN" -H 'Content-Type: application/json' \
  -d "$LIMIT_BODY" || exit 1
jq -e '.rpm == 30 and .tpm == 2000 and .concurrency == 2 and .dailyTokens == 20000
  and .creditLimit == 0 and .creditLimitReset == null
  and .creditAxisConnected == false' \
  "$BODY" >/dev/null && ok 'admin detail returns replaced limits' \
  || { ko 'admin detail returns replaced limits'; exit 1; }

request 'suspend active key' 200 -X POST "$BASE/admin/llm/keys/$KEY_ID/suspend" \
  -H "Authorization: Bearer $APPROVER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"LLM lifecycle smoke"}' || exit 1
wait_gateway 'suspension reaches gateway' 403 'account_suspended' || exit 1

request 'resume suspended key' 200 -X POST "$BASE/admin/llm/keys/$KEY_ID/resume" \
  -H "Authorization: Bearer $APPROVER_TOKEN" || exit 1
wait_gateway 'resume reaches gateway' 200 '' || exit 1

request 'revoke key' 204 -X POST "$BASE/llm-keys/$KEY_ID/revoke" \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H "X-Reauth-Token: $REAUTH_TOKEN" || exit 1
KEY_REVOKED=1
wait_gateway 'revocation reaches gateway' 401 'api_key_revoked' || exit 1

request 'admin detail preserves revoked record' 200 "$BASE/admin/llm/keys/$KEY_ID" \
  -H "Authorization: Bearer $APPROVER_TOKEN" || exit 1
jq -e '.status == "REVOKED" and (has("token") or has("tokenHash") or has("tokenPrefix") | not)' \
  "$BODY" >/dev/null && ok 'revoked record is secret-free and read-only' \
  || { ko 'revoked record is secret-free and read-only'; exit 1; }

echo
echo "LLM key lifecycle smoke: PASS=$P FAIL=$F"
[ "$F" -eq 0 ]
