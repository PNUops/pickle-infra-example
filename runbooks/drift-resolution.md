# Runbook — drift finding resolution

Operational procedure for the three drift classes the **DriftReconciler**
(`@Recurring` id `drift-reconciler`, every 10 min) records into `drift_findings`.
The reconciler is the source of truth for "DB intent vs Proxmox reality"; it
**flags only and never destroys anything**. A human resolves each finding using
the admin API. This runbook covers investigation and the safe resolution for each
`DriftFindingKind`.

## Concepts
- **Kinds** (`DriftFindingKind`): `MISSING_IN_PROXMOX`, `UNMANAGED_GUEST`,
  `SPEC_MISMATCH`.
- **Status** (`DriftFindingStatus`): `OPEN`, `RESOLVED`. At most one OPEN finding
  per condition (partial unique index on `(kind, dedup_key) where status='OPEN'`).
- **Auto-resolve**: each cycle upserts every observed condition (bumps
  `last_seen_at`); at end of cycle any OPEN finding **not seen this cycle** is
  auto-closed (`resolved_by` null = auto). Fix the underlying condition and the
  finding closes itself on the next clean cycle — you rarely need to resolve by
  hand. **Exception:** `UNMANAGED_GUEST` auto-resolve is **skipped on an
  incomplete cycle** (a node was OFFLINE or its listing failed) to avoid
  flapping, so a stale unmanaged finding during a node outage is expected.

## Investigate
All admin drift endpoints are **SYS_ADMIN only**, under
`https://pickle.pusan.ac.kr/api/v1/admin/drift-findings`.
```
# list open findings (optionally filter by kind)
GET /api/v1/admin/drift-findings?status=OPEN
GET /api/v1/admin/drift-findings?status=OPEN&kind=MISSING_IN_PROXMOX
# each finding carries: kind, vm_id (null for UNMANAGED_GUEST), proxmox_vmid,
# node_name, summary (Korean), detail (jsonb), first_seen_at, last_seen_at.
```
Cross-check against Proxmox read-only on pve1: `qm list` / `qm config <vmid>` /
`pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -c "select id,status,proxmox_vmid,vcpu,memory_mb from vms where proxmox_vmid=<n>"`.

## MISSING_IN_PROXMOX — DB row exists, no matching guest
Detection: a non-DELETED/ERROR `vms` row with a `proxmox_vmid` has no qemu guest
in the cluster listing (matched by vmid, so a live-migrated VM is **not** falsely
flagged). Effect: the finding is recorded **and** the VM is CAS-parked to
**NEEDS_ADMIN** with `status_detail = "Proxmox에 VM 없음(드리프트)"`. This is the
only kind that changes VM state — and only to park it, never destroy.

Investigate the cause, then:
- **Node was OFFLINE / transient** (the reconciler scope-excludes offline nodes
  and holds their VM keys) → bring the node back; the finding clears on the next
  complete cycle. Do nothing destructive.
- **Guest genuinely gone** (accidental `qm destroy`, failed provision) → if the
  VM should die: `POST /api/v1/admin/vms/{vmId}/force-delete` with
  `{"confirmName":"<exact vm name>"}` (SYS_ADMIN; 202). This runs the delete job
  (ACPI→force-stop, destroy+purge on Proxmox — a no-op when the guest is already
  gone, identity re-checked so a foreign vmid is never killed), releases the IP to
  24h quarantine, and CAS-marks the row `DELETED`. The MISSING finding then
  auto-resolves.
- **A provisioning task is stuck in NEEDS_ADMIN** for this VM (see `GET /api/v1/admin/tasks`)
  and the guest can still be created → `POST /api/v1/admin/tasks/{taskId}/retry`
  (SYS_ADMIN; 202). Only **NEEDS_ADMIN** tasks are retryable (→ RETRYING; anything
  else returns 409 `TASK_NOT_RETRYABLE`).

## UNMANAGED_GUEST — pickle-tagged guest no DB row claims
Detection: a qemu guest carrying the managed `pickle` tag whose vmid is claimed by
**no** non-DELETED `vms` row. Effect: **finding only** (`vm_id` null,
`dedup_key = "vmid:<n>"`); the guest is **left untouched**. Because there is no DB
row, the API's force-delete cannot act on it.

Investigate: usually a leftover from a delete that destroyed the row but not the
guest, or a manually-cloned VM that kept the tag.
- Confirm it is truly orphaned: `qm config <vmid>` and check the vmid against the
  DB **including DELETED rows** (`select id,status from vms where proxmox_vmid=<n>`).
  A vmid matched only by a DELETED row is a genuine orphan.
- Safe resolution (last resort, manual — the API deliberately won't): after
  confirming, `qm stop <vmid>` then `qm destroy <vmid> --purge`. Double-check the
  vmid before destroying; there is no confirmName guard on `qm`.
- If instead the guest **should** be managed (DB lost its row), reconcile the DB
  rather than destroying — escalate; do not invent a row by hand without the
  provisioning pipeline's invariants.
The finding auto-resolves once the guest disappears or gets claimed **and** a
complete cycle runs (remember the incomplete-cycle skip during node outages).

## SPEC_MISMATCH — granted spec ≠ live guest
Detection: `resource.maxcpu != vms.vcpu` or `resource.maxmem != vms.memory_mb`.
Effect: finding recorded **and** an informational `status_detail` note prefixed
`"사양 불일치(드리프트)"` — **no state transition**, the VM keeps running.

Investigate: a manual `qm set` resize, or a resize that didn't fully apply.
- To converge on the granted spec: `qm set <vmid> --cores <n> --memory <MB>` (a
  memory change is hot; a core change may need a guest reboot to reflect). The
  note and finding auto-clear once specs agree (the reconciler only wipes the note
  if it is still a spec-drift note — it never clobbers a real pipeline error).
- If the live spec is the intended one, update the grant through the normal
  admin/resize flow so DB intent matches; do not hand-edit `vms`.

### Protection-flag variant (`:protection` dedup key, since 2026-07-20)
A SPEC_MISMATCH finding reading "보호 플래그 불일치" means the always-on platform
invariant broke: **every managed VM must carry PVE `protection: 1`** (armed at
provisioning; only the destroy pipeline clears it, immediately before a
destroy). The user-facing `deletion_protection` setting is unrelated — it is a
pickle-side logical gate and is never mirrored to the flag.
- Cause is out-of-band tampering (`qm set <vmid> --protection 0`) or a missed
  backfill. Resolution: `qm set <vmid> --protection 1`; the finding auto-resolves
  on the next clean cycle. The reconciler stays report-only and never re-arms
  the flag itself.

## Decision summary
| Situation | Action |
|---|---|
| NEEDS_ADMIN task, guest still creatable | task **retry** (`/admin/tasks/{id}/retry`) |
| DB row for a guest that is genuinely gone | **force-delete** (`/admin/vms/{id}/force-delete`, confirmName) |
| Tagged guest with no DB row (confirmed orphan) | **manual `qm destroy --purge`** (API can't act) |
| vcpu/mem drift | `qm set` back to grant (or update the grant) |
| protection flag cleared out-of-band | `qm set <vmid> --protection 1` (always-on invariant) |
| Node offline / transient | wait one full cycle; nothing destructive |

## Manual resolve (when a finding lingers after the condition is fixed)
Normally unnecessary (auto-resolve handles it). If a finding stays OPEN after you
have genuinely fixed the condition:
```
POST /api/v1/admin/drift-findings/{findingId}/resolve      # SYS_ADMIN; optional note body
```
Prefer fixing the underlying drift and letting the next clean cycle auto-resolve
— a hand-resolved finding that is still truly drifting will simply reopen.

## Safety invariants
- The reconciler never destroys and only ever transitions a VM to NEEDS_ADMIN (a
  safe park), so a false MISSING flag during a node blip cannot lose a VM.
- The only immediate-destroy admin op is force-delete: SYS_ADMIN-only and it
  refuses unless `confirmName` exactly equals the VM name (409
  `VM_CONFIRM_NAME_MISMATCH`). It is **not** cancelable — unlike a scheduled
  delete, which has the `vm_delete_grace_hours` window.
