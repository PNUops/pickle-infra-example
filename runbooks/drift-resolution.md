# 드리프트 발견 항목 해소

DriftReconciler(`@Recurring` id `drift-reconciler`, 10분 주기)가 `drift_findings`에
기록하는 드리프트 3종의 운영 절차다. 리코실러는 "DB의 의도와 Proxmox의 현실"을 대조하는
기준이고, **표시만 하며 아무것도 파괴하지 않는다.** 항목마다 사람이 관리 API로 해소한다.
이 문서는 `DriftFindingKind` 종류별 조사 방법과 안전한 해소 방법을 담는다.

## 개념

- **종류**(`DriftFindingKind`): `MISSING_IN_PROXMOX`, `UNMANAGED_GUEST`, `SPEC_MISMATCH`.
- **상태**(`DriftFindingStatus`): `OPEN`, `RESOLVED`. 한 조건에 OPEN 항목은 최대 하나다.
- **자동 해소**: 원인 조건을 고치면 다음 온전한 주기에 항목이 스스로 닫힌다. 손으로
  해소할 일은 거의 없다. **예외**: `UNMANAGED_GUEST`의 자동 해소는 주기가 불완전하면
  건너뛴다(노드가 OFFLINE이거나 목록 조회가 실패한 경우). 항목이 깜빡이는 것을 막기
  위해서이므로, 노드 장애 중에 오래된 unmanaged 항목이 남아 있는 것은 정상이다.

## 조사

관리 드리프트 엔드포인트는 전부 **SYS_ADMIN 전용**이고
`https://pickle.pusan.ac.kr/api/v1/admin/drift-findings` 아래에 있다.

```
# OPEN 항목 목록 (종류로 걸러도 된다)
GET /api/v1/admin/drift-findings?status=OPEN
GET /api/v1/admin/drift-findings?status=OPEN&kind=MISSING_IN_PROXMOX
# 각 항목이 담는 것: kind, vm_id(UNMANAGED_GUEST면 null), proxmox_vmid,
# node_name, summary(한국어), detail(jsonb), first_seen_at, last_seen_at
```

pve-node에서 Proxmox를 읽기 전용으로 대조한다. `qm list`, `qm config <vmid>`,
`pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -c "select id,status,proxmox_vmid,vcpu,memory_mb from vms where proxmox_vmid=<n>"`.

## MISSING_IN_PROXMOX(DB 행은 있으나 게스트 없음)

탐지 조건은 `proxmox_vmid`를 가진 DELETED/ERROR 아닌 `vms` 행에 대응하는 qemu 게스트가
클러스터 목록에 없는 것이다. vmid로 대조하므로 라이브 마이그레이션된 VM은 잘못 표시되지
않는다. 결과로 항목이 기록되고 **동시에 VM이 CAS로 NEEDS_ADMIN에 주차된다**
(`status_detail = "Proxmox에 VM 없음(드리프트)"`). VM 상태를 바꾸는 유일한 종류이고,
바꾸는 방향은 주차뿐이며 파괴는 하지 않는다.

원인을 조사한 뒤 처리한다.

- **노드가 OFFLINE이었거나 일시적인 경우**(리코실러는 오프라인 노드를 범위에서 빼고 그
  노드의 VM 키를 보류한다). 노드를 되살리면 다음 온전한 주기에 항목이 사라진다. 파괴적인
  조치를 하지 않는다.
- **게스트가 실제로 사라진 경우**(실수로 `qm destroy` 했거나 프로비저닝이 실패한 경우).
  VM을 없애야 한다면 `POST /api/v1/admin/vms/{vmId}/force-delete`에
  `{"confirmName":"<VM 이름 그대로>"}`를 보낸다(SYS_ADMIN, 202). 삭제 잡이 실행되어
  ACPI 종료 후 강제 정지하고 Proxmox에서 destroy와 purge를 수행하며(게스트가 이미 없으면
  아무 일도 하지 않고, 신원을 다시 확인하므로 남의 vmid를 죽이지 않는다), IP를 24시간
  격리로 반납하고, 행을 CAS로 `DELETED` 표시한다. 그러면 MISSING 항목은 자동 해소된다.
- **이 VM의 프로비저닝 작업이 NEEDS_ADMIN에 멈춰 있고**(`GET /api/v1/admin/tasks`) 게스트를
  아직 만들 수 있는 경우. `POST /api/v1/admin/tasks/{taskId}/retry`(SYS_ADMIN, 202)를
  호출한다. **NEEDS_ADMIN** 작업만 재시도할 수 있고(RETRYING으로 전이), 나머지는 409
  `TASK_NOT_RETRYABLE`을 받는다.

## UNMANAGED_GUEST(어느 DB 행도 claim하지 않는 pickle 태그 게스트)

탐지 조건은 관리 대상 `pickle` 태그를 단 qemu 게스트의 vmid를 DELETED 아닌 `vms` 행이
**하나도** claim하지 않는 것이다. 결과는 **항목 기록뿐**이고(`vm_id`는 null,
`dedup_key = "vmid:<n>"`) 게스트는 **건드리지 않는다.** DB 행이 없으므로 API의
force-delete가 이 게스트에 대해 동작할 수 없다.

조사하면 대개 행은 지웠으나 게스트는 남긴 삭제의 잔재이거나, 태그를 그대로 둔 채 손으로
복제한 VM이다.

- 정말 고아인지 확인한다. `qm config <vmid>`를 읽고, vmid를 **DELETED 행까지 포함해서**
  DB와 대조한다(`select id,status from vms where proxmox_vmid=<n>`). DELETED 행에만
  걸리면 진짜 고아다.
- 안전한 해소는 최후 수단이고 수동이다(API가 의도적으로 하지 않는다). 확인을 마친 뒤
  `qm stop <vmid>`, `qm destroy <vmid> --purge`. destroy 전에 vmid를 다시 확인한다.
  `qm`에는 confirmName 같은 안전장치가 없다.
- 반대로 그 게스트를 **관리해야 하는** 경우라면(DB가 행을 잃은 경우) 파괴하지 말고 DB를
  맞춘다. 에스컬레이션하고, 프로비저닝 파이프라인의 불변식을 우회해서 행을 손으로
  만들지 않는다.

게스트가 사라지거나 claim되고 **또한** 온전한 주기가 한 번 돌면 항목이 자동 해소된다.
노드 장애 중에는 위의 불완전 주기 예외가 적용된다.

## SPEC_MISMATCH(부여한 사양과 실제 게스트 불일치)

탐지 조건은 `resource.maxcpu != vms.vcpu` 또는 `resource.maxmem != vms.memory_mb`다.
결과로 항목이 기록되고 `"사양 불일치(드리프트)"` 접두가 붙은 안내용 `status_detail`이
달린다. **상태 전이는 없고** VM은 계속 실행된다.

조사하면 손으로 `qm set` 해서 크기를 바꿨거나, 리사이즈가 끝까지 적용되지 않은 경우다.

- 부여한 사양으로 수렴시키려면 `qm set <vmid> --cores <n> --memory <MB>`를 실행한다.
  메모리 변경은 즉시 반영되고, 코어 변경은 게스트 재부팅이 필요할 수 있다. 사양이 일치하면
  안내 문구와 항목이 자동으로 지워진다(리코실러는 사양 드리프트 문구일 때만 지우고,
  실제 파이프라인 오류 문구는 덮어쓰지 않는다).
- 실제 사양이 의도한 값이라면 DB의 의도를 맞추도록 통상의 관리 리사이즈 흐름으로 부여
  사양을 갱신한다. `vms`를 손으로 고치지 않는다.

### 보호 플래그 변종(`:protection` dedup 키)

"보호 플래그 불일치"라고 적힌 SPEC_MISMATCH 항목은 상시 플랫폼 불변식이 깨졌다는 뜻이다.
**관리 대상 VM은 모두 PVE `protection: 1`을 달고 있어야 한다.** 프로비저닝 때 걸리고,
destroy 파이프라인만이 destroy 직전에 해제한다. 사용자에게 보이는 `deletion_protection`
설정은 이것과 무관하다. 그쪽은 pickle 안쪽의 논리적 관문이고 플래그로 반영되지 않는다.

원인은 대역 밖 조작(`qm set <vmid> --protection 0`)이거나 백필 누락이다. 해소는
`qm set <vmid> --protection 1`이고, 다음 온전한 주기에 항목이 자동 해소된다. 리코실러는
보고만 하며 플래그를 스스로 다시 걸지 않는다.

## 상황별 조치

| 상황 | 조치 |
|---|---|
| NEEDS_ADMIN 작업, 게스트를 아직 만들 수 있음 | 작업 **재시도**(`/admin/tasks/{id}/retry`) |
| 실제로 사라진 게스트의 DB 행 | **force-delete**(`/admin/vms/{id}/force-delete`, confirmName) |
| DB 행 없는 태그 게스트(고아 확인됨) | **수동 `qm destroy --purge`**(API로는 불가) |
| vcpu 또는 memory 드리프트 | `qm set`으로 부여 사양에 맞춤(또는 부여 사양을 갱신) |
| 대역 밖에서 해제된 보호 플래그 | `qm set <vmid> --protection 1`(상시 불변식) |
| 노드 오프라인 또는 일시적 | 온전한 주기 하나를 기다린다. 파괴적 조치 없음 |

## 수동 해소(조건을 고쳤는데 항목이 남을 때)

보통은 필요 없다(자동 해소가 처리한다). 조건을 실제로 고쳤는데도 항목이 OPEN으로 남으면
다음을 호출한다.

```
POST /api/v1/admin/drift-findings/{findingId}/resolve      # SYS_ADMIN, 본문에 메모 선택
```

원인 드리프트를 고치고 다음 온전한 주기의 자동 해소에 맡기는 쪽이 낫다. 실제로는 아직
드리프트 중인데 손으로 해소한 항목은 그대로 다시 열린다.

## 안전 불변식

즉시 파괴하는 관리 조작은 force-delete 하나뿐이다. SYS_ADMIN 전용이고 `confirmName`이 VM
이름과 정확히 일치하지 않으면 거부한다(409 `VM_CONFIRM_NAME_MISMATCH`). **취소할 수
없다.** `vm_delete_grace_hours` 유예 창이 있는 예약 삭제와 다르다.
