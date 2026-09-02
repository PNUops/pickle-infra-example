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
  scripts are unsafe against a second node — see "Never run" below. A `nodes`
  row that lands as ACTIVE changes every org's approval headroom at once, and
  one whose `disk_capacity_gb` is empty blanks the platform's disk capacity
  (the dashboard disk bar and trend) until it is measured.
- **Cluster membership is a separate decision.** The install is standalone.
  Joining a cluster is done later, while the node still has no guests, and only
  after the cluster shape has been decided.

## 1. Before the wipe: measured intake

Read-only, over the vendor account (a password account on a vendor image is
normal). Run it from pve-node, which has `sshpass`, and keep the password out of
the shell history: `read -rs SSHPASS; export SSHPASS`, then
`sshpass -e ssh <account>@<addr> ...`, then `unset SSHPASS`. Record what is,
not what was expected:

| Item | How | Why it matters here |
|---|---|---|
| Board, BIOS, serials | `/sys/class/dmi/id/{sys_vendor,product_name,bios_version}`, `dmidecode -t 1,3` (root) | Vendor images ship with placeholder serials; if so, identify the box by UUID and MAC and say so in the README row |
| GPU | `lspci \| grep -iE 'vga\|3d\|nvidia'`, `nvidia-smi -L` | The platform has no GPU resource model. A GPU decides BIOS settings before the install (§2) and whether a driver is installed at all |
| CPU and ISA level | `lscpu`, `/lib64/ld-linux-x86-64.so.2 --help \| grep x86-64-v` | The Rocky 10 template requires x86-64-v3; a lower node accepts the clone and the guest hangs silently |
| Memory | `free -g`, `dmidecode -t 17` (root) | Count empty slots: capacity is what `apply-platform-inventory.sh` measures at registration, so later upgrades mean a re-run |
| Disks | `lsblk -o NAME,SIZE,TYPE,ROTA,MODEL`, `nvme smart-log` or `smartctl` (root) | One consumer NVMe means the OS and the thin pool share a device with no power-loss protection; decide on a second or enterprise device **before** the install |
| NICs | `ip -br link`, `/sys/class/net/*/speed` | Note uncabled 10G ports; the guest VLAN trunk and migration traffic want them |
| Band and L2 | `ip -br addr`, `ip route`; from pve-node `ip neigh show <addr>` and `ping -c 1000 -i 0.01 -q <addr>` | Same L2 as pve-node makes a stretched guest VLAN a switch question; record max and mdev, not the mean |
| BMC | `ls -l /dev/ipmi0`, `ipmitool mc info`, `ipmitool lan print 1`, `ipmitool user list 1` (root) | Three answers: is the dedicated port on a network (an address of 0.0.0.0 on DHCP means no), which users exist, and who on the host can reach `/dev/ipmi0`. Never probe default credentials by logging in |
| sshd, listeners, time | `ss -tlnup`, `/etc/ssh/sshd_config*`, `timedatectl` | Snapshot only; the wipe replaces all of it |
| Host keys | `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub` | Recorded so the post-install keys are seen to be new |
| Address ownership | ask the network owner | Whether the campus address the vendor image carries is a static assignment to this host or a DHCP lease; the install makes it static, so it has to be the host's |

Record the results as a pre-wipe snapshot, marked as such, in the same unit of
work as the intake; the install rewrites every row.

While the vendor OS is still up and `ipmitool` is on it, this is the cheapest
moment to change the BMC `admin` password (`ipmitool user set password <id>`
over `/dev/ipmi0`, root). After the wipe it needs `apt install ipmitool` first.

Two things are worth pulling out of the vendor OS before it goes, because both
are gone the moment the installer writes a partition table:

- **The FRU.** `dmidecode` on these boards can be all placeholders — chassis,
  product and asset serials reading as one repeated digit string. `ipmitool fru`
  is where the real board serial and manufacturing date live, and a board serial
  is the only value an asset record can rely on. Capture it and write it down.
- **The partition table.** `sfdisk -d /dev/<disk>` is a few lines of text that
  reproduce the exact GPT the vendor shipped. Keeping it costs nothing and it is
  the one artefact a bare-metal restore cannot reconstruct by guessing.

## 1b. Preserving the delivered state

Decide deliberately whether the vendor's OS is worth keeping, and do not confuse
the three ways of keeping it. They cost very different amounts and preserve very
different things.

**First, ask the vendor for the source image.** These machines usually ship with
a customised installer image (a Cubic build, an OEM preseed) and its build date
is stamped in `/etc/lsb-release`. The vendor has that ISO; it is cleaner than
anything cloned off a running disk, it costs nothing to request, and storing it
is their problem rather than ours. Ask before spending an evening on a block
copy — this is the cheapest correct answer and it is the one most often skipped.

**Second, the text capture is usually what you actually wanted.** Package lists,
enabled units, network configuration, firmware versions, accounts, the FRU and
the partition table answer nearly every question a restore was going to answer,
and they diff cleanly against a later state. A block image answers "boot exactly
this again", which is a much narrower need.

**Third, if a block image is genuinely wanted**, note that the disk is mostly
empty: a fresh vendor install is tens of gigabytes on a terabyte device, so copy
used blocks, not the device.

Offline, the accurate way, from live media (SystemRescue and Clonezilla both
carry `partclone`), with a target host that has the room:

```bash
sfdisk -d /dev/nvme0n1 > gpt.txt                     # keep with the image
dd if=/dev/nvme0n1p1 bs=1M | zstd -T0 | ssh <target> 'cat > esp.img.zst'
partclone.ext4 -c -s /dev/nvme0n1p2 | zstd -T0 | ssh <target> 'cat > root.pcl.zst'
```

Online, without live media, when the machine is about to be wiped anyway and a
crash-consistent copy is acceptable insurance (ext4 replays its journal on
restore; a file being written at that instant is the risk you accept):

```bash
sudo partclone.ext4 -c -s /dev/nvme0n1p2 --force | zstd -T0 | ssh <target> 'cat > root.pcl.zst'
```

Restoring is the same three steps in reverse — write the GPT, `dd` the ESP back,
`partclone.ext4 -r` the root — followed by fixing the EFI boot entry, which the
Proxmox installer will have replaced.

Two rules about where the image goes. **Compress on the source**, or the whole
device crosses the network uncompressed. And **the image contains the vendor
account's password hash and every key on the box**: keep it on a host the
platform owns, never in a repository, and record its location and checksum in
the host records rather than the image itself.

## 2. Decisions the install needs (operator)

Answer before booting the installer; each one changes what the installer is
told or what is cabled first:

1. **Storage layout.** A second or enterprise NVMe goes in before the install.
   Record the installer's disk values (`swapsize`, `maxroot`, `minfree`,
   `maxvz`) at the prompt: pve-node's were never recorded and its VG ended with
   16 GiB free. Keep the storage id `local-lvm`, which the registration script
   defaults to.
2. **Management NIC and 10G.** If the guest VLAN trunk will ride the 10G
   ports, cable them first and pick the 1G port as management.
3. **Hostname.** Provisional names are provisional; the name becomes the
   pveproxy certificate SAN and the `api_host` the api pins, so changing it
   after registration is a certificate and a database change.
4. **BMC.** Change the `admin` password **before** the dedicated port is
   cabled: on DHCP the BMC appears on the campus band the moment the link is
   up. Decide the management network first, then cable.
5. **BIOS and GPU.** A host with a GPU needs the BIOS choices made before the
   installer boots if passthrough is ever wanted (VT-d/IOMMU, Above-4G
   decoding, SR-IOV where offered, Secure Boot off) and a kernel command line
   with vfio at first boot. If no use is decided, leave IOMMU on and install no
   driver: a driver bound to the card is what passthrough later has to undo.
6. **Cluster shape.** Not needed for the install; needed before any join. A
   join requires the same Proxmox major as pve-node (`pveversion`).

## 3. Install (standalone) — UNVERIFIED, written ahead of the first walk

1. Boot the Proxmox VE installer of the same major as pve-node, choose the disk
   layout decided above, set the hostname, the 1G management interface, and
   the campus address the host already had (confirmed as the host's in §1).
2. First boot: set the repositories the way pve-node has them (enterprise and Ceph
   lists disabled, `pve-no-subscription` enabled, per
   [new-environment.md](new-environment.md)), then confirm `timedatectl` is
   synchronized against the source pve-node uses (read it off pve-node with
   `chronyc sources`), and that `pveproxy` answers on `:8006`.
3. Operator key first: one ed25519 key per node, comment `pickle-node-<name>`,
   kept in the operator's encrypted key archive as
   [node-intake.md](node-intake.md) §2 describes; `~/.ssh/config` entry with
   the ProxyJump through pve-node spelled out (user, port, key), since campus
   addresses are not reachable from a development machine directly. Prove it
   from a second session: `ssh -o BatchMode=yes <name> hostname`.
4. Only then sshd: key-only for `root`, `PasswordAuthentication no`, with the
   proving session kept open until the change is verified from another one
   (the pattern [new-environment.md](new-environment.md) prescribes for the
   port move). Whether the port stays 22 or moves like pve-node's is a per-host
   choice; write it into this README's `## 운영 대상` row, because every later
   command reads the port from the host records.
5. Firewall: do **not** copy pve-node's `interfaces` file wholesale. Its 8006 rule
   only restricts the infra bridge (`-i vmbr1`); on the campus side pve-node's
   8006 is reachable from the whole campus band today, and a new host with no
   infra bridge would carry the same posture. Decide explicitly whether that
   is acceptable here, remembering that once the node is registered the api on
   pve-node has to reach this 8006 over the campus network.
6. Do not create `vmbr2` yet. A guest bridge on this host is a separate L2
   from pve-node's until a VLAN trunk or an SDN zone joins them; creating it
   early invites a pool to be pointed at it.
7. Do not build templates yet if the node may join a cluster: a joining node
   must hold no guests.

## 4. Never run against a new node before registration is redesigned

Neither script can run *on* the new node (no LXC 100/101 there); the dangerous
invocation is on pve-node with `PICKLE_NODE=<new node>`.

- `apply-platform-inventory.sh` — the guards that stop it today are the SAN
  check against the local pveproxy certificate, the hosts-file lookup inside
  LXC 101 and the node-status query; after a cluster join, a certificate
  override and a hosts entry all three can be satisfied, and then the pool
  guard is the last one and it catches a *changed* CIDR but not a *shared*
  one, so the run binds the node to pve-node's pool.
- `apply-os-catalog.sh` with `PICKLE_NODE=<new node>` — the catalog rows are
  unique on (name, version) and the upsert rewrites `node_id` while never
  touching status, so it **moves** every catalog row off pve-node as ACTIVE rows
  of the new node. Placement then sends every new VM to that node and the
  clone fails there (the template VMIDs live on pve-node): provisioning is broken
  platform-wide until the rows are moved back.
- Any manual insert into `nodes`. If one is unavoidable, PATCH it to
  MAINTENANCE immediately.
- `pvecm create` on pve-node, or `pvecm add` anywhere, before the cluster shape
  is decided. Before a `pvecm create` on pve-node is ever run, verify on a
  throwaway host whether cluster creation regenerates the pveproxy certificate
  or CA, because the api pins pve-node's CA and would lose Proxmox the moment it
  changed.

## 5. What this repository keeps

- A row for the node in the `## 운영 대상` block of the [README](../README.md),
  in the same unit of work as the intake.
- `hosts/<name>/` and an apply script — only when the first config artifact
  exists (the install's `interfaces` and sshd choices are the first
  candidates), per the README's 관례 section.
- The registration tooling this runbook says does not exist yet arrives with
  the round that builds it, and this runbook gains a §6 then.
