# Lightsail SSH relay (user-provisioned)

The external relay that gives users a stable public `:22` without any
campus-inbound port. **Not created by this repo's scripts** — the operator
provisions the AWS Lightsail instance (Seoul, Debian 13 (trixie), static public
IPv4, ~$5/mo) and applies the two templates here. Topology, transport IPs, and the
PROXY-protocol trust rule are described below; the relay's assigned public IP is
recorded with the operator (it fills the `ssh.example.ac.kr` A record).

## Topology

```
user ── :22 ──▶ HAProxy (mode tcp, send-proxy-v2)
                     │  10.100.100.2:22 over WireGuard
                     ▼
        WireGuard wg0 = 10.100.100.1/30  (ListenPort 51820)
                     │  outbound-initiated tunnel (campus dials out; relay has
                     ▼  no Endpoint — the campus peer roams in from behind NAT)
        sshgw LXC 10.100.100.2  →  sshgw-proxyfront :22  →  sshpiperd  →  VM
```

The relay is the WireGuard **listener**; the campus sshgw side is the
initiator (it holds `PersistentKeepalive` and dials the relay's public
`:51820`). HAProxy prepends a **PROXY v2** header so the sshgw shim recovers the
real client IP; the shim drops any connection that is not a valid PROXY v2
header from `10.100.100.1` (contract conditions #1–#4).

## Bring-up (after the instance exists)

1. Open the Lightsail firewall for **TCP 22** and **UDP 51820** (public);
   nothing else. Keep the instance's own admin SSH on a **different** port.
2. `apt-get install -y wireguard-tools haproxy`.
3. `wireguard/wg0.conf.template` → `/etc/wireguard/wg0.conf`: generate the
   relay keypair (`wg genkey | tee privkey | wg pubkey > pubkey`), fill
   `PrivateKey` and the sshgw peer `PublicKey` (printed by
   `create-sshgw-lxc.sh`). Give the relay **public** key + the relay's public
   IP back to the campus side to fill the `[Peer]` block in the sshgw
   `/etc/wireguard/wg0.conf`. `systemctl enable --now wg-quick@wg0`.
4. `haproxy/haproxy.cfg.template` → `/etc/haproxy/haproxy.cfg`;
   `systemctl enable --now haproxy`.
5. Point `ssh.example.ac.kr` (A record, DNS-only — a CDN cannot proxy SSH) at the relay public
   IP — only after end-to-end verification.

WireGuard keys live in `/etc/wireguard/` on each side (mode 600); the AWS/relay
SSH key is held by the operator on pve1. Never commit any of these values.
