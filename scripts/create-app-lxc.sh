#!/usr/bin/env bash
# Creates the pickle-app LXC (PostgreSQL 18 + JRE 25 + nginx) on vmbr1 and
# provisions the full app layout expected by deploy-api.sh / deploy-console.sh:
# pickle system user, /opt/pickle/api/releases, /etc/pickle/api.env skeleton,
# pickle-api systemd unit, nginx vhost, PostgreSQL role/db.
# Idempotent: safe to re-run against an existing container — creation and
# package bootstrap are skipped when already done, every provisioning step is
# guarded, and an existing /etc/pickle/api.env is never overwritten.
set -euo pipefail

CTID="${CTID:-101}"
HOSTNAME="${HOSTNAME_LXC:-pickle-app}"
IP="${IP:-198.18.1.20/16}"
GW="${GW:-198.18.0.1}"
STORAGE="${STORAGE:-local-lvm}"
DISK_GB="${DISK_GB:-32}"
CORES="${CORES:-4}"
MEMORY_MB="${MEMORY_MB:-8192}"
TEMPLATE="local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"

# --- 1. Container creation (skipped if CTID already exists) ------------------
if pct status "$CTID" >/dev/null 2>&1; then
  echo "CTID $CTID already exists; skipping creation."
else
  [ -f "/var/lib/vz/template/cache/$(basename "$TEMPLATE" | sed 's/^.*vztmpl\///')" ] || \
    pveam download local "$(basename "$TEMPLATE")"

  pct create "$CTID" "$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --storage "$STORAGE" \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap 1024 \
    --net0 "name=eth0,bridge=vmbr1,ip=${IP},gw=${GW}" \
    --nameserver 8.8.8.8 \
    --unprivileged 1 \
    --features nesting=1 \
    --onboot 1 \
    --start 1
  sleep 5
fi

# --- 2. Package bootstrap (skipped if the toolchain is already present) ------
if ! pct exec "$CTID" -- bash -lc \
    'command -v java >/dev/null && command -v nginx >/dev/null && command -v psql >/dev/null'; then
  pct exec "$CTID" -- bash -lc '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get full-upgrade -y -qq
apt-get install -y -qq curl ca-certificates locales postgresql-common
sed -i "s/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/" /etc/locale.gen && locale-gen >/dev/null
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >/dev/null
apt-get install -y -qq postgresql-18 nginx openjdk-25-jre-headless
systemctl enable --now postgresql nginx
'
else
  echo "java/nginx/psql already installed; skipping package bootstrap."
fi

# --- 3. App provisioning (every step guarded; mirrors the live layout) -------
provision=$(cat <<'EOS'
set -euo pipefail

# 3a. pickle system account
if ! id pickle >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /opt/pickle --shell /usr/sbin/nologin pickle
  echo "created system user pickle"
fi

# 3b. Directory layout expected by the deploy scripts
install -d -o pickle -g pickle /opt/pickle /opt/pickle/api /opt/pickle/api/releases
install -d -o root -g root /etc/pickle /var/www/pickle-console
# dev mock-mail spool home (full bodies incl. verification links live here,
# not in the journal) — service-user only
install -d -o pickle -g pickle -m 700 /var/lib/pickle

# 3b-1. pve-node hostname mapping — the Proxmox client pins the PVE root CA and the
# pveproxy cert's SANs cover the hostname (pve-node), not the vmbr1 bridge IP, so
# the API must be addressed as https://pve-node:8006 (nodes.api_host, api V10).
# Outside the PVE-managed block, so pct config rewrites leave it alone.
if ! grep -q '^198\.18\.0\.1 pve-node$' /etc/hosts; then
  printf '\n# pve-node Proxmox API via vmbr1 (cert SAN is the hostname)\n198.18.0.1 pve-node\n' >> /etc/hosts
  echo "added pve-node hosts mapping"
fi

# 3b-2. Route to the relay WG transport net via the sshgw LXC. The api answers
# relay sync requests (source 100.64.0.1); without this route the replies
# follow the default gateway (pve-node) toward the campus uplink and die. Same-L2
# next hop on vmbr1, so the leg never crosses pve-node FORWARD. The container's
# /etc/network/interfaces is Proxmox-regenerated — if-up.d is the persistence
# mechanism that survives it.
if [ ! -f /etc/network/if-up.d/pickle-relay-route ]; then
  cat > /etc/network/if-up.d/pickle-relay-route <<'ROUTE'
#!/bin/sh
# Relay WG transport net via the sshgw LXC (sync replies to the relay).
[ "$IFACE" = eth0 ] || exit 0
ip route replace 100.64.0.0/30 via 198.18.1.30 dev eth0
ROUTE
  chmod 755 /etc/network/if-up.d/pickle-relay-route
  echo "installed if-up.d relay transport route (100.64.0.0/30 via 198.18.1.30)"
fi
IFACE=eth0 /etc/network/if-up.d/pickle-relay-route

# 3c. /etc/pickle/api.env skeleton — never overwrite an existing file.
# Keep this skeleton in sync with the api.env key set whenever a new env key
# is introduced (it is the bootstrap-time authoritative key list).
if [ ! -f /etc/pickle/api.env ]; then
  cat > /etc/pickle/api.env <<'ENVSKEL'
# pickle-api environment (root:pickle 640). Uncomment and fill in real values
# before the first API deploy. Never commit this file anywhere.
#SPRING_PROFILES_ACTIVE=dev
#PICKLE_DB_URL=jdbc:postgresql://127.0.0.1:5432/pickle_dev
#PICKLE_DB_USER=pickle
#PICKLE_DB_PASSWORD=
#PICKLE_JWT_SECRET=
#PICKLE_VERIFICATION_BASE_URL=
#PICKLE_PASSWORD_RESET_BASE_URL=
#PICKLE_SEED_SYSADMIN_EMAIL=
#PICKLE_SEED_SYSADMIN_PASSWORD=
#PICKLE_SEED_ORGADMIN_EMAIL=
#PICKLE_SEED_ORGADMIN_PASSWORD=
#PICKLE_SMTP_HOST=
#PICKLE_SMTP_PORT=
#PICKLE_SMTP_USERNAME=
#PICKLE_SMTP_PASSWORD=
#PICKLE_MAIL_FROM=Pickle <no-reply@example.ac.kr>
#PICKLE_PROXMOX_URL=
#PICKLE_PROXMOX_TOKEN_ID=
#PICKLE_PROXMOX_TOKEN_SECRET=
# AES-256-GCM key for reversible credential storage (vms.initial_password_enc,
# contract v0.7.0+). base64 32 bytes: `openssl rand -base64 32`. Required
# outside dev/test (startup fails fast without it).
#PICKLE_CREDENTIALS_KEY=
# internal-API bearers — set on both ends
#PICKLE_PROXY_AGENT_TOKEN=
#PICKLE_PROXY_AGENT_URL=http://198.18.1.10:9443
#PICKLE_SSHGW_TOKEN=
# sshgw upstream PUBLIC key (from LXC 102, cloud-init injected into VMs)
#PICKLE_SSH_PLATFORM_PUBLIC_KEY=
# web terminal — terminal-bridge PUBLIC key
# (LXC 102 terminal_ed25519_key.pub, co-injected via cloud-init) and the
# api->bridge control bearer (same value as PICKLE_TERMINAL_CONTROL_TOKEN in
# LXC 102 /etc/pickle/sshgw.env).
#PICKLE_TERMINAL_PUBLIC_KEY=
#PICKLE_TERMINAL_CONTROL_TOKEN=
#PICKLE_TERMINAL_BRIDGE_URL=http://198.18.1.30:8083
ENVSKEL
  echo "wrote /etc/pickle/api.env skeleton — fill in secrets before starting pickle-api"
fi
# web-terminal keys — appended once to pre-existing env files too.
if ! grep -q PICKLE_TERMINAL_CONTROL_TOKEN /etc/pickle/api.env; then
  cat >> /etc/pickle/api.env <<'ENVSKEL'
# web terminal:
#PICKLE_TERMINAL_PUBLIC_KEY=
#PICKLE_TERMINAL_CONTROL_TOKEN=
#PICKLE_TERMINAL_BRIDGE_URL=http://198.18.1.30:8083
ENVSKEL
  echo "appended web-terminal keys to /etc/pickle/api.env"
fi
chown root:pickle /etc/pickle/api.env
chmod 640 /etc/pickle/api.env

# 3d. pickle-api systemd unit (installed/updated only when content differs)
unit_tmp=$(mktemp)
cat > "$unit_tmp" <<'UNIT'
[Unit]
Description=Pickle API (Spring Boot)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
User=pickle
Group=pickle
EnvironmentFile=/etc/pickle/api.env
ExecStart=/usr/bin/java -Xmx1g -jar /opt/pickle/api/current.jar
WorkingDirectory=/opt/pickle/api
Restart=on-failure
RestartSec=5
MemoryMax=2G
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
if ! cmp -s "$unit_tmp" /etc/systemd/system/pickle-api.service 2>/dev/null; then
  install -m 644 -o root -g root "$unit_tmp" /etc/systemd/system/pickle-api.service
  systemctl daemon-reload
  echo "installed /etc/systemd/system/pickle-api.service"
fi
rm -f "$unit_tmp"
systemctl enable -q pickle-api  # started by the first deploy-api.sh run

# 3e. nginx vhost (SPA + /api reverse proxy), disable the default site
nginx_changed=0
vhost_tmp=$(mktemp)
cat > "$vhost_tmp" <<'VHOST'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name pickle.pusan.ac.kr _;

    root /var/www/pickle-console;
    index index.html;

    # security headers (2026-07-17 hardening). This vhost is plain HTTP and
    # always sits behind the reverse proxy, which is where TLS terminates for
    # the main domain (its own certificate since the 2026-07-28 cutover) and
    # where the CDN-fronted names still arrive over HTTPS. HSTS is ignored by
    # browsers on the plain-HTTP dev path, so emitting it here is harmless.
    #
    # CSP (2026-07-26): the console bundle is fully self-hosted (fonts, JS, CSS,
    # icons) and talks only to its own origin, so 'self' covers everything.
    # 'unsafe-inline' is required for style-src alone: the web terminal
    # (xterm.js) injects <style> elements at runtime and cannot be nonced from
    # here. Scripts stay strict 'self' — the edge CDN's injected analytics
    # beacon is blocked by design and the console does not depend on it.
    # Seven days, not a year, and no includeSubDomains. The name this pin lands
    # on is an interim one — the service moves to a purchased domain before
    # launch — and its certificate chain has no operating history yet: it is
    # renewed on a ninety-day cycle that has not completed once. A pin outlives
    # the mistake that sets it, so a single missed renewal under a year-long
    # max-age would lock every visitor out with no way to click through, while
    # a week bounds that to something recoverable. There are no hosts beneath
    # this name, so includeSubDomains only stores up a trap for whatever gets
    # put there later. Raise it once renewals have run clean and the final
    # domain is settled.
    add_header Strict-Transport-Security "max-age=604800" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header X-XSS-Protection "0" always;
    add_header Content-Security-Policy "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'" always;

    location /api/ {
        # Notice images upload through here (one multipart file, 2 MB cap on
        # the api side); 4m mirrors the api's whole-request multipart cap so
        # this tier is never the tighter bound. The LXC 100 vhost in front
        # carries the same value on its own catch-all location: a request
        # crosses both nginx tiers, so raising only the edge leaves the reject
        # here instead of removing it. That vhost is written by
        # apply-main-domain-vhost.sh; the two values have to move together.
        client_max_body_size 4m;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # trust the client IP computed and validated by the LXC 100
        # tier (CF-Connecting-IP only from genuine CF peers), not the raw
        # attacker-controllable CF header. LXC 100 is this vhost's only client.
        proxy_set_header X-Real-IP $http_x_real_ip;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        # the API application emits its own copies of these; drop them so this
        # vhost stays the single source and responses carry no duplicates.
        proxy_hide_header X-Frame-Options;
        proxy_hide_header X-Content-Type-Options;
        proxy_hide_header X-XSS-Protection;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
VHOST
if ! cmp -s "$vhost_tmp" /etc/nginx/sites-available/pickle.conf 2>/dev/null; then
  install -m 644 -o root -g root "$vhost_tmp" /etc/nginx/sites-available/pickle.conf
  nginx_changed=1
fi
rm -f "$vhost_tmp"
if [ ! -L /etc/nginx/sites-enabled/pickle.conf ]; then
  ln -sfn /etc/nginx/sites-available/pickle.conf /etc/nginx/sites-enabled/pickle.conf
  nginx_changed=1
fi
if [ -e /etc/nginx/sites-enabled/default ]; then
  rm -f /etc/nginx/sites-enabled/default
  nginx_changed=1
fi
nginx -t
if [ "$nginx_changed" = 1 ]; then
  systemctl reload nginx
  echo "installed nginx vhost pickle.conf"
fi

# 3f. PostgreSQL role + database (localhost-only PGDG default, scram auth)
if [ "$(su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='pickle'\"")" != "1" ]; then
  db_pass=$(head -c 512 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 32)
  su - postgres -c "psql -qc \"CREATE ROLE pickle LOGIN PASSWORD '$db_pass'\""
  echo "created PostgreSQL role 'pickle'."
  echo "  generated password (shown once, NOT stored anywhere): $db_pass"
  echo "  record it as PICKLE_DB_PASSWORD in /etc/pickle/api.env"
fi
if [ "$(su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='pickle_dev'\"")" != "1" ]; then
  su - postgres -c "createdb -O pickle pickle_dev"
  echo "created database pickle_dev (owner pickle)"
fi

echo "app provisioning complete"
EOS
)
pct exec "$CTID" -- bash -lc "$provision"

echo "pickle-app LXC $CTID ready: $(pct exec "$CTID" -- bash -lc 'psql --version; java -version 2>&1 | head -1; nginx -v 2>&1')"
echo "next: run scripts/apply-log-retention.sh to reinstate the journald cap and"
echo "      the mock-mail spool rotation in this container, then"
echo "      scripts/apply-tls-ciphers.sh to reinstate the TLS cipher policy."
