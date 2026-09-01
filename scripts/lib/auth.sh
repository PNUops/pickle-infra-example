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

# Sign in and echo an access token. Empty output and a non-zero status mean the
# caller could not authenticate; every caller here treats that as fatal.
login_token() {
  local base="$1" email="$2" password="$3"
  local body token challenge secret code
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

  if ! secret=$(sysadmin_totp_secret); then
    rm -f "$body"
    return 1
  fi

  code=$(totp "$secret")
  curl -sS -o "$body" -X POST "$base/auth/mfa" \
    -H 'Content-Type: application/json' \
    -d "{\"mfaToken\":\"$challenge\",\"code\":\"$code\"}"

  token=$(jq -r '.accessToken // empty' "$body")
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
