# Runbook — non-Proxmox node intake

Brings a host that is not a Proxmox node into the platform: measured intake,
operator access over SSH, and the records this repository keeps. First walked
2026-08-24 for `gpu-node` (GPU serving box) and `dept-node` (department server).

Intake stops before any service lands on the box. Serving configuration, deploy
scripts, and per-host directories under `hosts/<name>/` arrive with the round
that first needs them. A Proxmox node is a different procedure entirely: one
added to the platform that already exists follows
[proxmox-node-intake.md](proxmox-node-intake.md), and a whole new platform on
a blank host follows [new-environment.md](new-environment.md).

Two boundaries hold throughout:

- **The platform inventory is for Proxmox nodes only.** A non-Proxmox host is
  never registered by `apply-platform-inventory.sh` and never becomes a VM
  placement candidate — the inventory row's API endpoint, bridge, and storage
  fields only mean something on a hypervisor.
- **A shared host is changed as little as possible.** If the box already runs
  other people's workloads, intake adds one `authorized_keys` line and reads
  everything else. No sshd edits, no package installs, no firewall changes.

## 1. Access path

Decide how the operator reaches the box from a development machine and write it
into `~/.ssh/config` there. Every later command uses this alias, so the
transport (port, jump, key) is defined in exactly one place.

- Directly reachable (has a DNS name or routable address): a plain `Host` entry.
- Campus-band only: hop through the Proxmox host — spell the jump's user and port out, so
  the entry works on a machine with no other pickle SSH config:

```
Host <name>
  HostName <campus address>
  User <account>
  ProxyJump root@pve-node:22
  IdentityFile <path to the node key — filled in after §2>
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

`accept-new` records the host key on first contact; a later mismatch is a
signal, not an inconvenience.

## 2. Operator key

One dedicated ed25519 key per node, comment `pickle-node-<name>`. No
passphrase — the key is used through jump paths and checks where nothing can
type one; the compensating control is that the copy of record lives only in the
operator's encrypted key archive, never on the node and never in this
repository. Generate it there:

```bash
ssh-keygen -t ed25519 -N "" -C "pickle-node-<name>" -f <name>
```

Take a fingerprint baseline of what is already on the box, then install
append-only. When only password auth exists yet, the operator runs the install
personally so the password is typed interactively and stored nowhere:

```bash
ssh <name> "ssh-keygen -lf ~/.ssh/authorized_keys"   # baseline (may fail if empty)
ssh-copy-id -i <name>.pub <name>
```

Then verify the key stands on its own, and that the delta against the baseline
is exactly the new fingerprint:

```bash
ssh -o BatchMode=yes <name> hostname
ssh <name> "ssh-keygen -lf ~/.ssh/authorized_keys"
```

Existing keys stay untouched. Disabling password auth, changing the account
password, or any other sshd change is a separate decision by the operator —
never part of intake, and on a shared host not this platform's call at all.

**Leaving**: delete the `pickle-node-<name>` line from `~/.ssh/authorized_keys`
on the node and re-run the fingerprint listing — that single line is everything
intake placed on the box.

## 3. Measured intake

Read-only, over the new key. Record what is, not what was expected:

| Item | How |
|---|---|
| Hostname, OS, kernel, architecture | `hostnamectl` |
| Hardware model | `cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name` |
| CPU, memory | `lscpu`, `free -h` |
| Disks and mounts | `lsblk -d -o NAME,SIZE,TYPE,MODEL`, `df -h` |
| GPU | `lspci \| grep -iE 'vga\|3d\|nvidia'`, `nvidia-smi -L` |
| Interfaces, routes | `ip -br addr`, `ip route` — note which campus band, and whether it shares a link with the Proxmox host |
| sshd posture | port and `PasswordAuthentication` from `/etc/ssh/sshd_config*` (absent means the default, yes) |
| Listening ports | `ss -tln` |
| Time sync | `timedatectl` |
| Existing tenants (shared host) | `ls /home`, `docker ps`, running services — recorded, never touched |

An aarch64 box breaks x86 assumptions quietly (container images, prebuilt
binaries, build toolchains); record the architecture prominently.

## 4. Out-of-band management plane

Every server-class box may carry a BMC that bypasses sshd, firewall, and every
platform control. At intake, answer three questions and record the answers even
when they are "none" or "unknown":

1. **Present?** `dmidecode -t 38` is the authoritative probe, and it **needs root** —
   on a shared host, the case where root is least likely, the question goes to the
   owner rather than staying unanswered. `ls /sys/class/ipmi/` needs no privilege but
   only shows a *registered* device, so it reads empty on a box whose driver never
   bound one; treat an empty result as "not registered", not as "not present". An
   OS-side pass-through interface is the other tell (some vendors expose one
   on a link-local address, named after the controller).
2. **Reachable, and from where?** Two paths, and the second is the one that gets
   forgotten. Whether the dedicated BMC port is cabled to a network is a physical
   question for the box's owner. But if the pass-through interface is up, **anyone
   with a shell on this host reaches the BMC's authentication surface** — no
   firewall sits on that link, and on a shared box that means every account on it.
   Record both, because an uncabled dedicated port does not close the second path.
3. **Default credentials survived?** Never probed by logging in. On a host the
   platform owns, the operator checks; on a shared host, the question is routed
   to the box's owner.

## 5. What this repository keeps

- A row for the node in the `## 운영 대상` block of the [README](../README.md),
  in the same unit of work as the intake.
- `hosts/<name>/` and an apply script — only when the first config artifact
  exists, per the pattern the README's 구성 section states.
