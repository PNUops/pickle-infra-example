# Runbook — Proxmox node intake (draft)

Brings a server the platform owns into the fleet as a **Proxmox placement node
candidate**: measured intake before the OS wipe, the install itself, and the
records this repository keeps. First walked 2026-09-01 for two internal
server-room hosts (`192.0.2.30` and `192.0.2.31`, provisional names
`pve-node-2` and `pve-node-3`); the install step is written ahead of being
walked and is marked so.

This is the counterpart of [node-intake.md](node-intake.md), which covers a
host that is *not* a Proxmox node. [new-environment.md](new-environment.md)
brings up a whole platform on a blank host; this runbook adds a node to a
platform that already exists, and **stops before the node is registered**.

Two boundaries hold throughout:

- **Intake and install do not register the node.** No `nodes` row, no OS
  catalog row, no pool. Registration needs tooling that does not exist yet
  (a node-only registration script and a per-node OS catalog), and the current
  scripts are unsafe on a second node — see "Never run" below. A `nodes` row
  that lands as ACTIVE changes every org's approval headroom at once.
- **Cluster membership is a separate decision.** The install is standalone.
  Joining a cluster is done later, while the node still has no guests, and only
  after the cluster shape has been decided.

## 1. Before the wipe: measured intake

Read-only, over the vendor account (a password account on a vendor image is
normal; use `sshpass -e` from the existing Proxmox host rather than typing it
into anything that records). Record what is, not what was expected:

| Item | How | Why it matters here |
|---|---|---|
| Board, BIOS, serials | `/sys/class/dmi/id/{sys_vendor,product_name,bios_version}`, `dmidecode -t 1,3` (root) | Vendor images ship with placeholder serials; if so, identify the box by UUID and MAC and say so in the records |
| CPU and ISA level | `lscpu`, `/lib64/ld-linux-x86-64.so.2 --help \| grep x86-64-v` | The Rocky 10 template requires x86-64-v3; a lower node accepts the clone and the guest hangs silently |
| Memory | `free -g`, `dmidecode -t 17` (root) | Count empty slots: capacity is what `apply-platform-inventory.sh` measures at registration, so later upgrades mean a re-run |
| Disks | `lsblk -o NAME,SIZE,TYPE,ROTA,MODEL`, `nvme smart-log` or `smartctl` (root) | One consumer NVMe means the OS and the thin pool share a device with no power-loss protection; decide on a second or enterprise device **before** the install |
| NICs | `ip -br link`, `/sys/class/net/*/speed` | Note uncabled 10G ports; the guest VLAN trunk and migration traffic want them |
| Band and L2 | `ip -br addr`, `ip route`; from the existing host `ip neigh show <addr>` and `ping -c 1000 -i 0.01 -q <addr>` | Same L2 as the existing host makes a stretched guest VLAN a switch question; record max and mdev, not the mean |
| BMC | `ls -l /dev/ipmi0`, `ipmitool mc info`, `ipmitool lan print 1`, `ipmitool user list 1` (root) | Three answers: is the dedicated port on a network (an address of 0.0.0.0 on DHCP means no), which users exist, and who on the host can reach `/dev/ipmi0`. Never probe default credentials by logging in |
| sshd, listeners, time | `ss -tlnup`, `/etc/ssh/sshd_config*`, `timedatectl` | Snapshot only; the wipe replaces all of it |
| Host keys | `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub` | Recorded so the post-install keys are seen to be new |

Record the results as a pre-wipe snapshot, marked as such, in the same unit of
work as the intake; the install rewrites every row.

## 2. Decisions the install needs (operator)

Answer before booting the installer; each one changes what the installer is
told or what is cabled first:

1. **Storage layout.** A second or enterprise NVMe goes in before the install.
   Record the installer's disk values (`swapsize`, `maxroot`, `minfree`,
   `maxvz`) at the prompt: the first host's were never recorded and its VG
   ended with 16 GiB free. Keep the storage id `local-lvm`, which the
   registration script defaults to.
2. **Management NIC and 10G.** If the guest VLAN trunk will ride the 10G
   ports, cable them first and pick the 1G port as management.
3. **Hostname.** Provisional names are provisional; the name becomes the
   pveproxy certificate SAN and the `api_host` the api pins, so changing it
   after registration is a certificate and a database change.
4. **BMC.** Change the `admin` password **before** the dedicated port is
   cabled: on DHCP the BMC appears on the campus band the moment the link is
   up. Decide the management network first, then cable.
5. **Cluster shape.** Not needed for the install; needed before any join.

## 3. Install (standalone) — UNVERIFIED, written ahead of the first walk

1. Boot the Proxmox VE installer, choose the disk layout decided above, set
   the hostname, the 1G management interface, and the campus address the
   host already had.
2. First boot: confirm `timedatectl` is synchronized against the same source
   the existing host uses, and that `pveproxy` answers on `:8006`.
3. sshd: key-only for `root`, `PasswordAuthentication no`. Whether the port
   stays 22 or moves to a non-standard one is a per-host choice; write it
   into the host records, because every later command reads it from there.
4. Operator key: one ed25519 key per node, comment `pickle-node-<name>`, kept
   in the operator's encrypted key archive as [node-intake.md](node-intake.md)
   §2 describes; `~/.ssh/config` entry with the ProxyJump through the existing
   host spelled out (user, port, key), since campus addresses are not
   reachable from a development machine directly.
5. Firewall: do **not** copy the first host's `interfaces` file wholesale.
   Its 8006 rule only restricts the infra bridge (`-i vmbr1`), so on a host
   with no such bridge the API port is open to the whole campus band. Decide
   that explicitly.
6. Do not create `vmbr2` yet. A guest bridge on this host is a separate L2
   from the first host's until a VLAN trunk or an SDN zone joins them;
   creating it early invites a pool to be pointed at it.
7. Do not build templates yet if the node may join a cluster: a joining node
   must hold no guests.

## 4. Never run on a new node before registration is redesigned

- `apply-platform-inventory.sh` — requires the app LXC locally, and its pool
  guard catches a *changed* CIDR but not a *shared* one: a run that got past
  `require_ct` would bind the node to the first host's pool.
- `apply-os-catalog.sh` with `PICKLE_NODE=<new node>` — the catalog rows are
  unique on (name, version) and the upsert rewrites `node_id`, so it **moves**
  every catalog row off the first host instead of adding rows for the new
  node.
- Any manual insert into `nodes`. If one is unavoidable, PATCH it to
  MAINTENANCE immediately.
- `pvecm create` on the first host, or `pvecm add` anywhere, before the
  cluster shape is decided.

## 5. What this repository keeps

- A row for the node in the `## 운영 대상` block of the [README](../README.md),
  in the same unit of work as the intake.
- `hosts/<name>/` and an apply script — only when the first config artifact
  exists (the install's `interfaces` and sshd choices are the first
  candidates), per the README's 관례 section.
- The registration tooling this runbook says does not exist yet arrives with
  the round that builds it, and this runbook gains a §6 then.
