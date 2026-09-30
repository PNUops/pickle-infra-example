#!/usr/bin/env bash
# Signup smoke test: the real signup and email-verification path on the
# deployed dev environment, verification mail included.
#
# Every other smoke writes its accounts straight into the database, because
# signup sends a real verification mail on this deployment (it runs the prod
# mail profile) and the api stores only the token's hash. That left signup,
# verification and the account state they produce with no live check at all.
# This smoke is that check: it signs up through the api and takes the token
# from the mail that actually arrived.
#
# Run on the pve-node HOST as root, from the infra checkout, with the secrets vault
# unlocked: the database checks go through `pct exec` into CTID 101, and the
# default address reaches the api through the host's hairpin /etc/hosts entry.
#
# Journey: preflight (mailbox credential signs in, before anything is created)
# -> GET /meta/terms -> POST /auth/signup with consent to every current
# document (202) -> DB: one PENDING_VERIFICATION account from this run, its
# consents recorded, no workspace yet, one open signup token -> login refused
# as not verified (403) -> the verification mail read from the mailbox -> POST
# /auth/verify-email (200) -> DB: ACTIVE, verified, token spent -> the same
# token again is refused (410) and changes nothing -> login (200) -> GET /me
# (ACTIVE, USER, no pending consents) -> GET /me/consents matches /meta/terms
# -> GET /workspaces holds exactly the personal workspace, owned -> cleanup.
#
# What it proves: the signup request the console sends is accepted with the
# current terms, the api mails a verification link that reaches a real mailbox,
# the link's token activates the account exactly once, and activation leaves
# what the rest of the platform expects: ACTIVE, verified, consents on file,
# and the personal workspace. A 202 from signup proves nothing on its own --
# the api answers 202 for an address it already knows too, and mails a notice
# instead -- which is why the database is checked before the mail is read and
# the notice is refused by subject.
#
# What it does not prove: deliverability to any mailbox other than the one the
# smoke domain is routed to (a mail filed as spam there is still accepted, and
# the run says which folder it came from), the console's verify-email page
# (the token is posted to the api directly), resend-verification, Google
# sign-in, or anything past the first login. No VM, workspace, request or
# notification is created beyond what signup itself makes.
#
# Who receives mail during a run: exactly one message, the verification mail,
# to this run's own address signup-<epoch>-<random>@example.com, which the
# domain's routing delivers to the operator mailbox. Signup and verification
# notify nobody else: the api publishes no notification and mails no
# administrator on either step (AuthService.signup, verifyEmail and
# activateAccount), and the invitation claims activation runs find nothing for
# an address that did not exist before the run. The run never sends a
# notification, announcement or request.
#
# The mailbox credential: an app password for the mailbox, read from
# ${VAULT}/smoke-mailbox/mailbox-imap.txt (a vault file; override the path with
# PICKLE_SMOKE_IMAP_PASSWORD_FILE). A missing or empty file, or a password
# the server refuses, fails the run before any account is created. It is
# never printed, and it reaches python by file path, not argv. The mailbox is
# read over IMAP (imap.example.com:993, SSL) by scripts/lib/signup_mailbox.py,
# which opens folders read-only, fetches with BODY.PEEK and never changes,
# moves or deletes a message; it looks only at mail addressed to this run's
# exact address that arrived after the signup was sent.
#
# The scratch account is closed on every exit, failed and interrupted runs
# included: an open signup token is spent, a still-pending account is set
# DISABLED, and an activated one goes through disable_scratch_user. Only an
# account created since this run started is touched. A cleanup that fails
# prints `CLEANUP FAILED: ...` as the last line.
#
# Usage: smoke-signup.sh [ORIGIN]   (default https://pickle.pusan.ac.kr)
# Env:   VAULT, PICKLE_SMOKE_IMAP_PASSWORD_FILE, PICKLE_SMOKE_IMAP_USER,
#        PICKLE_SMOKE_IMAP_HOST, PICKLE_SMOKE_SIGNUP_DOMAIN (default
#        example.com), PICKLE_SMOKE_VERIFY_BASE_URL (default
#        ORIGIN/verify-email, the api's PICKLE_VERIFICATION_BASE_URL), CTID.
# Requires: curl, jq, python3, pct.
set -uo pipefail # no -e: cleanup and the summary must run even after failures

ORIGIN="${1:-https://pickle.pusan.ac.kr}"
BASE="$ORIGIN/api/v1"
VERIFY_BASE="${PICKLE_SMOKE_VERIFY_BASE_URL:-$ORIGIN/verify-email}"
CTID="${CTID:-101}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VAULT="${VAULT:-/path/to/secrets-vault}"
IMAP_PW_FILE="${PICKLE_SMOKE_IMAP_PASSWORD_FILE:-$VAULT/smoke-mailbox/mailbox-imap.txt}"
IMAP_USER="${PICKLE_SMOKE_IMAP_USER:-ops-mailbox@example.com}"
IMAP_HOST="${PICKLE_SMOKE_IMAP_HOST:-imap.example.com}"
MAILBOX="$HERE/lib/signup_mailbox.py"

TS=$(date +%s)
# Lowercase by construction: login lowercases the address it is given.
# The domain has to be one whose mail is routed to the smoke mailbox, and one
# the api's signup address pattern accepts.
SIGNUP_DOMAIN="${PICKLE_SMOKE_SIGNUP_DOMAIN:-example.com}"
USER_EMAIL="signup-${TS}-${RANDOM}@${SIGNUP_DOMAIN,,}"
USER_PW="smoke-pass-${TS}-${RANDOM}!"
USER_NAME="가입 스모크"
REASON='스모크 확인용 임시 계정 정리'

# shellcheck source=scripts/lib/auth.sh
. "$HERE/lib/auth.sh"

pgq(){ pct exec "$CTID" -- su - postgres -c "psql -d pickle_dev -qtAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
pgx(){
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -d pickle_dev -v ON_ERROR_STOP=1 -f -" <<<"$1" 2>&1); then
    printf 'pgx failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
}

for cmd in curl jq python3 pct; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "missing required command: $cmd (this script must run on the pve-node host as root)"
    exit 2
  }
done
[ -r "$MAILBOX" ] || { echo "missing $MAILBOX (copy scripts/lib along with the script)"; exit 2; }

PASS=0
FAIL=0
USER_DB_ID=""
USER_AT=""
TOKEN=""
CONSENTS_JSON=""
SIGNUP_SENT=0

BODY=$(mktemp)
REQ=$(mktemp)
HDR=$(mktemp)
chmod 600 "$BODY" "$REQ" "$HDR"

# The accounts this run may touch: this address, created since the run began.
mine="email = '$USER_EMAIL' and created_at >= '$SMOKE_RUN_STARTED'::timestamptz"

# Close whatever signup left, whichever step the run stopped at. The open token
# is spent first so the link still sitting in the mailbox cannot activate the
# account later; a pending account is disabled the same way an admin would
# (status-change row, no actor), because disable_scratch_user only closes
# ACTIVE ones; then the activated case goes through the shared helper.
close_signup_account() {
  [ "$SIGNUP_SENT" = 1 ] || return 0
  pgx "update email_verifications set used_at = now(), updated_at = now()
        where used_at is null
          and user_id in (select id from users where $mine)" || return 1
  pgx "with u as (
         update users
            set status = 'DISABLED', disabled_at = now(), disabled_reason = '$REASON',
                token_version = token_version + 1
          where $mine and status = 'PENDING_VERIFICATION'
         returning id)
       insert into user_status_changes (user_id, from_status, to_status, actor_id, reason, changed_at)
       select id, 'PENDING_VERIFICATION', 'DISABLED', null, '$REASON', now() from u" || return 1
  disable_scratch_user "$USER_EMAIL" || return 1
  local open left
  left=$(pgq "select count(*) from users where $mine and status <> 'DISABLED'")
  open=$(pgq "select count(*) from email_verifications where used_at is null
               and user_id in (select id from users where $mine)")
  if [ "$left" != 0 ] || [ "$open" != 0 ]; then
    echo "close_signup_account: $USER_EMAIL not closed (not disabled=${left:-unknown}, open tokens=${open:-unknown})" >&2
    return 1
  fi
}

on_exit() {
  local rc=$?
  # A second Ctrl-C (or a TERM) during cleanup would otherwise cut it off with
  # nothing printed, leaving the account open.
  trap '' INT TERM
  TOKEN=""
  if close_signup_account; then
    if [ "$SIGNUP_SENT" = 1 ]; then
      echo "-- cleanup: scratch user $USER_EMAIL disabled, no open verification token --"
    else
      echo "-- cleanup: no signup was sent; nothing to close --"
    fi
  else
    echo "-- cleanup: scratch user $USER_EMAIL NOT closed --" >&2
    echo "CLEANUP FAILED: scratch user $USER_EMAIL not closed"
    rc=1
  fi
  rm -f "$BODY" "$REQ" "$HDR"
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ok() { echo "PASS  $1"; PASS=$((PASS + 1)); }
ko() { echo "FAIL  $1"; FAIL=$((FAIL + 1)); }

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

# step_masked: like step, but never prints the body. For login, whose success
# body is a bearer token, and for anything sent with the verification token.
step_masked() {
  local name="$1" expect="$2" out
  shift 2
  out=$(curl -sS -o "$BODY" -w '%{http_code}' "$@")
  if [ "$out" = "$expect" ]; then
    ok "$name ($out)"
    return 0
  fi
  ko "$name (expected $expect, got $out; body withheld)"
  return 1
}

# Secrets never reach a command line, where every process on the host could
# read them: request bodies carrying the token or the password are built by jq
# from its environment (`env.X`, not `--arg`) and handed to curl through a
# 0600 file, and the bearer header reaches curl the same way (`-H @file`).
# printf is a shell builtin, so writing the files execs nothing.
post_secret() {
  local name="$1" expect="$2" path="$3" json="$4"
  printf '%s' "$json" > "$REQ"
  step_masked "$name" "$expect" -X POST "$BASE$path" -H 'Content-Type: application/json' --data-binary "@$REQ"
  local rc=$?
  : > "$REQ"
  return "$rc"
}

login_json() { SMOKE_E="$USER_EMAIL" SMOKE_P="$USER_PW" jq -nc '{email: env.SMOKE_E, password: env.SMOKE_P}'; }
verify_json() { SMOKE_T="$TOKEN" jq -nc '{token: env.SMOKE_T}'; }

# Write the bearer header file for the calls made as the scratch user.
auth_header() { printf 'Authorization: Bearer %s\n' "$USER_AT" > "$HDR"; }

# The terms in force, as SQL, for comparing recorded consents.
current_terms="select distinct on (doc_type) id from terms_versions
                where effective_at <= now() order by doc_type, effective_at desc, id desc"

# ── preflight: nothing is created unless the mail can be read back ──
phase_preflight() {
  # A run that cannot read the mailbox would sign up an account and then have
  # no way to verify it, so the credential is proven first: the file exists,
  # is not empty, and the server accepts it.
  local rc
  python3 -B "$MAILBOX" --host "$IMAP_HOST" --user "$IMAP_USER" \
    --password-file "$IMAP_PW_FILE" --check-login
  rc=$?
  case "$rc" in
    0) ok "mailbox credential signs in ($IMAP_USER at $IMAP_HOST)" ;;
    2) ko "mailbox credential unreadable at $IMAP_PW_FILE (unlock the vault or set PICKLE_SMOKE_IMAP_PASSWORD_FILE)"; return 1 ;;
    *) ko "mailbox sign-in failed (exit $rc)"; return 1 ;;
  esac

  local n
  n=$(pgq "select count(*) from users where email = '$USER_EMAIL'")
  if [ "$n" != 0 ]; then
    ko "scratch address $USER_EMAIL is not new (rows=${n:-unknown}; database unreachable?)"
    return 1
  fi
  ok "scratch address $USER_EMAIL is new"
}

# ── phase 1: signup, and what it wrote ──
phase_signup() {
  step "meta/terms" 200 "$BASE/meta/terms" || return 1
  CONSENTS_JSON=$(jq -c '[.[] | {docType, version}]' "$BODY")
  if [ -z "$CONSENTS_JSON" ] || [ "$(jq 'length' <<<"$CONSENTS_JSON")" -lt 1 ]; then
    ko "no current terms to consent to"
    return 1
  fi
  ok "consenting to $(jq 'length' <<<"$CONSENTS_JSON") current document(s)"

  SIGNUP_AT=$(date +%s)
  SIGNUP_SENT=1
  SMOKE_E="$USER_EMAIL" SMOKE_P="$USER_PW" SMOKE_N="$USER_NAME" SMOKE_C="$CONSENTS_JSON" \
    jq -nc '{email: env.SMOKE_E, password: env.SMOKE_P, name: env.SMOKE_N,
             consents: (env.SMOKE_C | fromjson)}' > "$REQ"
  step "signup" 202 -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' --data-binary "@$REQ"
  local rc=$?
  : > "$REQ"
  [ "$rc" = 0 ] || return 1

  # The 202 is the same for an address the api already knows, so the account
  # itself is the evidence.
  local n st verified
  n=$(pgq "select count(*) from users where email = '$USER_EMAIL'")
  if [ "$n" != 1 ]; then
    ko "signup created exactly one account for $USER_EMAIL (rows=${n:-unknown})"
    return 1
  fi
  USER_DB_ID=$(pgq "select id from users where $mine")
  if [ -z "$USER_DB_ID" ]; then
    ko "the account for $USER_EMAIL was not created by this run"
    return 1
  fi
  st=$(pgq "select status from users where id = $USER_DB_ID")
  verified=$(pgq "select email_verified_at is not null from users where id = $USER_DB_ID")
  if [ "$st" = PENDING_VERIFICATION ] && [ "$verified" = f ]; then
    ok "account created PENDING_VERIFICATION, not verified"
  else
    ko "account state after signup (status=${st:-none}, verified=${verified:-?})"
  fi

  local want got total
  want=$(pgq "select count(*) from ($current_terms) t")
  got=$(pgq "select count(*) from user_consents where user_id = $USER_DB_ID
              and terms_version_id in ($current_terms)")
  total=$(pgq "select count(*) from user_consents where user_id = $USER_DB_ID")
  if [ "${want:-0}" -ge 1 ] && [ "$got" = "$want" ] && [ "$total" = "$want" ]; then
    ok "consents recorded for all $want current document(s)"
  else
    ko "consents recorded (current=${want:-?}, recorded current=${got:-?}, recorded total=${total:-?})"
  fi

  local ws
  ws=$(pgq "select count(*) from workspace_members where user_id = $USER_DB_ID")
  if [ "$ws" = 0 ]; then
    ok "no workspace before verification"
  else
    ko "workspace memberships before verification (expected 0, got ${ws:-?})"
  fi

  local open
  open=$(pgq "select count(*) from email_verifications where user_id = $USER_DB_ID
               and purpose = 'SIGNUP' and used_at is null and expires_at > now()")
  if [ "$open" = 1 ]; then
    ok "one open signup verification token"
  else
    ko "open signup verification tokens (expected 1, got ${open:-?})"
    return 1
  fi

  post_secret "login before verification refused" 403 /auth/login "$(login_json)"
  local code
  code=$(jq -r '.code // empty' "$BODY" 2>/dev/null)
  : > "$BODY"
  if [ "$code" = AUTH_EMAIL_NOT_VERIFIED ]; then
    ok "refusal code AUTH_EMAIL_NOT_VERIFIED"
  else
    ko "refusal code (expected AUTH_EMAIL_NOT_VERIFIED, got ${code:-none})"
  fi
}

# ── phase 2: the verification mail ──
phase_mail() {
  # Three lines on stdout: the token, "junk" or "normal", and the folder.
  local rc out rest kind folder
  out=$(python3 -B "$MAILBOX" --host "$IMAP_HOST" --user "$IMAP_USER" \
    --password-file "$IMAP_PW_FILE" --address "$USER_EMAIL" --verify-base "$VERIFY_BASE" \
    --not-before "$SIGNUP_AT" --timeout 120)
  rc=$?
  TOKEN=${out%%$'\n'*}
  rest=${out#*$'\n'}
  kind=${rest%%$'\n'*}
  folder=${rest#*$'\n'}
  out=""
  if [ "$rc" != 0 ] || [ "${#TOKEN}" != 43 ]; then
    TOKEN=""
    ko "verification mail to $USER_EMAIL read from the mailbox (exit $rc)"
    return 1
  fi
  ok "verification mail to $USER_EMAIL read from $folder; link base $VERIFY_BASE (token withheld)"
  if [ "$kind" = junk ]; then
    echo "WARN  the verification mail was filed as spam ($folder)"
  fi
}

# ── phase 3: verification, once and only once ──
phase_verify() {
  post_secret "verify-email" 200 /auth/verify-email "$(verify_json)" || return 1

  local st verified used
  st=$(pgq "select status from users where id = $USER_DB_ID")
  verified=$(pgq "select email_verified_at is not null from users where id = $USER_DB_ID")
  if [ "$st" = ACTIVE ] && [ "$verified" = t ]; then
    ok "account ACTIVE and verified"
  else
    ko "account after verification (status=${st:-none}, verified=${verified:-?})"
  fi
  used=$(pgq "select string_agg(coalesce(used_at::text, 'open'), ',') from email_verifications
               where user_id = $USER_DB_ID and purpose = 'SIGNUP'")
  if [ -n "$used" ] && [ "$used" != open ] && [[ "$used" != *,* ]]; then
    ok "the signup token is spent"
  else
    ko "signup token state after verification (${used:-none})"
  fi

  # Single use: the same token again. 410 alone would also be the answer for a
  # token that never existed, so this counts only because the same token was
  # just accepted, and the token row must not move.
  post_secret "same token again refused" 410 /auth/verify-email "$(verify_json)"
  local code after
  code=$(jq -r '.code // empty' "$BODY" 2>/dev/null)
  if [ "$code" = AUTH_VERIFICATION_TOKEN_EXPIRED ]; then
    ok "refusal code AUTH_VERIFICATION_TOKEN_EXPIRED"
  else
    ko "refusal code (expected AUTH_VERIFICATION_TOKEN_EXPIRED, got ${code:-none})"
  fi
  after=$(pgq "select string_agg(coalesce(used_at::text, 'open'), ',') from email_verifications
                where user_id = $USER_DB_ID and purpose = 'SIGNUP'")
  if [ "$after" = "$used" ]; then
    ok "second use changed nothing"
  else
    ko "signup token row changed on second use"
  fi
  TOKEN=""
}

# ── phase 4: the account signup produced ──
phase_account() {
  post_secret "login" 200 /auth/login "$(login_json)" || return 1
  USER_AT=$(jq -r '.accessToken // empty' "$BODY")
  : > "$BODY"
  if [ -z "$USER_AT" ]; then
    ko "login returned an access token"
    return 1
  fi
  ok "login returned an access token"
  auth_header

  step "me" 200 "$BASE/me" -H "@$HDR" || return 1
  local summary
  summary=$(jq -c '{email, status, role, pending: (.pendingConsents | length)}' "$BODY")
  if [ "$summary" = "{\"email\":\"$USER_EMAIL\",\"status\":\"ACTIVE\",\"role\":\"USER\",\"pending\":0}" ]; then
    ok "me: $USER_EMAIL ACTIVE USER, no pending consents"
  else
    ko "me (got $summary)"
  fi

  step "me/consents" 200 "$BASE/me/consents" -H "@$HDR" || return 1
  local mine_c want_c
  mine_c=$(jq -c '[.[] | {docType, version}] | sort' "$BODY")
  want_c=$(jq -c 'sort' <<<"$CONSENTS_JSON")
  if [ "$mine_c" = "$want_c" ]; then
    ok "me/consents matches the terms consented to"
  else
    ko "me/consents (got $mine_c, expected $want_c)"
  fi

  step "workspaces" 200 "$BASE/workspaces" -H "@$HDR" || return 1
  local ws
  ws=$(jq -c '[.[] | {kind, myRole}]' "$BODY")
  if [ "$ws" = '[{"kind":"PERSONAL","myRole":"OWNER"}]' ]; then
    ok "exactly one workspace: the personal one, owned"
  else
    ko "workspaces after verification (got $ws)"
  fi
  local db_ws
  db_ws=$(pgq "select count(*) from workspace_members m join workspaces w on w.id = m.workspace_id
                where m.user_id = $USER_DB_ID and w.kind = 'PERSONAL' and m.role = 'OWNER'")
  if [ "$db_ws" = 1 ]; then
    ok "DB: one PERSONAL workspace owned"
  else
    ko "DB: PERSONAL workspace ownership (expected 1, got ${db_ws:-?})"
  fi
}

echo "== Signup smoke against $BASE (CTID $CTID) =="

if phase_preflight && phase_signup && phase_mail && phase_verify; then
  phase_account
fi

TOTAL=$((PASS + FAIL))
echo "SIGNUP SMOKE: $PASS/$TOTAL"
[ "$FAIL" -eq 0 ]
