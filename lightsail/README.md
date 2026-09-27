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
                     │  100.64.0.2:22 over WireGuard
                     ▼
        WireGuard wg0 = 100.64.0.1/30  (ListenPort 51820)
                     │  outbound-initiated tunnel (campus dials out; relay has
                     ▼  no Endpoint — the campus peer roams in from behind NAT)
        sshgw LXC 100.64.0.2  →  sshgw-proxyfront :22  →  sshpiperd  →  VM
```

릴레이가 WireGuard **listener**이고 캠퍼스 sshgw가 `PersistentKeepalive`를 유지하며
릴레이의 공개 `:51820`으로 연결합니다. HAProxy는 **PROXY v2** 헤더에 실제 클라이언트 IP를
담습니다. sshgw 방화벽은 `wg0`의 릴레이 피어 `100.64.0.1`에서 오는 `:22` 연결만
허용하며, shim은 유효한 PROXY v2 헤더가 없는 연결을 SSH 배너 없이 끊습니다. 헤더가
잘못되거나 송신자가 릴레이 피어가 아니면 TCP 송신자 IP를 클라이언트 IP로 대신 신뢰하지
않고 연결을 끊습니다. 내부 브리지에서 직접 연결하거나 비허용 송신자가 헤더를 위조한
경우와 릴레이 피어가 손상된 헤더를 보낸 경우도 거부되는지 확인해야 합니다.

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
SSH key is held by the operator on pve-node. Never commit any of these values.
