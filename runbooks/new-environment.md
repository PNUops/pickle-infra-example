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

Two markers are used throughout, and they are the point of the document:

- **HUMAN** — only the operator can do this step: purchases, account creation,
  requests to the university, physical work. No script or session can do it, so
  it must be scheduled, not discovered mid-build.
- **BLOCKED** — no committed procedure exists for this step. The gap is stated,
  with what would close it. **Do not improvise these steps from memory or
  guesswork**; a wrong host or proxy skeleton fails in ways that look like
  faults in the layers above it.

## The order

Steps must run top to bottom; each "needs" column names the hard dependency
that makes the order real. Details and gaps: the numbered notes below the table.

| # | Step | Procedure | Needs |
|---|---|---|---|
| 0 | **HUMAN** Acquire names, accounts, and access | note 0 — partially **BLOCKED** | — |
| 1 | **HUMAN** Physical host: disks, OS + Proxmox VE install, admin SSH port, campus network | note 1 — **BLOCKED** | 0 (campus IP, firewall request filed) |
| 2 | Host bridges, NAT, firewall | install [`hosts/pve1/interfaces`](../hosts/pve1/interfaces) adapted per the values table; hardening + post-reboot checklist: the network runbook (private repo) | 1 |
| 3 | Clone the workspace and unlock the secrets vault | note 3 | 1 |
| 4 | Proxmox API account, role, ACLs for the api | note 4 — **BLOCKED** | 1 |
| 5 | App container (PostgreSQL + api + console nginx) | `scripts/create-app-lxc.sh`, then fill `/etc/pickle/api.env` from the vault (or issue fresh secrets) | 2, 3 |
| 6 | SSH gateway container (sshpiperd + WireGuard endpoint) | `scripts/create-sshgw-lxc.sh` — prints the WG public key the relay needs; fill `/etc/pickle/sshgw.env` | 2, 3 |
| 7 | **HUMAN** creates the relay instance; then bring it up | the relay bring-up runbook (private repo) end to end (WG pairing with step 6, HAProxy, firewall), agent via `scripts/deploy-relay.sh` | 0, 6 |
| 8 | Reverse-proxy container (public web entry) | the reverse-proxy rebuild runbook (private repo) — note 8, **BLOCKED** on a truly new host; install the Origin CA wildcard pair per platform root | 2, 3, and 0 (certificates) |
| 9 | User VM templates | image-builder repo (public; on this host a workspace checkout), per-OS profiles → the template VMIDs in the values table; rebuild flow: the template rebuild runbook (private repo) | 2 |
| 10 | Deploy the services | `scripts/deploy-api.sh` (first api start = Flyway V1→latest), `deploy-console.sh`, `deploy-proxy-agent.sh`, `deploy-sshgw.sh` | 5, 6, 8; api.env filled |
| 11 | Ingress and host policy | `scripts/apply-terminal-ingress.sh` → `scripts/apply-main-domain-vhost.sh` (note 11 — **BLOCKED** on a new host) → `scripts/apply-tls-ciphers.sh`; then `scripts/apply-log-retention.sh` and `scripts/apply-ops-timers.sh` (note 11a) | 8, 10; 0 (DNS + firewall live, for the LE issuance) |
| 12 | Database bootstrap — inventory, settings, terms, OS catalog, relay token | [db-restore.md](db-restore.md) §Clean-slate bootstrap, the whole numbered order there; note 12 for the dev-profile clearing and the measured sequence | 5–10 done; 8 for the certificate row |
| 13 | First operating data | note 13 — enable an OS, switch the kill switches on, create ≥1 organisation and the spec presets, verify the admin account | 12 |
| 14 | Smoke tests, health snapshot, first backup | note 14 | 13 |

## Values that change per environment

Every literal this environment is built on, and every file or variable it
appears in. A new environment decides each value ONCE, in this table, before
step 1 — chasing them one failing script at a time is how a build stalls.

| Value | This environment | Where it appears |
|---|---|---|
| Campus/public IP the DNS records point at | held by the operator (Cloudflare dashboard + university DNS) | Cloudflare A records; the university firewall request (step 0); nowhere in this repo |
| Host LAN address, gateway, NIC name | `192.0.2.10/24`, gw `192.0.2.1`, `nic0` | [`hosts/pve1/interfaces`](../hosts/pve1/interfaces) vmbr0 stanza |
| Infra bridge net (vmbr1) | `172.30.0.0/16`, host `.0.1`; proxy `.1.10`, app `.1.20`, sshgw `.1.30` | `hosts/pve1/interfaces` (NAT/DNAT/FORWARD rules pin `.1.10`), `create-app-lxc.sh`, `create-sshgw-lxc.sh`, the reverse-proxy rebuild runbook, `apply-terminal-ingress.sh`, `apply-main-domain-vhost.sh` (`HOST_PROBE_IP`) |
| Guest bridge net (vmbr2) | `172.29.0.0/16`, host `.0.1` | `hosts/pve1/interfaces`; `PICKLE_POOL_CIDR` / `PICKLE_POOL_GATEWAY` / `PICKLE_POOL_RESERVED` (`apply-platform-inventory.sh`) |
| WireGuard transport net | `10.100.100.0/30` — relay `.1`, sshgw `.2` | `create-sshgw-lxc.sh`, `lightsail/wireguard/`, `hosts/pve1/interfaces` (the `/30` route + the `.1` FORWARD accept), `PICKLE_RELAY_SOURCE_IP` |
| Main entry domain | `pickle.pusan.ac.kr` | `apply-main-domain-vhost.sh` (**literal `DOMAIN=`, no env override** — note 11), smoke-test defaults (`BASE`), the pve1 `/etc/hosts` hairpin entry |
| Platform root domain(s) | `pusan.dev` | `PICKLE_ROOT_DOMAIN` (`apply-platform-inventory.sh` **and** `apply-settings.sh` — same value, on purpose), cert path `/etc/nginx/pickle-certs/<root, dots as dashes>.{crt,key}`, the Cloudflare zone |
| User SSH host | `ssh.example.dev` (DNS-only A record → relay static IP) | the relay bring-up runbook; shown to users by the console |
| Relay public host (port forwarding) | `ssh.example.dev` — the same name as the user SSH host above, because both resolve to the relay | `PICKLE_RELAY_PUBLIC_HOST` (`apply-platform-inventory.sh`, required — no default, and no API writes the column) |
| Relay static IP + admin SSH | `198.51.100.10`, admin sshd `:22`, key `$VAULT/lightsail-ssh.pem` | `RELAY_HOST` / `RELAY_SSH_PORT` / `RELAY_SSH_KEY` (`deploy-relay.sh`), sshgw `wg0.conf` `Endpoint`, the relay bring-up runbook |
| Container IDs | proxy `100`, app `101`, sshgw `102` | `CTID` defaults in the two create scripts; `PICKLE_APP_CTID` / `PICKLE_PROXY_CTID` in every apply script; `RP=100` in `apply-main-domain-vhost.sh`; literal in most runbooks |
| Template VMIDs | `1001`–`1005` (Ubuntu 24.04/26.04/22.04, Debian 13/12; `1000` is the retired predecessor) | `CATALOG` in `apply-os-catalog.sh` (name **and** VMID), image-builder per-OS profiles, the template rebuild runbook |
| Proxmox node name | `pve1` | `PICKLE_NODE`; must be a SAN of the node API certificate **and** resolve inside LXC 101 (its `/etc/hosts` entry, written by `create-app-lxc.sh`) |
| Storage | `local-lvm` | `PICKLE_NODE_STORAGE`, the create scripts, template builds |
| Host admin SSH port | `22` | host sshd config (**no committed procedure sets it** — note 1); the firewall rules in `hosts/pve1/interfaces` assume it |

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

### 1. Physical host — HUMAN, BLOCKED

**BLOCKED — there is no committed procedure that takes a bare machine to "step
2 can run".** Missing: the Proxmox VE install choices, disk/storage layout
(what `local-lvm` is carved from), moving the admin sshd to its non-default
port, repository setup, and the campus-side network attach. An early setup log
exists but predates the 2026-07-08 network renumbering, so following it
produces a host with the user and infra networks swapped — treat any old
record as procedure-shape only, never as values. What would close this: a host
build runbook whose end state is exactly `hosts/pve1/interfaces` plus a
listening Proxmox API. The physical parts (racking, disks, cabling, BIOS) are
HUMAN in any case.

### 3. Workspace and vault

Every script in this repo assumes the workspace layout on the host:
`/root/pickle/<repo>` checkouts of this repo and the service repos it deploys
(api, console, sshgw, proxy-agent, relay-agent, image-builder), plus the
git-crypt secrets vault at `$VAULT`. Clone them, then **HUMAN**:
the git-crypt key is held by the operator and must be transferred out of band —
without the unlocked vault, step 5 has no secrets to install and
`deploy-relay.sh` refuses (no SSH key). A brand-new environment that starts an
empty vault instead must issue every secret fresh and commit the new
locations; the secret-rotation runbook (private repo) lists what must exist.

### 4. Proxmox API account for the api — BLOCKED

The api authenticates to Proxmox with a dedicated user + API token, authorized
by a custom role and ACLs. **BLOCKED — only the token *rotation* is written
down (in the secret-rotation runbook, private repo); the creation is not.** No
committed `pveum` sequence creates the user, defines the role's privilege set,
or grants the ACL paths; the role contents exist as a name in prose elsewhere
and nowhere as commands. Without this the api boots but every provisioning
call 403s. What would close it: the creation commands beside the rotation
procedure, so a rebuild and a rotation read from the same page. (The token's
`--privsep 0` in the rotation section is deliberate — the token must carry the
user's full permissions.)

### 8. Reverse proxy from blank — BLOCKED on a truly new host

The reverse-proxy rebuild runbook (private repo) is honest about its own limit:
it *restores* the nginx state (the SNI stream router, the include skeleton, the
pre-existing tenant vhost) from the newest backup archive, and that archive is
the **only** source of that skeleton. A new host has no archive. **BLOCKED —
nothing can produce the nginx skeleton from source.** The apply scripts layer
onto it (`apply-terminal-ingress.sh`, `apply-tls-ciphers.sh`,
`apply-main-domain-vhost.sh`) but none of them writes the base `nginx.conf`
stream block or the vhost include layout. What would close it: either commit
the skeleton as files this repo owns, or extend the rebuild runbook with a
from-source section. Until then: carry a current archive from the old host
(and for a first environment with no old host, this step cannot be completed
as documented). The Origin CA wildcard pair per platform root (from step 0)
installs at `/etc/nginx/pickle-certs/` either way — step 12's inventory
refuses without it.

### 11. Ingress and host policy

Order inside the step matters and is stated in each script header: the
terminal-ingress plumbing writes no app vhost, so the platform answers nothing
until `apply-main-domain-vhost.sh` runs — it owns the final ingress state and
runs LAST among the vhost writers.

**BLOCKED on a new host — `apply-main-domain-vhost.sh` will not run there.**
Two facts, both currently true and stated nowhere else: (1) its preflight
asserts the pre-existing tenant of THIS host's proxy answers 200 and aborts
otherwise — a new host without that tenant dies at the preflight; (2) the main
domain is a literal `DOMAIN=pickle.pusan.ac.kr` with no environment override,
unlike every other domain input in this repo. This is a known, accepted debt
(the production main domain is undecided; the plan is to swap it right before
launch); what would close it is parameterizing `DOMAIN` and making the tenant
assertion opt-in. Until then a new environment must edit the script copy it
runs — note that you did, and do not commit the edit as if general.

Also required by the LE issuance inside it: the DNS record and the university
firewall opening from step 0 must already be live, and the pve1 `/etc/hosts`
hairpin entry (`172.30.1.10 pickle.pusan.ac.kr`) must exist for anything
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
end-to-end proof), `smoke-http-publish.sh`, `smoke-ssh-gateway.sh`,
`smoke-web-terminal.sh`. Then `health-check.sh` for the snapshot, confirm the
backup timer's first run left a marker and a dump, and re-run the network
runbook's post-reboot checklist once after a deliberate host reboot — a
platform that only survives until the first reboot is not built yet.
