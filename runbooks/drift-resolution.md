# 드리프트 발견 항목 해소

DriftReconciler(`@Recurring` id `drift-reconciler`, 10분 주기)가 `drift_findings`에 기록하는
Proxmox 드리프트 3종의 조사와 대응 절차예요. 리컨실러는 "DB의 의도와 Proxmox의 현실"을
대조하며 게스트를 파괴하지 않아요. `DriftFindingKind`별로 원인을 조사하고 관리 API나 수동 절차로 처리해요.

## 개념

- **종류**(`DriftFindingKind`): `MISSING_IN_PROXMOX`, `UNMANAGED_GUEST`, `SPEC_MISMATCH`.
- **상태**(`DriftFindingStatus`): `OPEN`, `RESOLVED`. 한 조건의 OPEN 항목은 최대 하나예요.
- 원인 조건을 고치면 다음 온전한 주기에 항목이 자동으로 닫혀요. 다만 `UNMANAGED_GUEST`는
  노드가 OFFLINE이거나 목록 조회가 실패해 주기가 불완전하면 자동 해소를 건너뛰어요.
  노드 장애 중 오래된 unmanaged 항목이 남아 있는 것은 정상이에요.

## 조사

관리 드리프트 엔드포인트는 모두 **SYS_ADMIN 전용**이며 `https://pickle.pusan.ac.kr/api/v1/admin/drift-findings` 아래에 있어요.

```
# OPEN 항목 목록 (선택: 종류 필터)
GET /api/v1/admin/drift-findings?status=OPEN
GET /api/v1/admin/drift-findings?status=OPEN&kind=MISSING_IN_PROXMOX
# 각 항목이 담는 것: kind, vm_id(UNMANAGED_GUEST면 null), proxmox_vmid,
# node_name, summary(한국어), detail(jsonb), first_seen_at, last_seen_at
```

pve-node에서 Proxmox를 읽기 전용으로 대조해요. `qm list`, `qm config <vmid>`,
`pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -c "select id,status,proxmox_vmid,vcpu,memory_mb from vms where proxmox_vmid=<n>"`.

## MISSING_IN_PROXMOX(DB 행은 있으나 게스트 없음)

DELETED/ERROR가 아니고 `proxmox_vmid`가 있는 `vms` 행에 대응하는 qemu 게스트가 클러스터
목록에 없으면 기록해요. vmid로 대조하므로 라이브 마이그레이션된 VM을 잘못 표시하지 않아요.
동시에 VM을 CAS로 NEEDS_ADMIN에 주차해요(`status_detail = "Proxmox에 VM 없음(드리프트)"`).
이 세 종류 중 VM 상태를 바꾸는 유일한 경우이며, 주차만 하고 파괴하지 않아요.

원인을 조사한 뒤 처리해요.

- **노드가 OFFLINE이거나 일시적인 경우**: 리컨실러는 해당 노드를 범위에서 빼고 VM 키를
  보류해요. 노드가 돌아온 뒤 온전한 주기가 돌면 항목이 사라져요. 파괴적인 조치는 하지 않아요.
- **게스트가 실제로 사라진 경우**: 실수로 `qm destroy`했거나 프로비저닝이 실패했는지 확인해요.
  VM을 삭제해야 한다면 `POST /api/v1/admin/vms/{vmId}/force-delete`에
  `{"confirmName":"<VM 이름 그대로>"}`를 보내요(SYS_ADMIN, 202). 비동기 삭제 잡이 ACPI 종료와
  필요시 강제 정지, Proxmox destroy와 purge를 수행해요. 게스트가 이미 없으면 건너뛰고,
  신원을 다시 확인해 다른 게스트를 지우지 않아요. IP를 24시간 격리로 반납하고 행을 CAS로
  `DELETED` 처리하면 MISSING 항목도 자동으로 닫혀요. 접수 즉시 파기가 끝났다는 뜻은 아니에요.
- **프로비저닝이 NEEDS_ADMIN에 멈췄고 게스트를 아직 만들 수 있는 경우**:
  `GET /api/v1/admin/tasks`에서 작업을 확인하고
  `POST /api/v1/admin/tasks/{taskId}/retry`를 호출해요(SYS_ADMIN, 202).
  **NEEDS_ADMIN** 작업만 RETRYING으로 전이하며 나머지는 409 `TASK_NOT_RETRYABLE`이에요.

## UNMANAGED_GUEST(어느 DB 행도 claim하지 않는 pickle 태그 게스트)

관리 대상 `pickle` 태그를 가진 qemu 게스트의 vmid를 DELETED가 아닌 `vms` 행이 하나도
claim하지 않으면 기록해요. `vm_id`는 null이고 `dedup_key = "vmid:<n>"`이며 게스트는
**건드리지 않아요.** DB 행이 없으므로 API force-delete도 사용할 수 없어요.

- `qm config <vmid>`를 읽고 vmid를 **DELETED 행까지 포함해** DB와 대조해요
  (`select id,status from vms where proxmox_vmid=<n>`). DELETED 행에만 걸리면 고아예요.
- 수동 파기는 고아임을 확인한 뒤에만 쓰는 최후 수단이에요. `qm stop <vmid>`,
  `qm destroy <vmid> --purge` 전에 vmid를 다시 확인해요. `qm`에는 confirmName 같은 보호가 없어요.
- 관리해야 할 게스트라면 파괴하지 않고 에스컬레이션해 DB의 의도를 맞춰요. 프로비저닝
  파이프라인의 불변식을 우회해 행을 손으로 만들지 않아요.

게스트가 사라지거나 claim된 뒤 **온전한 주기가 한 번 돌아야** 자동으로 닫혀요.
노드 장애 중에는 불완전 주기 예외를 적용해요.

## SPEC_MISMATCH(부여한 사양과 실제 게스트 불일치)

`resource.maxcpu != vms.vcpu` 또는 `resource.maxmem != vms.memory_mb`이면 항목을 기록하고
`"사양 불일치(드리프트)"` 접두의 `status_detail`을 붙여요. **상태 전이는 없고** VM은 계속 실행돼요.
수동 `qm set`이나 미완료 변경이 있는지 확인해요.

- 부여 사양으로 맞추려면 `qm set <vmid> --cores <n> --memory <MB>`를 실행해요. 변경 뒤
  실제 게스트에 반영됐는지 확인해요. 재부팅이 필요할 수 있어요. 사양이 일치하면 안내 문구와
  항목이 자동으로 사라져요. 사양 드리프트 문구만 지우고 실제 파이프라인 오류는 덮어쓰지 않아요.
- 실제 사양을 유지해야 한다면 별도 복구 절차를 검토해요. `vms`를 손으로 고치지 않아요.

### 보호 플래그 변종(`:protection` dedup 키)

"보호 플래그 불일치"인 SPEC_MISMATCH는 상시 플랫폼 불변식이 깨졌다는 뜻이에요.
**관리 대상 VM은 모두 PVE `protection: 1`을 유지해야 해요.** 프로비저닝 때 설정하고,
destroy 파이프라인만 파기 직전에 해제해요. 사용자 `deletion_protection`은 별도 논리 게이트이며
PVE 플래그로 반영하지 않아요.

대역 밖의 `qm set <vmid> --protection 0`이나 백필 누락을 확인해요.
`qm set <vmid> --protection 1`로 복구하면 다음 온전한 주기에 항목이 자동으로 닫혀요.
리컨실러가 플래그를 다시 설정하지는 않아요.

## 상황별 조치

| 상황 | 조치 |
|---|---|
| NEEDS_ADMIN 작업, 게스트를 아직 만들 수 있음 | 작업 **재시도**(`/admin/tasks/{id}/retry`) |
| 실제로 사라진 게스트의 DB 행 | **force-delete**(`/admin/vms/{id}/force-delete`, confirmName) |
| DB 행 없는 태그 게스트(고아 확인됨) | **수동 `qm destroy --purge`**(API로는 불가) |
| vcpu 또는 memory 드리프트 | `qm set`으로 부여 사양에 맞추거나 별도 복구 절차 검토 |
| 대역 밖에서 해제된 보호 플래그 | `qm set <vmid> --protection 1`(상시 불변식) |
| 노드 오프라인 또는 일시적 | 온전한 주기 하나를 기다려요. 파괴적 조치 없음 |

## 수동 해소(조건을 고쳤는데 항목이 남을 때)

원인을 고쳤는데도 OPEN 항목이 남으면 수동으로 닫을 수 있어요. 이 API는 발견 항목을 닫으며
원인 드리프트를 복구하지 않아요.

```
POST /api/v1/admin/drift-findings/{findingId}/resolve      # SYS_ADMIN, 본문에 메모 선택
```

보통은 원인을 고친 뒤 다음 온전한 주기의 자동 해소를 기다려요. 드리프트가 남아 있으면
수동으로 닫아도 다시 열려요.

## 안전 불변식

force-delete는 **유예 없이 파기를 접수**하는 SYS_ADMIN 전용 흐름이에요. `confirmName`이
VM 이름과 정확히 일치하지 않으면 409 `VM_CONFIRM_NAME_MISMATCH`로 거부하며 **취소할 수 없어요.**
본인 삭제의 `vm_delete_grace_hours` 유예나 관리자의 미래 파기 시각 예약과 구분해요.

최종 갱신: 2026-11-03
