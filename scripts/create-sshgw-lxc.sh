#!/usr/bin/env bash
# Creates the pickle-sshgw LXC (CTID 102, 198.18.1.30) on vmbr1 and provisions
# the SSH gateway: stock sshpiperd (v1.5.4) + the two Pickle Go daemons
# (sshgw-proxyfront, sshgw-route-plugin), a kernel-WireGuard endpoint (wg0 =
# 100.64.0.2/30) to the AWS Lightsail relay, systemd units, and a peer-only
# nftables firewall. Mirrors create-app-lxc.sh: idempotent, every step guarded.
#
# WireGuard runs INSIDE this unprivileged LXC (an early spike confirmed kernel WG
# works with no special container config; only the host `wireguard` module load
# is required — this script registers it). The proxyfront enforces PROXY-v2 from
# the relay peer only; the route plugin calls pickle-api /internal/sshgw/route.
#
# The two Go binaries are NOT built here (matching create-app-lxc vs deploy-api):
# build them with `sshgw/scripts/build.sh` and place them at
# /opt/pickle/sshgw/bin/. Every service is fail-closed and stays stopped until
# secrets + binaries are in place.
#
# The Lightsail relay side (HAProxy + WireGuard peer) is user-provisioned later
# its config template lives in infra/lightsail/. After it exists, fill the [Peer]
# block in /etc/wireguard/wg0.conf with the relay public key + endpoint.
set -euo pipefail

CTID="${CTID:-102}"
SSHGW_DIR="${SSHGW_DIR:-/root/pickle/sshgw}"
UNITS="sshpiperd.service sshgw-proxyfront.service sshgw-terminal-bridge.service"
UNIT_STAGE=/run/pickle-sshgw-units
HOSTNAME="${HOSTNAME_LXC:-pickle-sshgw}"
IP="${IP:-198.18.1.30/16}"
GW="${GW:-198.18.0.1}"
STORAGE="${STORAGE:-local-lvm}"
DISK_GB="${DISK_GB:-8}"
CORES="${CORES:-1}"
MEMORY_MB="${MEMORY_MB:-512}"
TEMPLATE="local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"

# Pinned sshpiperd release (checksum-verified below).
SSHPIPERD_VERSION="${SSHPIPERD_VERSION:-v1.5.4}"
SSHPIPERD_ASSET="sshpiperd_with_plugins_linux_x86_64.tar.gz"
SSHPIPERD_SHA256="f03ab1a52d2856094180388727788f0dc4ef9b436c0d9348c1363bdd689b4ec7"

# --- 0. Host prerequisite: kernel WireGuard module (persistent) --------------
if [ ! -f /etc/modules-load.d/wireguard.conf ]; then
  echo "wireguard" > /etc/modules-load.d/wireguard.conf
  echo "registered persistent wireguard module load on host"
fi
modprobe wireguard

# --- 1. Container creation (skipped if CTID already exists) ------------------
if pct status "$CTID" >/dev/null 2>&1; then
  echo "CTID $CTID already exists; skipping creation."
else
  [ -f "/var/lib/vz/template/cache/$(basename "$TEMPLATE")" ] || \
    pveam download local "$(basename "$TEMPLATE")"

  pct create "$CTID" "$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --storage "$STORAGE" \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap 256 \
    --net0 "name=eth0,bridge=vmbr1,ip=${IP},gw=${GW}" \
    --nameserver 8.8.8.8 \
    --unprivileged 1 \
    --onboot 1 \
    --start 1
  sleep 5
fi

# --- 2. Package bootstrap (skipped if the toolchain is already present) ------
if ! pct exec "$CTID" -- bash -lc \
    'command -v wg >/dev/null && command -v nft >/dev/null && [ -x /usr/local/bin/sshpiperd ]'; then
  pct exec "$CTID" -- bash -lc "
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get full-upgrade -y -qq
apt-get install -y -qq curl ca-certificates wireguard-tools nftables
# stock sshpiperd release, checksum-pinned
cd /tmp
curl -fsSLO 'https://github.com/tg123/sshpiper/releases/download/${SSHPIPERD_VERSION}/${SSHPIPERD_ASSET}'
echo '${SSHPIPERD_SHA256}  ${SSHPIPERD_ASSET}' | sha256sum -c -
tar -xzf '${SSHPIPERD_ASSET}' sshpiperd
install -m 755 -o root -g root sshpiperd /usr/local/bin/sshpiperd
rm -f '${SSHPIPERD_ASSET}' sshpiperd
"
else
  echo "wg/nft/sshpiperd already installed; skipping package bootstrap."
fi

# --- 2b. Stage the systemd units shipped by the sshgw repo -------------------
# The unit definitions belong to the sshgw repo (scripts/systemd) — the same
# files deploy-sshgw.sh re-syncs on every deploy. This script installs those
# copies instead of carrying its own, so provisioning and deploys can never
# disagree about what the units say.
pct exec "$CTID" -- install -d -m 700 "$UNIT_STAGE"
for u in $UNITS; do
  [ -f "$SSHGW_DIR/scripts/systemd/$u" ] || {
    echo "missing unit $SSHGW_DIR/scripts/systemd/$u (is SSHGW_DIR right?)" >&2; exit 1; }
  pct push "$CTID" "$SSHGW_DIR/scripts/systemd/$u" "$UNIT_STAGE/$u" --perms 644
done

# --- 3. Provisioning (every step guarded; transport subnet 100.64.0.0/30) --
# The heredoc is single-quoted: it runs verbatim inside the container. Transport
# constants (100.64.0.2/.1, :22, :2222, :51820) are architectural and fixed.
pct exec "$CTID" -- bash -lc "$(cat <<'EOS'
set -euo pipefail

# 3a. pickle service account (runs sshpiperd + the plugin + shim, no shell)
if ! id pickle >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /opt/pickle --shell /usr/sbin/nologin pickle
  echo "created system user pickle"
fi

# 3a-1. Disable the container's own sshd: the Debian template ships openssh-server
# listening on 0.0.0.0:22, which collides with the proxyfront shim's bind of
# 100.64.0.2:22 ("address already in use"). This LXC is managed via `pct exec`
# from the host, so it needs no network SSH of its own.
if systemctl is-enabled ssh >/dev/null 2>&1 || systemctl is-active ssh >/dev/null 2>&1; then
  systemctl disable --now ssh 2>/dev/null || true
  echo "disabled the container's own sshd (frees :22 for the proxyfront shim)"
fi
# The socket unit must be masked too, not only the service. Debian 13 activates
# sshd through ssh.socket, which binds the WILDCARD [::]:22 early at boot and
# then wins the race against the shim's 100.64.0.2:22 bind — the shim dies with
# "address already in use" and user SSH is down until it is stopped. Disabling
# ssh.service alone leaves the socket enabled, so this was only visible after a
# reboot (observed live 2026-07-26 on a container provisioned long before).
if [ "$(systemctl is-enabled ssh.socket 2>/dev/null || true)" != masked ]; then
  systemctl disable --now ssh.socket 2>/dev/null || true
  systemctl mask ssh.socket
  echo "masked ssh.socket (it would grab :22 at boot ahead of the shim)"
fi

# 3b. Directory layout. bin/ holds the two Go daemons (deployed separately);
# /etc/pickle/sshgw holds the sshpiperd host key (pickle-readable).
install -d -o pickle -g pickle /opt/pickle /opt/pickle/sshgw /opt/pickle/sshgw/bin
install -d -o root   -g root   /etc/pickle
install -d -o pickle -g pickle -m 750 /etc/pickle/sshgw

# 3c. /etc/pickle/sshgw.env — never overwrite an existing file (holds the token)
if [ ! -f /etc/pickle/sshgw.env ]; then
  cat > /etc/pickle/sshgw.env <<'ENVSKEL'
# pickle-sshgw environment (root:pickle 640). Fill in before starting services.
# Route plugin -> pickle-api /internal/sshgw/route:
PICKLE_SSHGW_API_BASE=http://198.18.1.20:8080
#PICKLE_SSHGW_TOKEN=          # shared bearer; REQUIRED (services fail-closed without it)
# Platform upstream key the route plugin presents to VMs on the publickey path
# (generated on-box by create-sshgw-lxc.sh). Default path shown; override only if
# the key lives elsewhere. The plugin fails closed if the file is missing/invalid.
#PICKLE_SSHGW_UPSTREAM_KEY_FILE=/etc/pickle/sshgw/upstream_ed25519_key
# Ingress shim (defaults shown; override only if the transport subnet changes):
SSHGW_PROXYFRONT_LISTEN=100.64.0.2:22
SSHGW_PROXYFRONT_UPSTREAM=127.0.0.1:2222
SSHGW_PROXYFRONT_PEER=100.64.0.1/32
ENVSKEL
  echo "wrote /etc/pickle/sshgw.env skeleton — set PICKLE_SSHGW_TOKEN before starting"
fi
# web-terminal bridge keys — appended once to pre-existing env files too
# (the bridge shares sshgw.env and PICKLE_SSHGW_TOKEN).
if ! grep -q PICKLE_TERMINAL_CONTROL_TOKEN /etc/pickle/sshgw.env; then
  cat >> /etc/pickle/sshgw.env <<'ENVSKEL'
# Web-terminal bridge. Reuses PICKLE_SSHGW_API_BASE
# and PICKLE_SSHGW_TOKEN above for its api calls; fails closed without tokens.
#PICKLE_TERMINAL_CONTROL_TOKEN=   # api->bridge control bearer; REQUIRED
#PICKLE_TERMINAL_KEY_FILE=/etc/pickle/sshgw/terminal_ed25519_key
#PICKLE_TERMINAL_WS_LISTEN=198.18.1.30:8082
#PICKLE_TERMINAL_CONTROL_LISTEN=198.18.1.30:8083
PICKLE_TERMINAL_CONSOLE_ORIGIN=https://pickle.pusan.ac.kr
#PICKLE_TERMINAL_WS_PEER=198.18.1.10
#PICKLE_TERMINAL_CONTROL_PEER=198.18.1.20
#PICKLE_TERMINAL_MAX_FRAME=1048576
#PICKLE_TERMINAL_MAX_SESSIONS=200
ENVSKEL
  echo "appended web-terminal bridge keys to /etc/pickle/sshgw.env — set PICKLE_TERMINAL_CONTROL_TOKEN"
fi
chown root:pickle /etc/pickle/sshgw.env
chmod 640 /etc/pickle/sshgw.env

# 3c-1. Platform upstream key — the ed25519 key the route plugin presents to VMs
# on the publickey path. Generated once on-box, NEVER overwritten and never leaves the LXC;
# its public half is copied into pickle-api (PICKLE_SSH_PLATFORM_PUBLIC_KEY) for
# cloud-init injection into every VM. ssh-keygen comes from the template's
# openssh-client. Owner pickle:pickle so the fail-closed plugin (runs as pickle)
# can read it; private half 600, public half 644.
if [ ! -f /etc/pickle/sshgw/upstream_ed25519_key ]; then
  ssh-keygen -t ed25519 -N '' -C pickle-platform-upstream -f /etc/pickle/sshgw/upstream_ed25519_key
  echo "generated platform upstream key at /etc/pickle/sshgw/upstream_ed25519_key"
  echo "  upstream public key (give to pickle-api PICKLE_SSH_PLATFORM_PUBLIC_KEY):"
  echo "    $(cat /etc/pickle/sshgw/upstream_ed25519_key.pub)"
fi
chown pickle:pickle /etc/pickle/sshgw/upstream_ed25519_key /etc/pickle/sshgw/upstream_ed25519_key.pub
chmod 600 /etc/pickle/sshgw/upstream_ed25519_key
chmod 644 /etc/pickle/sshgw/upstream_ed25519_key.pub

# 3c-2. Terminal platform key — the ed25519 key the web-terminal bridge
# presents to VMs. DELIBERATELY separate from the sshgw upstream key so either
# can be revoked/rotated without touching the other.
# Same lifecycle: generated once on-box, never leaves the LXC; public half goes
# to pickle-api as PICKLE_TERMINAL_PUBLIC_KEY for cloud-init co-injection.
if [ ! -f /etc/pickle/sshgw/terminal_ed25519_key ]; then
  ssh-keygen -t ed25519 -N '' -C pickle-terminal-bridge -f /etc/pickle/sshgw/terminal_ed25519_key
  echo "generated terminal platform key at /etc/pickle/sshgw/terminal_ed25519_key"
  echo "  terminal public key (give to pickle-api PICKLE_TERMINAL_PUBLIC_KEY):"
  echo "    $(cat /etc/pickle/sshgw/terminal_ed25519_key.pub)"
fi
chown pickle:pickle /etc/pickle/sshgw/terminal_ed25519_key /etc/pickle/sshgw/terminal_ed25519_key.pub
chmod 600 /etc/pickle/sshgw/terminal_ed25519_key
chmod 644 /etc/pickle/sshgw/terminal_ed25519_key.pub

# 3d. WireGuard endpoint. Generate the private key once (never printed/stored
# off-box); wg0.conf ships with the [Interface] populated and the [Peer] block
# commented until the Lightsail relay exists. The interface comes up
# address-only so the shim can bind 100.64.0.2 before the tunnel is peered.
umask 077
install -d -m 700 /etc/wireguard
if [ ! -f /etc/wireguard/wg0.privkey ]; then
  wg genkey > /etc/wireguard/wg0.privkey
  wg pubkey < /etc/wireguard/wg0.privkey > /etc/wireguard/wg0.pubkey
  echo "generated WireGuard keypair at /etc/wireguard/wg0.{privkey,pubkey}"
fi
if [ ! -f /etc/wireguard/wg0.conf ]; then
  WG_PRIV=$(cat /etc/wireguard/wg0.privkey)
  cat > /etc/wireguard/wg0.conf <<WGCONF
[Interface]
Address = 100.64.0.2/30
PrivateKey = ${WG_PRIV}
ListenPort = 51820

# --- Lightsail relay peer — uncomment and fill after the relay exists ---
# The campus side initiates outbound; PersistentKeepalive holds the NAT mapping.
#[Peer]
#PublicKey = <lightsail-wg-public-key>
#Endpoint = <lightsail-public-ip>:51820
#AllowedIPs = 100.64.0.1/32
#PersistentKeepalive = 25
WGCONF
  chmod 600 /etc/wireguard/wg0.conf
  echo "wrote /etc/wireguard/wg0.conf (peer block commented until the relay exists)"
  echo "  sshgw WireGuard public key (give to Lightsail): $(cat /etc/wireguard/wg0.pubkey)"
fi
systemctl enable -q wg-quick@wg0

# 3e. nftables — peer-only ingress. :22 (the shim) is reachable ONLY over wg0
# from the Lightsail transport IP; everything else is dropped. Network-layer
# half of contract condition #2 (the shim's REQUIRE policy + 100.64.0.2 bind
# are the app-layer half).
nft_tmp=$(mktemp)
cat > "$nft_tmp" <<'NFT'
#!/usr/sbin/nft -f
flush ruleset

table inet sshgw {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        ct state invalid drop
        iif "lo" accept
        ip protocol icmp accept
        ip6 nexthdr ipv6-icmp accept
        # SSH gateway: only from the WireGuard peer, only over wg0.
        iifname "wg0" ip saddr 100.64.0.1 tcp dport 22 accept
        # web-terminal bridge: WS ingress only from the LXC 100 TLS tier,
        # control only from pickle-api (LXC 101). vmbr1-internal, never DNAT'd.
        ip saddr 198.18.1.10 tcp dport 8082 accept
        ip saddr 198.18.1.20 tcp dport 8083 accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        ct state invalid drop
        # Relay port-forwarding data path: the WG peer may open new flows ONLY
        # toward the guest net (DNAT targets) and the api sync endpoint. The
        # peer's AllowedIPs is a route, not a port grant — these two rules ARE
        # the grant; keep them this narrow (no wider daddr, no extra dports).
        # iifname (not iif): this file loads at boot before wg0 exists; iif
        # resolves the ifindex at load time and would abort the whole ruleset.
        iifname "wg0" ip saddr 100.64.0.1 ip daddr 198.19.0.0/16 ct state new accept
        iifname "wg0" ip saddr 100.64.0.1 ip daddr 198.18.1.20 tcp dport 8080 ct state new accept
    }
    chain output  { type filter hook output priority filter; policy accept; }
}
NFT
if ! cmp -s "$nft_tmp" /etc/nftables.conf 2>/dev/null; then
  install -m 644 -o root -g root "$nft_tmp" /etc/nftables.conf
  echo "installed /etc/nftables.conf (peer-only :22)"
fi
rm -f "$nft_tmp"
systemctl enable -q nftables
systemctl restart nftables

# 3e-1. IP forwarding — the relay port-forwarding path routes wg0 → eth0 through
# this container. A runtime-only sysctl dies on reboot and forwarding then fails
# SILENTLY while SSH keeps working (sshpiperd proxies, it does not route), so
# persist it and let the reboot check below prove it.
if [ ! -f /etc/sysctl.d/99-pickle-forward.conf ]; then
  cat > /etc/sysctl.d/99-pickle-forward.conf <<'SYSCTL'
# Relay port-forwarding: this container routes relay-tunnel traffic to the
# guest net and the api sync endpoint (scoped by the nftables forward chain).
net.ipv4.ip_forward = 1
SYSCTL
  echo "persisted net.ipv4.ip_forward=1 (relay forwarding path)"
fi
sysctl -q -p /etc/sysctl.d/99-pickle-forward.conf

# 3f. systemd units, installed from the staged sshgw repo copies (see 2b):
#  - sshpiperd: loopback listener, lax PROXY trust for the shim only, our routing
#    plugin. Fail-closed — the plugin refuses to start without a token, so a
#    misconfigured unit does not open an unauthenticated gateway.
#  - sshgw-proxyfront: the :22 ingress shim. Needs wg0 up to bind 100.64.0.2
#    (Requires + After) and sshpiperd as its upstream (Wants + After).
#  - sshgw-terminal-bridge: browser-WS termination + VM SSH client, LXC-local.
#    Fail-closed: refuses to start without its tokens/key.
# Services are enabled but stay stopped until secrets and binaries are in place.
for src in /run/pickle-sshgw-units/*.service; do
  unit=$(basename "$src")
  if ! cmp -s "$src" "/etc/systemd/system/$unit" 2>/dev/null; then
    install -m 644 -o root -g root "$src" "/etc/systemd/system/$unit"
    systemctl daemon-reload
    echo "installed /etc/systemd/system/$unit"
  fi
  systemctl enable -q "$unit"
done
rm -rf /run/pickle-sshgw-units

echo "sshgw provisioning complete."
echo "REMAINING (not done by this script):"
echo "  1. deploy binaries -> /opt/pickle/sshgw/bin/{sshgw-proxyfront,sshgw-route-plugin,sshgw-terminal-bridge}"
echo "  2. set PICKLE_SSHGW_TOKEN and PICKLE_TERMINAL_CONTROL_TOKEN in /etc/pickle/sshgw.env"
echo "  3. copy the upstream PUBLIC key (printed above, or"
echo "     /etc/pickle/sshgw/upstream_ed25519_key.pub) into pickle-api's api.env as"
echo "     PICKLE_SSH_PLATFORM_PUBLIC_KEY (cloud-init injects it into every VM)"
echo "  3b. copy the terminal PUBLIC key (/etc/pickle/sshgw/terminal_ed25519_key.pub)"
echo "     into pickle-api's api.env as PICKLE_TERMINAL_PUBLIC_KEY, and the same"
echo "     PICKLE_TERMINAL_CONTROL_TOKEN value into api.env (api->bridge control)"
echo "  4. once the relay exists: uncomment/fill the [Peer] block in /etc/wireguard/wg0.conf, then"
echo "     systemctl restart wg-quick@wg0 sshpiperd sshgw-proxyfront"
EOS
)"

echo "pickle-sshgw LXC $CTID ready at 198.18.1.30 (services enabled, start after deploy)."
echo "next: run scripts/apply-log-retention.sh to reinstate the journald cap here."
