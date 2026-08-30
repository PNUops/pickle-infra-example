# Runbook — new environment bring-up (bare host → working platform)

The through-line for standing the whole platform up from nothing: a bare
Proxmox-capable machine to a deployment that provisions VMs, serves the console,
answers SSH, and survives a reboot. Written 2026-08-07, after a reproduction
review asked "can a fresh session build this from the committed material alone?"
and the honest answer was **no** — the database bootstrap and the relay could be
completed from their runbooks, but the host and reverse-proxy skeleton under
them could not. This document is the order, the dependencies, and the values
that change per environment. It deliberately restates **no** procedure that
already exists — each step points at the runbook or script that owns it.
(Runbooks not carried in this sample copy are kept in the private repository;
they are named descriptively below.)

This runbook targets a Proxmox node. A host that is not a Proxmox node joins
the platform via [node-intake.md](node-intake.md) instead.

Three markers are used throughout, and they are the point of the document:

- **HUMAN** — only the operator can do this step: purchases, account creation,
  requests to the university, physical work. No script or session can do it, so
  it must be scheduled, not discovered mid-build.
- **BLOCKED** — no committed procedure exists for this step. The gap is stated,
  with what would close it. **Do not improvise these steps from memory or
  guesswork**; a wrong host or proxy skeleton fails in ways that look like
  faults in the layers above it.

- **UNVERIFIED** — the step is written but nobody has followed it on a machine
  that did not already have the thing. Steps 12 to 14 were walked end to end on
  2026-08-07 during a database rebuild; everything before them was reconstructed
  by reading the running host, which proves the destination and not the route.
  **The next real build is the verification.** Whoever walks it: keep this file
  open, and as each step completes either leave it alone or correct it on the
  spot, then drop the marker for that step. A correction made while the failure
  is in front of you is worth more than the same correction guessed at later —
  the two runbook errors this document already carries were both found that way,
  by running the procedure rather than reading it.

## Verification status

| Steps | State |
|---|---|
| 0, 1 | HUMAN, and partly undocumented — see their notes |
| 2 to 11 | **UNVERIFIED** — written from the running host, never walked on a blank one |
| 12 to 14 | Walked 2026-08-07: the database was dropped, replayed, bootstrapped by the five scripts, and proven with the smoke suite |

## The order

Steps must run top to bottom; each "needs" column names the hard dependency
that makes the order real. Details and gaps: the numbered notes below the table.

| # | Step | Procedure | Needs |
|---|---|---|---|
| 0 | **HUMAN** Acquire names, accounts, and access | note 0 — partially **BLOCKED** | — |
| 1 | **HUMAN** Physical host: disks, OS + Proxmox VE install, admin SSH port, campus network | note 1 — end state recorded, installer choices are not | 0 (campus IP, firewall request filed) |
| 2 | Host bridges, NAT, firewall | install [`hosts/pve-node/interfaces`](../hosts/pve-node/interfaces) adapted per the values table; hardening + post-reboot checklist: the network runbook (private repo) | 1 |
| 3 | Clone the workspace and unlock the secrets vault | note 3 | 1 |
| 4 | Proxmox API account, role, ACLs for the api | note 4 | 1 |
| 5 | App container (PostgreSQL + api + console nginx) | `scripts/create-app-lxc.sh`, then fill `/etc/pickle/api.env` from the vault (or issue fresh secrets) | 2, 3 |
| 6 | SSH gateway container (sshpiperd + WireGuard endpoint) | `scripts/create-sshgw-lxc.sh` — prints the WG public key the relay needs; fill `/etc/pickle/sshgw.env` | 2, 3 |
| 7 | **HUMAN** creates the relay instance; then bring it up | the relay bring-up runbook (private repo) end to end (WG pairing with step 6, HAProxy, firewall), agent via `scripts/deploy-relay.sh` | 0, 6 |
| 8 | Reverse-proxy container (public web entry) | note 8 — create the container, then its config arrives with the agent deploy (step 10) and the apply scripts (step 11); install the Origin CA wildcard pair per platform root | 2, 3, and 0 (certificates) |
| 9 | User VM templates | image-builder repo (public; on this host a workspace checkout), per-OS profiles → the template VMIDs in the values table; rebuild flow: the template rebuild runbook (private repo) | 2 |
| 10 | Deploy the services | `scripts/deploy-api.sh` (first api start = Flyway V1→latest), `deploy-console.sh`, `deploy-proxy-agent.sh`, `deploy-sshgw.sh` | 5, 6, 8; api.env filled |
| 11 | Ingress and host policy | `scripts/apply-terminal-ingress.sh` → `scripts/apply-main-domain-vhost.sh` (note 11) → `scripts/apply-tls-ciphers.sh`; then `scripts/apply-log-retention.sh` and `scripts/apply-ops-timers.sh` (note 11a) | 8, 10; 0 (DNS + firewall live, for the LE issuance) |
| 12 | Database bootstrap — inventory, settings, terms, OS catalog, relay token | [db-restore.md](db-restore.md) §Clean-slate bootstrap, the whole numbered order there; note 12 for the dev-profile clearing and the measured sequence | 5–10 done; 8 for the certificate row |
| 13 | First operating data | note 13 — enable an OS, switch the kill switches on, create ≥1 organisation and the spec presets, verify the admin account | 12 |
| 14 | Smoke tests, health snapshot, first backup | note 14 | 13 |

## Values that change per environment

Every literal this environment is built on, and every file or variable it
appears in. A new environment decides each value ONCE, in this table, before
step 1 — chasing them one failing script at a time is how a build stalls.

| Value | This environment | Where it appears |
|---|---|---|
| Guest thin pool and its volume group | `pve/data`, VG `pve` | `THINPOOL_LV` in `scripts/health-check.sh` (must be `vg/lv`; the VG is derived from it unless `THINPOOL_VG` is set too). A host whose guest storage is named differently reports a permanently red thin-pool row until this is set, and the script refuses a value that is only a volume group |
| LVM query bound | 10s (`LVM_TIMEOUT`) | `scripts/health-check.sh` — the ceiling on each `lvs`/`vgs` read, so a pool that has exhausted and suspended its volumes cannot hold the snapshot open. Raise it only if a healthy host genuinely needs longer |
| Campus/public IP the DNS records point at | held by the operator (DNS provider dashboard + university DNS) | public A records; the university firewall request (step 0); `MAIN_DOMAIN_PUBLIC_IP` in `scripts/health-check.sh` (**hardcoded default** — left alone, the health check expects another site's address every run); the relay's edge firewall in the relay bring-up runbook, which admits admin SSH from this `/32` only — a wrong value there locks the operator out of the relay |
| Host LAN address, gateway, NIC name | `192.0.2.10/24`, gw `192.0.2.1`, `nic0` | [`hosts/pve-node/interfaces`](../hosts/pve-node/interfaces) vmbr0 stanza |
| Infra bridge net (vmbr1) | `198.18.0.0/16`, host `.0.1`; proxy `.1.10`, app `.1.20`, sshgw `.1.30` | `hosts/pve-node/interfaces` (NAT/DNAT/FORWARD rules pin `.1.10`), `create-app-lxc.sh`, `create-sshgw-lxc.sh`, the reverse-proxy rebuild runbook (private repo) §1, `apply-terminal-ingress.sh`, `apply-main-domain-vhost.sh` (`PICKLE_PROXY_IP`, `PICKLE_HOST_PROBE_IP`); **and on the relay** — `lightsail/wireguard/wg0.conf.template` admits the api's `.1.20/32` through the tunnel, which is the same address `PICKLE_RELAY_SYNC_URL` names |
| Guest bridge net (vmbr2) | `198.19.0.0/16`, host `.0.1` | `hosts/pve-node/interfaces`; `PICKLE_POOL_CIDR` / `PICKLE_POOL_GATEWAY` / `PICKLE_POOL_RESERVED` (`apply-platform-inventory.sh`) |
| WireGuard transport net | `100.64.0.0/30` — relay `.1`, sshgw `.2` | `create-sshgw-lxc.sh`; `lightsail/wireguard/wg0.conf.template` (its `AllowedIPs` also carries the guest network and **the api's address**, so a wrong entry breaks relay sync rather than the tunnel); `lightsail/haproxy/haproxy.cfg.template` (`server sshgw 100.64.0.2:22`); `lightsail/nftables/nftables.conf`; `hosts/pve-node/interfaces` (the `/30` route and the `.1` FORWARD accept); `PICKLE_RELAY_SOURCE_IP` |
| Main entry domain | `pickle.pusan.ac.kr` | `apply-main-domain-vhost.sh` via `PICKLE_MAIN_DOMAIN`; `create-sshgw-lxc.sh` as `PICKLE_TERMINAL_CONSOLE_ORIGIN` — **the terminal bridge accepts this origin and no other, and that container is built at step 6, long before step 11**, so a stale value here kills the web terminal silently; `create-app-lxc.sh` console vhost `server_name`; `health-check.sh` (`PICKLE_DEV_DOMAIN` default, and the Let's Encrypt certificate path); `apply-tls-ciphers.sh`, whose before/after assertions demand a 200 from this name; smoke-test defaults (`BASE`); the host `/etc/hosts` hairpin entry |
| Platform root domain(s) | `pusan.dev` | `PICKLE_ROOT_DOMAIN` (`apply-platform-inventory.sh` **and** `apply-settings.sh` — same value, on purpose); `PICKLE_PROXY_AGENT_WILDCARD_CERTS` in the proxy agent's environment, whose format is `<root>=<crt>:<key>` and which the agent needs before it can render anything for that root (the proxy agent deploy runbook (private repo)); `ROOT` (`smoke-http-publish.sh`); cert path `/etc/nginx/pickle-certs/<root, dots as dashes>.{crt,key}`; the DNS zone |
| User SSH host | `ssh.example.dev` (DNS-only A record → relay static IP) | the relay bring-up runbook; api `PICKLE_SSH_HOST` (the override point) — **and the api falls back to a compiled-in default when it is blank**, in the meta endpoint and in notification text, so an unset variable does not fail, it advertises the wrong host; the console carries its own constant for the unauthenticated landing page, which its own comment admits is duplicated state |
| Relay public host (port forwarding) | `ssh.example.dev` — the same name as the user SSH host above, because both resolve to the relay | `PICKLE_RELAY_PUBLIC_HOST` (`apply-platform-inventory.sh`, required — no default, and no API writes the column) |
| Relay static IP + admin SSH | `198.51.100.10`, admin sshd ``:22``, key `$VAULT/lightsail-ssh.pem` | `RELAY_HOST` / `RELAY_SSH_PORT` / `RELAY_SSH_KEY` (`deploy-relay.sh`); **a second, differently named set** `PICKLE_RELAY_SSH_KEY` / `_USER` / `_PORT` (`apply-relay-token.sh`); `RELAY` (`smoke-ssh-gateway.sh`, in the step 14 set); sshgw `wg0.conf` `Endpoint`; the relay bring-up runbook (private repo) |
| Container IDs | proxy `100`, app `101`, sshgw `102` | `CTID` in the create, deploy and backup scripts, each defaulting to its own container; `PICKLE_PROXY_CTID` / `PICKLE_APP_CTID` in every apply script, including the two ingress ones that carried the numbers as literals until 2026-08-07; `PICKLE_TUNNEL_CTID` in `apply-relay-token.sh`, which is the sshgw container under a third name because it is used for its tunnel rather than its gateway role; literal in most runbooks |
| Template VMIDs | `1001`–`1005` (Ubuntu 24.04/26.04/22.04, Debian 13/12; `1000` is the retired predecessor) | `CATALOG` in `apply-os-catalog.sh` (name **and** VMID), image-builder per-OS profiles, the template rebuild runbook |
| Proxmox node name | `pve-node` | `PICKLE_NODE`; must be a SAN of the node API certificate **and** resolve inside LXC 101 (its `/etc/hosts` entry, written by `create-app-lxc.sh`) |
| Storage | `local-lvm` | `PICKLE_NODE_STORAGE`, the create scripts, template builds |
| Host admin SSH port | `22` | host sshd config (**no committed procedure sets it** — note 1); the firewall rules in `hosts/pve-node/interfaces` assume it |

## Notes per step

### 0. Acquire names, accounts, and access — HUMAN, partially BLOCKED

What a build needs to already hold before step 1: the university-side public IP
and inbound 80/443 opened to it; the main entry domain delegated; the platform
root domain, the SSH host domain and the relay host domain registered with
their zones on Cloudflare; a Cloudflare dashboard login able to create the
wildcard record and issue Origin CA certificates for each platform root; an AWS
account able to create the Lightsail relay; an SMTP sender (app password).

**BLOCKED — the acquisition paths are not recorded anywhere.** The values are
known; where each came from is not: no request channel or lead time for the
university IP/domain/firewall work, no registrar/renewal records for the
purchased domains, and no inventory of which external accounts exist or who
holds them. Until that is written down (it belongs with the credentials
records, as locations — never values), this step runs on the operator's memory.
Lead-time warning regardless: the university requests are measured in days to
weeks, so file them first.

### 1. Physical host — HUMAN, partially recovered

No procedure was ever committed for taking a bare machine to "step 2 can run".
The end state was read off the running host on 2026-08-07, so what this
deployment settled on is recorded below; the choices that produced it are not,
and the physical parts stay HUMAN in any case.

**Read off the live host — reproduce these:**

| | This host |
|---|---|
| Proxmox VE | 9.2 on Debian trixie |
| Repositories | `pve-no-subscription` enabled; the enterprise and Ceph sources disabled |
| Disks | two: a 977 GB device carrying the install, a 1.8 TB device **currently unused** |
| Storage | `local` (directory, on the 96 GB root LV) and `local-lvm` (LVM-thin, ~839 GB) — the names the platform's variables default to |
| Root/swap | 96 GB root LV, 8 GB swap LV, the rest given to the thin pool |
| Admin sshd | port 22, moved off the default in the private deployment |

**Still not recorded, and each needs a decision rather than a transcription:**

- The installer choices that produce that layout: filesystem, how much of the
  device the volume group takes, and how root and swap are sized against the thin
  pool. Sizing differs with the disk in the new machine, so this is a decision.
- What the second device is for. It has been attached and unused here long enough
  that the question is open rather than settled.
- Moving the admin sshd off its default port. The port itself is in the values
  table; **the change is the dangerous step in the whole build** — done wrong it
  ends the session that is doing it. Follow the pattern the relay bring-up runbook (private repo)
  uses for exactly this: add the new port alongside the old, prove the new one
  from a second session, and only then remove the old.
- The campus-side attach: the address, the firewall opening, and who grants it.
  That is note 0.

An early setup log exists but predates the 2026-07-08 network renumbering, so
following it produces a host with the user and infra networks swapped. Treat it
as procedure-shape only, never as values.

### 3. Workspace and vault

Every script in this repo assumes the workspace layout on the host:
`/srv/pickle/<repo>` checkouts of this repo and the service repos it deploys
(api, console, sshgw, proxy-agent, relay-agent, image-builder), plus the
git-crypt secrets vault at `$VAULT`. Clone them, then **HUMAN**:
the git-crypt key is held by the operator and must be transferred out of band —
without the unlocked vault, step 5 has no secrets to install and
`deploy-relay.sh` refuses (no SSH key). A brand-new environment that starts an
empty vault instead must issue every secret fresh and commit the new
locations; the secret-rotation runbook (private repo) lists what must exist.

### 4. Proxmox API account for the api

The api authenticates to Proxmox with a dedicated user and API token, authorized
by a custom role and four ACL grants. No committed procedure created these — only
the token *rotation* was written down (the secret rotation runbook (private repo)
§2b). The sequence below was **reconstructed on 2026-08-07 by reading the live
account back out of `pveum`**, so it reproduces exactly what this host runs
rather than what somebody remembers. Without it the api boots and every
provisioning call answers 403.

```sh
# 4a. the role. Every privilege the platform actually holds, and no more —
#     measured, not copied: the live role carried three more that were removed
#     and every path still passed, so they are not here. Datastore.Audit came
#     back on 2026-08-10: the console's storage-capacity surface reads
#     GET /nodes/{n}/storage, which that privilege alone answers.
pveum role add PickleProvisioner --privs \
  "Datastore.AllocateSpace,Datastore.Audit,SDN.Use,Sys.Audit,\
VM.Allocate,VM.Audit,VM.Clone,VM.Config.CPU,VM.Config.Cloudinit,\
VM.Config.Disk,VM.Config.Memory,VM.Config.Network,VM.Config.Options,\
VM.GuestAgent.Unrestricted,VM.PowerMgmt"

# 4b. the user. API-only: no password is set, so the account cannot log in to
#     the web UI at all and the token is its only credential.
pveum user add pickle@pve --comment "Pickle platform service account"

# 4c. the grants. Four paths, each propagating to children. Narrower than
#     granting on `/`: the platform never touches another storage or zone.
for path in /vms /nodes /storage/local-lvm /sdn/zones/localnetwork; do
  pveum acl modify "$path" --users pickle@pve --roles PickleProvisioner
done

# 4d. the token. `--privsep 0` makes it carry the user's permissions; with
#     privsep on it would have none of them and the first clone would 403.
#     The secret is printed once — capture it to a mode-600 file, never to
#     the terminal, and copy it into the api environment from there.
umask 077
pveum user token add pickle@pve pickle-api --privsep 0 --output-format json \
  > /root/pickle-api-token.json
```

This list is 15. The 2026-08-07 measurement started from a live role of 17 and
removed three — `VM.Console`, `VM.GuestAgent.Audit`, `Datastore.Audit` — after
provisioning (39/39), the web terminal, the SSH gateway and the node capacity
measurement all passed without them. Two stay removed; `Datastore.Audit`
returned on 2026-08-10 because the storage-capacity surface added a call
(`GET /nodes/{n}/storage`) that genuinely needs it — the re-grant follows the
same doctrine, matching the calls the platform makes rather than accumulating.
The console privilege is the clearest removal: nothing in the platform opens a
Proxmox console any more, because the web terminal reaches guests over SSH.

Two of the fifteen are worth naming because they are easy to trim and expensive
to miss. **`VM.GuestAgent.Unrestricted`** is what lets provisioning read the guest's
host keys through the agent; without it the pipeline parks every VM at the
host-key step. **`SDN.Use`** covers the bridge the guest NIC attaches to.

Verify before moving on — the token is the thing that actually has to work:

```sh
pveum acl list          # four rows, type=user, ugid pickle@pve
pveum user token list pickle@pve   # privsep 0
```

The ACLs live on the **user**, not the token, so a later token rotation does not
disturb them (the secret rotation runbook (private repo) §2b relies on that).

### 8. Reverse proxy from blank

the reverse-proxy rebuild runbook (private repo) *restores* nginx from the newest
backup archive, and it is honest that the archive is its source. That reads as a
dead end for a first environment, and this runbook said so until the live
container was inventoried on 2026-08-07. It is not: **every platform config file
on that container has a source in the repositories.** The archive is a
convenience for a rebuild, not the only way to the state.

| File | Written by |
|---|---|
| `conf.d/pickle-base.conf` | the proxy-agent's own deploy script (the websocket upgrade map and the `pickle.d` include glob the rendered vhosts need) |
| `conf.d/pickle-terminal.conf`, `stream-conf.d/*-sni.conf` | `apply-terminal-ingress.sh` — this is also what defines `$pickle_client_ip` and the stream SNI router that owns :443 and prepends the PROXY header |
| `conf.d/pickle-tls.conf` | `apply-tls-ciphers.sh` |
| `conf.d/pickle-ratelimit.conf`, `sites-available/pickle-main*.conf`, `pickle-reject*.conf`, `stream-conf.d/pickle-stream-limits.conf`, `snippets/proxy-common.conf` | `apply-main-domain-vhost.sh` |
| `pickle.d/<fqdn>.conf` | the proxy-agent at run time, one per published domain |

So the order for a blank container is: create it (Debian, nginx, the
infrastructure-bridge address from the values table), deploy the proxy agent
(step 10) so its base config lands, then run the three apply scripts (step 11).
Install the Origin CA wildcard pair per platform root at
`/etc/nginx/pickle-certs/<root, dots as dashes>.{crt,key}` — step 12's inventory
refuses without it, and the proxy agent needs it named in its own environment.

Two things are genuinely not in any repository, and both are specific to the
host this platform grew on rather than to the platform:

- the **legacy tenant vhost** that shares this proxy. A new environment has no
  such tenant and needs none of it — but see note 11, because one apply script
  asserts that tenant answers before it will run.
- `nginx.conf` itself, beyond the stock Debian file: the `stream {}` block that
  includes `stream-conf.d/`, and `worker_shutdown_timeout` set high enough that a
  reload does not sever web-terminal websockets. Both are one line each and named
  in `pickle-base.conf`'s own comments.

### 11. Ingress and host policy

Order inside the step matters and is stated in each script header: the
terminal-ingress plumbing writes no app vhost, so the platform answers nothing
until `apply-main-domain-vhost.sh` runs — it owns the final ingress state and
runs LAST among the vhost writers.

`apply-main-domain-vhost.sh` used to refuse on a new host: its main domain was a
literal and its pre-flight asserted that a tenant which predates the platform on
this proxy answers 200. Both are parameterised as of 2026-08-07 — the defaults
reproduce this deployment, and a new environment passes its own
`PICKLE_MAIN_DOMAIN` and `PICKLE_LEGACY_TENANT_HOST=none`. The other two scripts
in this step take the container id and addresses from the same variables.

Also required by the LE issuance inside it: the DNS record and the university
firewall opening from step 0 must already be live, and the pve-node `/etc/hosts`
hairpin entry (`198.18.1.10 pickle.pusan.ac.kr`) must exist for anything
on-host to reach the public name (campus NAT does not hairpin).

#### 11a. Timers and retention — easy to forget, invisible when forgotten

`apply-ops-timers.sh` and `apply-log-retention.sh` are named by **no other
procedure** — this step is their only home in any build order. Skip the first
and the new environment has no nightly DB backup, no health-check timer, no
failure markers, no login notice — and nothing will ever say so; skip the
second and journald/log growth is unbounded. Run both after the containers
exist; re-run the retention script after any container rebuild.

### 12. Database bootstrap

Follow [db-restore.md](db-restore.md) §Clean-slate bootstrap **in its numbered
order** — it carries the per-script environment tables and the
what-breaks-if-skipped column; none of that is repeated here. Two additions
from the bring-up actually performed on 2026-08-07:

- The as-measured sequence, end to end: stop the api → drop/create the
  database → `deploy-api.sh` (clean build, first start runs Flyway) → **on a
  dev-profile api only**: clear what the development seeder filled
  (`delete from user_consents; delete from terms_versions; delete from
  settings; delete from os_images;` — the seeder fills empty tables at first
  start, and the bootstrap scripts by design do not take rows over) →
  `apply-platform-inventory.sh` (`PICKLE_RELAY_PUBLIC_HOST` required) →
  `apply-settings.sh` → `apply-terms.sh` → `apply-os-catalog.sh` → enable an
  OS → switch the kill switches on → `scripts/apply-relay-token.sh` → create
  an organisation (console) → smoke.
- Whether a new environment runs the `dev` or `prod` profile is itself an
  **undecided criterion** — nothing states which profile a second host should
  run, and the clearing step above exists only because of `dev`. Decide the
  profile before this step and record the decision; on `prod` the development
  seeder does not run and the clearing step must be skipped.

### 13. First operating data

The clean-slate order ends with rows the scripts wrote; the platform is still
not usable until an operator adds what only the console/API can:

- **An organisation.** `vm_requests.org_id` is not null and nothing seeds an
  organisation in prod — with zero orgs, **nobody can request a VM** even
  though everything is green. Create at least one in the admin console.
- **Spec presets (flavors).** Also unseeded in prod; create them in the admin
  console (the request form is empty without them).
- **The initial SYS_ADMIN.** On `prod` the api seeds exactly one from
  `PICKLE_BOOTSTRAP_ADMIN_EMAIL` / `PICKLE_BOOTSTRAP_ADMIN_PASSWORD` in
  `/etc/pickle/api.env` and refuses to start on a missing/guessable value —
  so this is really a step-5 input; verify the login here. On `dev` the
  development seeder's accounts (`PICKLE_SEED_SYSADMIN_*`) exist instead.
- **Kill switches.** `ssh_gateway_enabled`, `web_terminal_enabled`,
  `port_forwarding_enabled` are written **off** by the settings script on
  purpose; switch each on only after its path is proven (the smoke tests in
  step 14 are the proof).

### 14. Prove it

Smoke tests run on the host as root (they need the guest bridge); defaults
target the main domain through the hairpin entry — see the smoke section of
[README.md](../README.md) for the prerequisites and the variables to override.
Minimum set for a new environment, in order: `smoke-provisioning.sh` (the
end-to-end proof), `smoke-llm-key-lifecycle.sh`, `smoke-http-publish.sh`, `smoke-ssh-gateway.sh`,
`smoke-web-terminal.sh`. Then `health-check.sh` for the snapshot, confirm the
backup timer's first run left a marker and a dump, and re-run the network
runbook's post-reboot checklist once after a deliberate host reboot — a
platform that only survives until the first reboot is not built yet.
