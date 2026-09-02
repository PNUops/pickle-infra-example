#!/usr/bin/env bash
# Login helper for scripts that sign in as a system-tier administrator.
#
# Why it exists: since admin 2FA enforcement went on, `POST /auth/login` no
# longer answers with a token for an enrolled account -- it answers with a
# challenge, `{"mfaRequired":true,"mfaToken":"..."}`, and the token only comes
# from a second call to `POST /auth/mfa`. Every script here read `.accessToken`
# straight off the login response, so each one died the moment the system
# administrator enrolled. The failure is worse than it looks in two of them:
# smoke-provisioning and smoke-prod log in as the administrator during cleanup,
# so they fail *after* creating a VM and leave it running on the host.
#
# The org tier is deliberately outside enforcement, so an org login still gets a
# token directly. That is why this helper branches on the response instead of
# always doing two round trips: one function is then correct for both tiers, and
# an org account that enrols later keeps working without another edit. Do not
# "simplify" it into always posting to /auth/mfa.
#
# Usage:
#   . "$(dirname "$0")/lib/auth.sh"
#   AT=$(login_token "$BASE" "$EMAIL" "$PW") || exit 1
#
# The caller's response file is untouched: this uses its own temp file, because
# several callers read the shared `$B` after logging in and would find the MFA
# exchange there instead of what they expected.

# TOTP over the enrolled secret. Computed with stdlib python3 because oathtool
# is not installed on pve-node by design -- the same implementation
# smoke-account-ops uses for its enrolment phase, kept in one place now that a
# second caller wants it.
totp() {
  python3 - "$1" <<'PY'
import base64,hmac,hashlib,struct,sys,time
key=base64.b32decode(sys.argv[1].upper()+'='*((8-len(sys.argv[1])%8)%8))
ctr=int(time.time())//30
mac=hmac.new(key,struct.pack('>Q',ctr),hashlib.sha1).digest()
off=mac[-1]&0xf
print('{:06d}'.format((struct.unpack('>I',mac[off:off+4])[0]&0x7fffffff)%1000000))
PY
}

# The enrolled secret for the seeded system administrator, from the vault.
#
# It is a vault file rather than an api.env variable, so `seed_env` cannot reach
# it: nothing serves this value to the container, and the api keeps its own
# encrypted copy in the database. The file carries a trailing newline that
# base32 refuses, hence the trim.
#
# Recovery codes are deliberately not used here. They are single-use and the
# api consumes one on redemption, so a smoke run driven by them would burn all
# ten in ten runs and then lock the account out of its own recovery path.
sysadmin_totp_secret() {
  local vault file
  vault="${VAULT:-/path/to/secrets-vault}"
  file="${PICKLE_SYSADMIN_TOTP_KEY:-$vault/sysadmin-totp.key}"
  if [ ! -r "$file" ]; then
    echo "auth: cannot read the TOTP secret at $file (unlock the vault first)" >&2
    return 1
  fi
  tr -d '[:space:]' < "$file"
}

# The 30-second TOTP step this process last spent, kept in a file because every
# caller reads the token through `$(...)` and a variable set inside that subshell
# would not survive.
#
# A code buys exactly one authentication: the api records the matched step and
# refuses anything at or below it, so that a code used to sign in cannot also
# turn 2FA off a second later. Two logins inside one step therefore cannot both
# succeed -- and the web-terminal cleanup logs in twice, seconds apart, to
# restore the kill switch and then delete the VM. Waiting for the next step is
# cheaper than spending a round trip to be told no, and the file also covers two
# smoke scripts run back to back.
_auth_step_file() { echo "${TMPDIR:-/tmp}/.pickle-auth-totp-step"; }
_auth_step_now() { python3 -c 'import time;print(int(time.time())//30)'; }
_auth_wait_past() {
  local spent="$1"
  while [ "$(_auth_step_now)" = "$spent" ]; do sleep 2; done
}

# Sign in and echo an access token. Empty output and a non-zero status mean the
# caller could not authenticate; every caller here treats that as fatal.
#
# A fourth argument supplies the enrolled secret for accounts other than the
# seeded administrator. A smoke that promotes a scratch user to the system tier
# has to enrol it too -- enforcement covers the tier, not the account -- and the
# secret for that one comes from the enrolment response, not the vault.
login_token() {
  local base="$1" email="$2" password="$3" given="${4:-}"
  local body token challenge secret code spent
  body=$(mktemp) || return 1

  curl -sS -o "$body" -X POST "$base/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$email\",\"password\":\"$password\"}"

  token=$(jq -r '.accessToken // empty' "$body")
  if [ -n "$token" ]; then
    # Not enrolled in 2FA, or an org-tier account: the token arrives directly.
    rm -f "$body"
    printf '%s' "$token"
    return 0
  fi

  challenge=$(jq -r '.mfaToken // empty' "$body")
  if [ -z "$challenge" ]; then
    echo "auth: login as $email returned neither a token nor a challenge" >&2
    head -c 300 "$body" >&2
    echo >&2
    rm -f "$body"
    return 1
  fi

  if [ -n "$given" ]; then
    secret="$given"
  elif ! secret=$(sysadmin_totp_secret); then
    rm -f "$body"
    return 1
  fi

  spent=$(cat "$(_auth_step_file)" 2>/dev/null || true)
  if [ -n "$spent" ]; then _auth_wait_past "$spent"; fi

  # A locked vault reads as a git-crypt blob rather than base32, and python
  # would print a traceback and nothing else. Without this the empty code goes
  # to the server and comes back as "refused", which names the wrong cause.
  if ! code=$(totp "$secret"); then
    echo "auth: could not compute a code from the stored secret (is the vault unlocked?)" >&2
    rm -f "$body"
    return 1
  fi
  _auth_step_now > "$(_auth_step_file)"

  curl -sS -o "$body" -X POST "$base/auth/mfa" \
    -H 'Content-Type: application/json' \
    -d "{\"mfaToken\":\"$challenge\",\"code\":\"$code\"}"
  token=$(jq -r '.accessToken // empty' "$body")

  # Another process may have spent this step. One retry on the next code covers
  # that without hiding a genuinely wrong secret, which fails again.
  if [ -z "$token" ]; then
    _auth_wait_past "$(cat "$(_auth_step_file)")"
    code=$(totp "$secret") || { rm -f "$body"; return 1; }
    _auth_step_now > "$(_auth_step_file)"
    curl -sS -o "$body" -X POST "$base/auth/login" \
      -H 'Content-Type: application/json' \
      -d "{\"email\":\"$email\",\"password\":\"$password\"}"
    challenge=$(jq -r '.mfaToken // empty' "$body")
    curl -sS -o "$body" -X POST "$base/auth/mfa" \
      -H 'Content-Type: application/json' \
      -d "{\"mfaToken\":\"$challenge\",\"code\":\"$code\"}"
    token=$(jq -r '.accessToken // empty' "$body")
  fi

  if [ -z "$token" ]; then
    echo "auth: the second factor was refused for $email" >&2
    head -c 300 "$body" >&2
    echo >&2
    rm -f "$body"
    return 1
  fi

  rm -f "$body"
  printf '%s' "$token"
}

# mk_verified_user EMAIL PASSWORD NAME → "<accessToken> <publicId> <internalId>"
#
# The account is written straight into the database rather than signed up for.
# Signup sends a real verification mail on this deployment -- it runs the prod
# profile -- and these addresses are fabricated, so every run would post bounces
# to a real domain. Reading the token back is not an option either: it is stored
# hashed, and the mock spool that two of these scripts still read stopped filling
# on 2026-08-18 when the profile changed. They had been making unverified
# accounts ever since, and because the helper echoes three fields, an empty token
# shifted them and handed callers an internal id where a public one was expected.
# smoke-ssh-gateway already worked this way; this is that same function, moved
# here so the other two stop drifting from it.
#
# What signup would have produced is reproduced: a verified ACTIVE account with
# the current consents recorded. The id comes back twice because callers want
# different halves -- the API takes the public UUID, while foreign keys such as
# `audit_logs.actor_id` still hold the internal bigint.
#
# Requires `pgq` and `pgx` from the calling script.
bcrypt_hash() {
  python3 -c "import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt(12)).decode())" "$1" 2>/dev/null
}

mk_verified_user() {
  local base="$1" email="$2" password="$3" name="$4"
  local hash body token
  hash=$(bcrypt_hash "$password")
  if [ -z "$hash" ]; then
    echo "mk_verified_user: could not hash the password" >&2
    return 1
  fi
  pgx "insert into users (email, password_hash, name, role, status, email_verified_at)
       values ('$email', '$hash', '$name', 'USER'::user_role, 'ACTIVE'::user_status, now())
       on conflict (email) do nothing" || return 1
  # The version of each document that is in force, not every version ever
  # published. Signup records what the consent screen showed, and the terms phase
  # counts those rows back. A plain cross join happens to agree today because
  # nothing has been revised yet, and would stop agreeing on the first revision.
  pgx "insert into user_consents (user_id, terms_version_id, consented_at)
       select u.id, t.id, now()
         from users u
         join (select distinct on (doc_type) id from terms_versions
                where effective_at <= now()
                order by doc_type, effective_at desc, id desc) t on true
        where u.email = '$email'
       on conflict do nothing" || return 1
  # Signup also gives the account its personal workspace, and phases that check
  # withdrawal or the refusal to delete a personal workspace look for it. Without
  # this the account exists but owns nothing, and those checks answer 404 rather
  # than the 409 they are asserting.
  pgx "with w as (
         insert into workspaces (kind, name)
         select 'PERSONAL'::workspace_kind, '$name'
          where not exists (
            select 1 from workspace_members m
              join users u on u.id = m.user_id
              join workspaces ws on ws.id = m.workspace_id
             where u.email = '$email' and ws.kind = 'PERSONAL')
         returning id)
       insert into workspace_members (workspace_id, user_id, role)
       select w.id, u.id, 'OWNER'::workspace_member_role from w cross join users u
        where u.email = '$email'" || return 1
  # Every request in a smoke run leaves the host by one address, so the per-IP
  # login window fills partway through and the rest of the run gets 429 with an
  # empty token -- which then reads as a wall of 401s in phases that have nothing
  # to do with authentication. The signup-based helper this replaced cleared the
  # counters for the same reason.
  pgx "delete from auth_rate_limits" || return 1
  body=$(mktemp) || return 1
  curl -sS -o "$body" -X POST "$base/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$email\",\"password\":\"$password\"}"
  token=$(jq -r '.accessToken // empty' "$body")
  rm -f "$body"
  # Never echo a line with an empty first field. Callers `read -r A B C`, and the
  # shell folds leading whitespace, so a missing token slides the public id into
  # A and the internal id into B -- the exact shift this function was written to
  # end. Login can still fail here: the counters cleared above are the
  # application's, not the reverse proxy's, and a row left by an earlier run
  # under a different password survives the `on conflict do nothing` insert.
  if [ -z "$token" ]; then
    echo "mk_verified_user: created $email but could not sign in as it" >&2
    return 1
  fi
  echo "$token $(pgq "select public_id from users where email='$email'") $(pgq "select id from users where email='$email'")"
}
