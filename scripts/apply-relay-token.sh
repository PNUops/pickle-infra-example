#!/usr/bin/env bash
# Issues a relay's sync token through the admin API and installs it on the relay.
#
# Why this is a script: it was the one bootstrap step left to be done by hand,
# and it has to be redone after every database rebuild — the token exists only
# as a hash in the database and as a value in one file on the relay, so a
# drop/create destroys the pairing and every sync fails closed until both sides
# are rewritten together. Doing that by hand is where the mistakes were: the
# plaintext appears exactly once, in one HTTP response, and it is easy to lose
# it into a shell history, a log, or a terminal scrollback that outlives it.
#
# What this deliberately does NOT do: build the relay's environment file. The
# file carries the agent's firewall shaping variables, none of which have
# in-code defaults, and a write that drops one fails the agent closed at its
# next restart. So this script edits the token line of an existing file and
# refuses when there is none — building the file belongs to the relay setup
# procedure, which is where those values are decided.
#
# The plaintext never touches disk on this host, never appears in a command
# line (it travels on stdin), and is never printed. The remote write happens in
# a non-interactive shell, so it is not recorded in the relay's history either.
#
# Usage:
#   bash scripts/apply-relay-token.sh
#
# Environment (all optional; the admin credentials are asked for at the terminal):
#   PICKLE_ADMIN_EMAIL       SYS_ADMIN to act as; asked if unset
#   PICKLE_ADMIN_PASSWORD    that account's password; asked if unset, never echoed
#   PICKLE_RELAY_NAME        lightsail-1   which relay row to issue for
#   PICKLE_APP_CTID          101           container running PostgreSQL + the api
#   PICKLE_DB                pickle_dev
#   PICKLE_TUNNEL_CTID       102           container that can reach the relay's tunnel address
#   PICKLE_RELAY_SSH_KEY     $VAULT/lightsail-ssh.pem
#   PICKLE_RELAY_SSH_USER    admin
#   PICKLE_RELAY_SSH_PORT    22
set -euo pipefail

CTID="${PICKLE_APP_CTID:-101}"
DB="${PICKLE_DB:-pickle_dev}"
TUNNEL_CTID="${PICKLE_TUNNEL_CTID:-102}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
require_ct "$TUNNEL_CTID" pickle-sshgw
RELAY_NAME="${PICKLE_RELAY_NAME:-lightsail-1}"
VAULT="${VAULT:-/path/to/secrets-vault}"
SSH_KEY="${PICKLE_RELAY_SSH_KEY:-$VAULT/lightsail-ssh.pem}"
SSH_USER="${PICKLE_RELAY_SSH_USER:-admin}"
SSH_PORT="${PICKLE_RELAY_SSH_PORT:-22}"
API="http://127.0.0.1:8080/api/v1"

pgq() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'query failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

# The relay's admin port is not reachable from this host: the gateway's forward
# chain admits the tunnel to the api's internal port and nothing else. So the
# TCP connection is proxied through the container that does have the tunnel,
# while ssh itself runs here — the private key never enters the container.
relay_ssh() {
  ssh -i "$SSH_KEY" -p "$SSH_PORT" \
      -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      -o ProxyCommand="pct exec $TUNNEL_CTID -- nc %h %p" \
      "$SSH_USER@$RELAY_IP" "$@"
}

echo "== find the relay"
name_sql=$(sql_escape "$RELAY_NAME")
row=$(pgq "select id, source_ip, coalesce(token_hash, '') <> ''
             from relays where name = '$name_sql';")
[ -n "$row" ] || {
  echo "  no relay named '$RELAY_NAME' in the database." >&2
  echo "  Register the inventory first, then re-run." >&2
  exit 1
}
IFS='|' read -r RELAY_ID RELAY_IP HAD_TOKEN <<<"$row"
echo "  $RELAY_NAME is relay $RELAY_ID at $RELAY_IP"
if [ "$HAD_TOKEN" = "t" ]; then
  echo "  a token is already issued; issuing again invalidates it immediately"
  echo "  and sync fails until the new one is installed a few seconds from now"
fi

echo "== check the relay is reachable and already has an environment file"
RELAY_ENV=/etc/pickle/relay-agent.env
relay_ssh "sudo test -f $RELAY_ENV" || {
  echo "  $RELAY_ENV does not exist on the relay (or the relay is unreachable)." >&2
  echo "  This script replaces the token line of an existing file; it does not" >&2
  echo "  write the agent's other variables, which have no in-code defaults and" >&2
  echo "  are decided when the relay is built. Build the relay first." >&2
  exit 1
}
before=$(relay_ssh "sudo grep -c . $RELAY_ENV")
# Counted without any token line, so the check after the install holds whether
# or not this relay was ever paired before.
before_nontoken=$(relay_ssh "sudo sh -c 'grep -c . $RELAY_ENV | cat
grep -c \"^PICKLE_RELAY_SYNC_TOKEN=\" $RELAY_ENV || true'" | { read -r a; read -r b; echo $((a - b)); })
echo "  environment file present, $before lines"

echo "== authenticate as a system administrator"
if [ -z "${PICKLE_ADMIN_EMAIL:-}" ]; then
  printf '  SYS_ADMIN e-mail: '
  read -r PICKLE_ADMIN_EMAIL </dev/tty
fi
if [ -z "${PICKLE_ADMIN_PASSWORD:-}" ]; then
  printf '  password (not echoed): '
  read -rs PICKLE_ADMIN_PASSWORD </dev/tty
  echo
fi

# Credentials go to curl on stdin, never as arguments: an argument is visible in
# /proc/<pid>/cmdline to every local account for as long as the call runs.
api_call() { # method path json [bearer]
  local extra=()
  [ -n "${4:-}" ] && extra=(-H "Authorization: Bearer $4")
  pct exec "$CTID" -- curl -sS -X "$1" "$API$2" \
    -H 'Content-Type: application/json' "${extra[@]}" --data-binary @- <<<"$3"
}

login=$(api_call POST /auth/login \
  "$(jq -nc --arg e "$PICKLE_ADMIN_EMAIL" --arg p "$PICKLE_ADMIN_PASSWORD" \
       '{email:$e, password:$p}')")
ACCESS=$(jq -r '.accessToken // empty' <<<"$login")
[ -n "$ACCESS" ] || { echo "  login failed: $(jq -r '.message // .' <<<"$login")" >&2; exit 1; }
role=$(jq -r '.user.role // empty' <<<"$login")
[ "$role" = "SYS_ADMIN" ] || { echo "  that account is $role, not SYS_ADMIN" >&2; exit 1; }
echo "  logged in as $(jq -r '.user.email' <<<"$login")"

# Issuing a token is one of the operations behind re-authentication: holding a
# session is not enough, the password has to be proven again in the moment.
reauth=$(api_call POST /auth/reverify \
  "$(jq -nc --arg p "$PICKLE_ADMIN_PASSWORD" '{password:$p}')" "$ACCESS")
unset PICKLE_ADMIN_PASSWORD
if jq -e '.reauthToken // .token // empty' >/dev/null <<<"$reauth"; then
  REAUTH=$(jq -r '.reauthToken // .token' <<<"$reauth")
else
  echo "  re-authentication failed: $(jq -r '.message // .' <<<"$reauth")" >&2
  exit 1
fi
echo "  re-authenticated"

echo "== issue the token"
issued=$(pct exec "$CTID" -- curl -sS -X POST "$API/admin/relays/$RELAY_ID/token" \
  -H "Authorization: Bearer $ACCESS" -H "X-Reauth-Token: $REAUTH" \
  -H 'Content-Length: 0')
TOKEN=$(jq -r '.token // .syncToken // empty' <<<"$issued")
[ -n "$TOKEN" ] || { echo "  issuance failed: $(jq -r '.message // .' <<<"$issued")" >&2; exit 1; }
case "$TOKEN" in
  *[!0-9a-f]* | "") echo "  issued token is not 64 hex characters, refusing to install" >&2; exit 1 ;;
esac
[ "${#TOKEN}" -eq 64 ] || { echo "  issued token is ${#TOKEN} characters, expected 64" >&2; exit 1; }
echo "  issued, ${#TOKEN} hex characters (not shown)"

echo "== install it on the relay"
# Only the token line changes. The file is rewritten through a temporary file
# created at its final mode, so there is never a window where a world-readable
# copy of the token exists, and the value arrives on stdin rather than in the
# command line of any process on the relay.
#
# Every step of the remote half has to fail loudly, because the failure that
# matters is silent: ssh runs this in a plain shell with no `set -e`, so a grep
# that reads nothing or a token that never arrives used to leave a file holding
# one line and no shaping variables at all — which is the exact state the header
# of this script says it refuses to create. The agent then failed closed at the
# restart two lines later. So the remote script sets its own -e, proves the
# token arrived intact, and counts the lines it carried over before it is
# allowed to overwrite anything.
remote_install=$(cat <<'REMOTE'
set -eu
set +o history
umask 077

read -r tok
case "$tok" in
  *[!0-9a-f]* | "") echo "  the token did not arrive intact" >&2; exit 1 ;;
esac
[ "${#tok}" -eq 64 ] || { echo "  token arrived as ${#tok} characters" >&2; exit 1; }

# How many lines have to survive. grep -c prints 0 and exits 1 when there are
# none, which is not an error here.
had=$(sudo grep -cv '^PICKLE_RELAY_SYNC_TOKEN=' "$ENV_FILE" || true)

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
# Exit 1 means it kept nothing — legitimate for a file that held only a token.
sudo grep -v '^PICKLE_RELAY_SYNC_TOKEN=' "$ENV_FILE" > "$tmp" || [ $? -eq 1 ]
kept=$(wc -l < "$tmp")
[ "$kept" -eq "$had" ] || {
  echo "  carried over $kept of $had lines; refusing to overwrite $ENV_FILE" >&2
  exit 1
}

printf 'PICKLE_RELAY_SYNC_TOKEN=%s\n' "$tok" >> "$tmp"
sudo install -m 640 -o root -g root "$tmp" "$ENV_FILE"
sudo systemctl restart relay-agent
REMOTE
)
relay_ssh "ENV_FILE=$RELAY_ENV
$remote_install" <<<"$TOKEN"
unset TOKEN
# The file now holds the lines it had, minus any token line it already carried,
# plus exactly one. Counting non-empty lines against that is the only reading
# that is right both on a re-issue and on the first install.
read -r after tokens <<<"$(relay_ssh "sudo sh -c 'grep -c . $RELAY_ENV || true
grep -c \"^PICKLE_RELAY_SYNC_TOKEN=\" $RELAY_ENV || true'" | tr '\n' ' ')"
want=$((before_nontoken + 1))
echo "  environment file rewritten, $after non-empty lines (expected $want)"
[ "$tokens" -eq 1 ] || { echo "  $tokens token lines in $RELAY_ENV, expected 1" >&2; exit 1; }
[ "$after" -eq "$want" ] || { echo "  expected $want non-empty lines, found $after" >&2; exit 1; }

echo "== verify"
# The journal is the wrong place to look: the agent logs 'applied' only when a
# snapshot actually changes, so its absence after a restart means nothing. The
# proof is the relay's last contact advancing, which only a successfully
# authenticated sync can do.
was=$(pgq "select coalesce(to_char(last_contact_at, 'YYYY-MM-DD HH24:MI:SS'), 'never')
             from relays where id = $RELAY_ID;")
echo "  last contact before: $was"
for _ in $(seq 1 12); do
  sleep 5
  now=$(pgq "select coalesce(to_char(last_contact_at, 'YYYY-MM-DD HH24:MI:SS'), 'never')
               from relays where id = $RELAY_ID;")
  if [ "$now" != "$was" ]; then
    echo "  last contact now:    $now"
    echo "  the relay authenticated with the new token"
    exit 0
  fi
done
echo "  last contact has not advanced in 60 seconds." >&2
echo "  Check the agent: journalctl -u relay-agent -n 20 on the relay." >&2
echo "  Repeated 'sync failed' means the token did not take." >&2
exit 1
