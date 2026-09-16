# 노드 단독 등록

`scripts/register-node.py`는 실측한 Proxmox 노드 한 개를 플랫폼 DB에 등록합니다.
새 노드는 `MAINTENANCE`로 시작합니다. 기존 IP pool을 이름과 CIDR, gateway로 확인해
연결하며 pool, relay, domain, certificate, OS 이미지 행은 만들거나 수정하지 않습니다.

PVE 노드에서 `collect`로 실측하고, PostgreSQL 호스트에서 `register`로 검토합니다.
`register`의 기본값은 읽기 전용 dry-run입니다. 실제 쓰기는 `--apply`가 있을 때만 수행합니다.
두 호스트 모두 정확한 hostname과 root 권한을 확인합니다. DB 접속은 해당 호스트의
Unix socket과 `postgres` 계정으로 한정하며 비밀번호를 전달하지 않습니다.

## 입력 준비

`examples/node-registration.json`을 복사해 해당 환경의 값으로 바꾸세요. 예시는 예약
주소와 예시 이름이므로 그대로 적용할 수 없습니다. 모든 필드가 필수이며 예약량에 기본값은 없습니다.
예시 pool은 새 운영 사용자망 `100.66.0.0/16`입니다. 기존 개발망 예시
`198.19.0.0/16`과 별도로 준비한 운영 pool을 지정하고 실제 배포 주소와 대조하세요.

| 입력 | 확인할 내용 |
|---|---|
| `node`, `cluster` | 로컬 hostname과 `/cluster/status`의 실제 노드, cluster 이름 및 quorum |
| `api_url`, `api_address`, `ca_file` | 노드에 실제 할당된 전송 주소, 인증서가 포함하는 DNS 이름, 신뢰할 공개 CA 파일 |
| `bridge`, `bridge_mtu` | 이미 생성된 guest bridge와 MTU. 대기 노드는 gateway 주소를 소유할 필요가 없음 |
| `storage` | 해당 노드의 활성 `lvmthin` storage. PVE 총량과 실제 VG/thin-pool의 크기를 대조 |
| `pool_name`, `pool_cidr`, `pool_gateway` | DB에 이미 존재하는 사용자 VM IP pool의 정확한 값 |
| `reserve_cpu_threads`, `reserve_memory_mb`, `reserve_disk_gb` | core, 호스트와 장애 복구에 남길 CPU thread 수, MiB, GiB. 0도 명시해야 함 |
| `gpu_node` | GPU 노드로 운영할지 명시한 boolean. 실제 GPU의 존재나 사용 가능 상태를 자동 판정하는 값이 아님 |
| `database`, `database_hostname`, `database_system_identifier`, `database_socket_dir` | 새 플랫폼 DB의 이름, PostgreSQL 호스트 이름, 실제 PostgreSQL system identifier, 로컬 socket 경로 |
| `existing_public_id` | 최초 등록은 `null`. 재등록은 기존 노드의 정확한 공개 UUID |

PostgreSQL system identifier는 **대상 DB 호스트에서** 먼저 확인하세요.

```bash
hostname -s
sudo -u postgres psql -X -h /var/run/postgresql -d pickle_example -Atc \
  'SELECT current_database(), system_identifier::text FROM pg_control_system();'
```

OS root 계정과 DB의 `postgres` 계정이 필요합니다. 다른 Unix socket이나 다른 DB로
환경 변수가 접속을 바꾸지 않도록 스크립트는 `PG*` 환경 변수를 제거하고 접속 대상을
명시합니다. DB의 이름과 system identifier, 로컬 접속, primary 상태를 다시 확인합니다.

## 실측과 dry-run

PVE 노드에서 실행합니다. HTTP 요청은 CA와 TLS hostname을 검증하며 `-k`를 사용하지 않습니다.
전송 주소는 이 노드의 실제 주소여야 합니다. 기존 파일을 덮어쓰지 않고 새 보고서를 0600으로 만듭니다.

```bash
sudo python3 scripts/register-node.py collect \
  --config /root/node-registration.json --output /root/node-inventory.json
```

보고서와 출력한 SHA-256을 별도 경로로 대상 PostgreSQL 호스트에 전달하세요.
등록 직전 15분 이내의 보고서만 받습니다. 시간이 지났거나 설정을 바꿨으면 다시 실측하세요.

```bash
sudo python3 scripts/register-node.py register \
  --inventory /root/node-inventory.json --inventory-sha256 '<확인한 SHA-256>'
```

dry-run은 `BEGIN READ ONLY` 안에서 조회만 합니다. INSERT를 실행했다가 ROLLBACK하는
방식이 아니므로 노드 ID sequence도 증가하지 않습니다. 결과에서 노드 이름, 기존 UUID,
상태와 물리 용량, 예약량, 배치 가능량을 확인하세요. 이미지 등록이나 활성화는 하지 않습니다.

## 용량의 의미와 활성화 조건

기존 컬럼의 의미를 유지합니다. `cpu_threads`는 실측 물리 thread 수이고 `memory_mb`는
실측 MiB에서 RAM 예약량을 뺀 값입니다. `disk_capacity_gb`는 thin-pool의 **물리 총 GiB**이며
예약량을 빼지 않습니다. 디스크 용량 컬럼은 현재 강제 배치 제한이 아닌 참고 분모입니다.

`labels.placement_capacity`에 다음 세 값을 함께 보존합니다.

- `physical`: 실측 CPU thread, RAM MiB, disk GiB
- `reserved`: 명시한 core와 복구 예약량
- `allocatable`: 각 실측 값에서 예약량을 한 번 뺀 값

label에는 `schema_version: 1`과 `measured_at`도 포함합니다. 기존 GPU 표시와 다른 label은
보존합니다. API가 `allocatable`을 직접 사용해야 하며, 이미 차감된 `memory_mb`에서
예약량을 다시 빼면 안 됩니다.

**CPU와 디스크 예약은 label 기록만으로 집행되지 않습니다.** 해당 label을 읽는 배치
기능이 검증되기 전에는 신규 노드를 활성화하지 마세요. CPU overcommit 정책도 별도입니다.
API 실행 환경의 노드 이름 해석, CA와 Proxmox 권한, IPAM 예약 범위, VM 생성과 접속도
활성화 전에 검증해야 합니다. 이 도구의 HTTPS 검사는 PVE 노드에서 수행한 확인입니다.

OS 이미지 등록은 이 도구의 범위 밖입니다. 이미지의 전역 이름/revision 유니크 제약이
유지된 상태에서는 같은 revision을 여러 노드에 등록할 수 없으므로 별도 이미지 지원과
실제 배치 검증을 마쳐야 합니다.

새 노드는 운영자가 입력한 `gpu_node`를 기존 `labels.gpu`에 기록합니다. 이 표시는 일반
VM 배치의 노드 선택에 쓰이며, 사용 가능한 GPU 자원을 등록하거나 GPU 할당을 허용하는
동작이 아닙니다. GPU 노드의 활성화 전에는 별도로 GPU 자원과 mapping, 권한, 실제 부착과
해제 경로를 검증하세요. 재등록에서는 기존 GPU 노드 표시와 입력이 다르면 거부합니다.

## 적용과 재등록

대상 DB 호스트의 보호 백업 디렉터리를 준비하세요. root 소유 0700이어야 합니다.
기존 노드 행과 요청한 변경을 0600 파일로 먼저 보관하고, DB transaction이 성공하면
별도 결과 파일을 추가합니다. 같은 디렉터리의 기존 기록을 덮어쓰지 않습니다.

```bash
sudo python3 scripts/register-node.py register \
  --inventory /root/node-inventory.json --inventory-sha256 '<확인한 SHA-256>' \
  --backup-dir /var/backups/node-registration --apply
```

실제 쓰기는 node별 transaction lock과 해당 node/pool 행 잠금 아래에서 실행합니다.
조회 이후 node나 pool이 바뀌면 transaction을 실패시킵니다. 대상은 `nodes`의 한 행뿐입니다.

재등록은 `existing_public_id`를 정확히 입력한 새 보고서로 수행합니다. `api_host`,
bridge, storage와 pool 연결이 다르면 자동 이동으로 취급하지 않고 거부합니다.
기존 `public_id`, 상태와 다른 label은 유지합니다. ACTIVE 노드의 용량이나 예약량을
바꾸려면 먼저 별도 관리 절차로 MAINTENANCE에 두세요. 이 스크립트는 상태를 바꾸지 않습니다.
기존 `placement_capacity`가 알려진 schema 1 형상과 다르거나 `node_registration`이
object가 아니면 덮어쓰지 않습니다. 모르는 예약 방식은 별도 검토가 필요합니다.

## 실패와 복구

DB가 응답하지 않았거나 결과 파일 기록에 실패했다면 적용 결과를 단정하지 마세요.
`*-before.json`은 쓰기 전에 생성하므로 파일이 있다는 사실만으로 성공이나 롤백을
판단할 수 없습니다. 실제 node 행을 공개 UUID로 다시 조회해 저장 기록과 대조하세요.

기존 노드의 용량을 되돌릴 때는 기록의 node ID와 공개 UUID, 현재 상태를 먼저 확인하고
`cpu_threads`, `memory_mb`, `disk_capacity_gb`, 이 작업이 기록한 두 label만 검토해 복원합니다.
그 사이 운영자가 바꾼 상태, UUID와 다른 label을 과거 값으로 덮어쓰지 않습니다.
새 노드는 자동 삭제하지 않습니다. MAINTENANCE를 유지한 채 VM, 이미지와 다른 참조가
없는지 확인한 다음 별도 관리 절차로 처리하세요.

## 검증

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/tests/test_node_registration.py
```

기본 테스트는 호스트와 DB를 변경하지 않습니다. 실제 SQL 검증은 로컬에 이미 존재하는
PostgreSQL 이미지 digest를 `PICKLE_TEST_POSTGRES_IMAGE`로 지정하면 별도 컨테이너에서
실행합니다. 테스트 컨테이너는 외부 네트워크와 연결하지 않고 종료 시 제거합니다.
