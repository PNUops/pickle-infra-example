# 노드 단독 등록

`scripts/register-node.py`는 실측한 Proxmox 노드 한 개를 플랫폼 DB에 등록해요.
새 노드는 `MAINTENANCE`로 시작해요. 기존 IP pool을 이름과 CIDR, gateway로 확인해
연결하고, pool과 relay, domain, certificate, OS 이미지 행은 만들거나 수정하지 않아요.

PVE 노드에서 `collect`로 실측하고, PostgreSQL 호스트에서 `register`로 검토해요.
`register`는 기본적으로 읽기 전용 dry-run이며, `--apply`가 있어야 DB에 써요.
두 호스트 모두 정확한 hostname과 root 권한을 확인해요. DB에는 해당 호스트의
Unix socket과 `postgres` 계정으로만 접속하고 비밀번호를 전달하지 않아요.

## 입력 준비

`examples/node-registration.json`을 복사해 해당 환경의 값으로 바꿔 주세요. 예약 주소와
예시 이름을 그대로 적용할 수는 없어요. 기존 예시의 모든 필드는 필수이고 예약량에는 기본값이 없어요.
CPU 공유 정책의 `cpu_allocation_ratio`와 `committed_vcpu`만 선택 항목이며 둘 다 넣거나 둘 다 생략해요.
입력 형상은 `examples/node-registration-cpu-sharing.json`에서 확인해요.
예시 pool은 새 운영 사용자망 `100.66.0.0/16`이에요. 기존 개발망 예시
`198.19.0.0/16`과 별도로 준비한 운영 pool을 지정하고 실제 배포 주소와 대조해 주세요.

| 입력 | 확인할 내용 |
|---|---|
| `node`, `cluster` | 로컬 hostname과 `/cluster/status`의 실제 노드, cluster 이름 및 quorum |
| `api_url`, `api_address`, `ca_file` | 노드에 실제 할당된 전송 주소, 인증서가 포함하는 DNS 이름, 신뢰할 공개 CA 파일 |
| `bridge`, `bridge_mtu` | 이미 생성된 guest bridge와 MTU. 대기 노드는 gateway 주소를 소유할 필요가 없음 |
| `storage` | 해당 노드의 활성 `lvmthin` storage. PVE 총량과 실제 VG/thin-pool의 크기를 대조 |
| `pool_name`, `pool_cidr`, `pool_gateway` | DB에 이미 존재하는 사용자 VM IP pool의 정확한 값 |
| `reserve_cpu_threads`, `reserve_memory_mb`, `reserve_disk_gb` | CPU는 보호할 물리 thread 예약량. host와 별도로 격리할 플랫폼 thread를 합쳐 한 번 차감. RAM과 디스크는 남길 MiB와 GiB. 0도 명시 |
| `cpu_allocation_ratio`, `committed_vcpu` | 선택 항목 한 쌍. 정수 비율 1 또는 2와 다른 용도로 이미 commit된 vCPU. 생략하거나 1/0이면 기존 schema 1 |
| `gpu_node` | GPU 노드로 운영할지 명시한 boolean. 실제 GPU의 존재나 사용 가능 상태를 자동 판정하는 값이 아님 |
| `database`, `database_hostname`, `database_system_identifier`, `database_socket_dir` | 새 플랫폼 DB의 이름, PostgreSQL 호스트 이름, 실제 PostgreSQL system identifier, 로컬 socket 경로 |
| `existing_public_id` | 최초 등록은 `null`. 재등록은 기존 노드의 정확한 공개 UUID |

PostgreSQL system identifier는 **대상 DB 호스트에서** 먼저 확인해 주세요.

```bash
hostname -s
sudo -u postgres psql -X -h /var/run/postgresql -d pickle_example -Atc \
  'SELECT current_database(), system_identifier::text FROM pg_control_system();'
```

OS root 계정과 DB의 `postgres` 계정이 필요해요. 환경 변수 때문에 다른 Unix socket이나
DB에 접속하지 않도록 `PG*` 환경 변수를 제거하고 접속 대상을 명시해요.
DB 이름과 system identifier, 로컬 접속, primary 상태도 다시 확인해요.

## 실측과 dry-run

PVE 노드에서 실행해요. HTTP 요청은 CA와 TLS hostname을 검증하고 `-k`를 사용하지 않아요.
전송 주소는 이 노드의 실제 주소여야 해요. 기존 파일을 덮어쓰지 않고 새 보고서를 0600으로 만들어요.

```bash
sudo python3 scripts/register-node.py collect \
  --config /root/node-registration.json --output /root/node-inventory.json
```

보고서와 출력한 SHA-256을 별도 경로로 대상 PostgreSQL 호스트에 전달해 주세요.
등록 직전 15분 이내의 보고서만 받아요. 시간이 지났거나 설정을 바꿨으면 다시 실측해 주세요.

```bash
sudo python3 scripts/register-node.py register \
  --inventory /root/node-inventory.json --inventory-sha256 '<확인한 SHA-256>'
```

dry-run은 `BEGIN READ ONLY` 안에서 조회만 해요. INSERT 후 ROLLBACK하는 방식이 아니어서
노드 ID sequence도 증가하지 않아요. 결과에서 노드 이름과 기존 UUID, 상태, 물리 용량,
예약량, 배치 가능량을 확인해 주세요. 이미지 등록이나 활성화는 하지 않아요.

## 용량의 의미와 활성화 조건

`cpu_threads`는 실측 물리 thread 수이고 `memory_mb`는 실측 MiB에서 RAM 예약량을 뺀 값이에요.
`disk_capacity_gb`는 예약량을 빼지 않은 thin-pool의 **물리 총 GiB**예요.
디스크 용량 컬럼은 현재 강제 배치 제한이 아닌 참고 분모예요.

`labels.placement_capacity`에 다음 세 값을 함께 보존해요.

- `physical`: 실측 CPU thread, RAM MiB, disk GiB
- `reserved`: 명시한 물리 예약량. schema 2 CPU는 host와 별도로 격리할 플랫폼의 물리 thread 예약량
- `allocatable`: RAM과 디스크는 실측 값에서 예약량을 한 번 뺀 값. CPU는 아래 정책 계산 값

기본 입력은 label `schema_version: 1`과 기존 세 용량 그룹, `measured_at`을 유지해요.
공유 비율이 2이거나 commit된 vCPU가 0보다 크면 schema 2와
`cpu_policy: {allocation_ratio, committed_vcpu}`를 함께 기록해요.

CPU 배치 가능량은 `(physical.cpu_threads - reserved.cpu_threads) × allocation_ratio - committed_vcpu`예요.
물리 thread와 보호 예약량은 물리 단위로 보존하고 다른 용도의 commit은 vCPU 단위로 기록해요.
플랫폼 DB에 등록된 VM은 배치 consumer가 합산하므로 그 vCPU를 `committed_vcpu`에 다시 넣지 마세요.
보호 예약량이나 같은 외부 VM을 두 번 차감하지 않도록 현재 인벤토리와 운영자 정책을 함께 검토해 주세요.
예를 들어 물리 24, host 예약 4, 비율 2, 별도 commit 17이면 새 플랫폼 vCPU 예산은 23이에요.
이 값이 사용률이나 속도를 보장하거나 kernel 제한을 설정하지는 않아요. RAM과 디스크의 예약 정책도 이 비율로 바뀌지 않아요.

플랫폼 물리 core를 학생 영역과 분리한다면 host 4thread와 플랫폼 8thread의 합계
12thread를 물리 예약으로 먼저 차감해요. 물리 24/32thread에서 학생 영역만 2:1로
공유하고 외부 보존 VM 5/3vCPU를 별도 commit하면 admission은 19/37vCPU예요.
이미 물리 예약으로 뺀 플랫폼 CT vCPU를 별도 commit에 다시 넣지 마세요.
실제 SMT와 cgroup, 시작 경계 검증은 [CPU 격리 런북](cpu-isolation.md)을 따라요.

한 필드만 지정하거나 bool이나 소수, 문자열, 음수, 지원하지 않는 비율, 정수 범위를 넘는 값을 주면 거부해요.
보호 예약량이 물리량 이상이거나 commit이 공유 예산을 모두 소비해도 거부해요.
CPU label 예산은 signed 64-bit 범위를 사용하고 DB의 `cpu_threads` 컬럼에는 실제 물리 thread 수를 유지해요.
기존 GPU 표시와 다른 label도 보존해요. API는 `allocatable`을 직접 사용해야 하며,
이미 차감된 `memory_mb`에서 예약량을 다시 빼면 안 돼요.

**CPU와 디스크 예약은 label 기록만으로 집행되지 않아요.** 해당 label을 읽는 배치 기능을
검증하기 전에는 신규 노드를 활성화하지 마세요. schema 2 CPU 정책을 쓰면 이를 읽는
API consumer를 먼저 배포하고 실제 배치 거부/허용 경계를 확인해야 해요.
구버전 consumer에 schema 2 label을 기록한 뒤 ACTIVE로 올리지 마세요.
API 실행 환경의 노드 이름 해석과 CA, Proxmox 권한, IPAM 예약 범위, VM 생성과 접속도
활성화 전에 검증해야 해요. 이 도구의 HTTPS 검사는 PVE 노드에서 수행한 확인이에요.

OS 이미지 등록은 이 도구의 범위 밖이에요. 이미지의 전역 이름/revision 유니크 제약이
유지된 상태에서는 같은 revision을 여러 노드에 등록할 수 없으므로 별도 이미지 지원과
실제 배치 검증을 마쳐야 해요.

새 노드는 운영자가 입력한 `gpu_node`를 기존 `labels.gpu`에 기록해요. 이 표시는 일반 VM 배치의
노드 선택에 쓰여요. 사용 가능한 GPU 자원을 등록하거나 GPU 할당을 허용하지는 않아요.
GPU 노드를 활성화하기 전에 별도로 GPU 자원과 mapping, 권한, 실제 부착과 해제 경로를 검증해 주세요.
재등록에서는 기존 GPU 노드 표시와 입력이 다르면 거부해요.

## 적용과 재등록

대상 DB 호스트에 root 소유 0700의 보호 백업 디렉터리를 준비해 주세요.
기존 노드 행과 요청한 변경을 0600 파일로 먼저 보관하고, DB transaction이 성공하면
별도 결과 파일을 추가해요. 기존 기록은 덮어쓰지 않아요.

```bash
sudo python3 scripts/register-node.py register \
  --inventory /root/node-inventory.json --inventory-sha256 '<확인한 SHA-256>' \
  --backup-dir /var/backups/node-registration --apply
```

실제 쓰기는 node별 transaction lock과 해당 node/pool 행 잠금 아래에서 실행해요.
조회 이후 node나 pool이 바뀌면 transaction이 실패해요. 변경 대상은 `nodes`의 한 행뿐이에요.

재등록은 `existing_public_id`를 정확히 입력한 새 보고서로 진행해요. `api_host`나 bridge,
storage, pool 연결이 다르면 자동으로 이동하지 않고 거부해요. 기존 `public_id`와 상태, 다른 label은 유지해요.
ACTIVE 노드의 용량이나 예약량, CPU 공유 비율이나 commit된 vCPU를 바꾸려면 먼저 별도 관리 절차로
MAINTENANCE에 두세요. 재등록으로 기존 상태는 바뀌지 않아요.
기존 `placement_capacity`가 알려진 schema 1/2 형상이나 정수 타입, 계산식과 다르거나 `node_registration`이
object가 아니면 덮어쓰지 않아요. 모르는 예약 방식은 별도로 검토해야 해요.

## 실패와 복구

DB가 응답하지 않았거나 결과 파일 기록에 실패했다면 적용 결과를 단정하지 마세요.
`*-before.json`은 쓰기 전에 만들어지므로 파일이 있다는 사실만으로 성공이나 롤백을 판단할 수 없어요.
설정한 노드 이름으로 실제 node 행을 읽기 전용 조회해 저장한 요청과 snapshot을 대조하고 공개 UUID를 확인해 주세요.
기대한 공개 UUID를 알고 있다면 정확히 일치해야 해요. 적용 결과를 확인하기 전에는 다시 적용하지 마세요.

기존 노드의 용량을 되돌릴 때는 기록의 node ID와 공개 UUID, 현재 상태를 먼저 확인해요.
`cpu_threads`, `memory_mb`, `disk_capacity_gb`, 이 작업이 기록한 두 label만 검토해 복원해요.
그 사이 운영자가 바꾼 상태나 UUID, 다른 label을 과거 값으로 덮어쓰지 마세요.
새 노드는 자동 삭제하지 않아요. MAINTENANCE를 유지한 채 VM과 이미지, 다른 참조가 없는지 확인한 뒤
별도 관리 절차로 처리해 주세요.

## 검증

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/tests/test_node_registration.py
```

기본 테스트는 호스트와 DB를 변경하지 않아요. 실제 SQL 검증은 로컬에 이미 존재하는
PostgreSQL 이미지 digest를 `PICKLE_TEST_POSTGRES_IMAGE`로 지정하면 별도 컨테이너에서 실행해요.
테스트 컨테이너는 외부 네트워크와 연결하지 않고 종료할 때 제거해요.

최종 갱신: 2026-11-03
