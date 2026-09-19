# IP pool 등록과 VM 방화벽 노드 준비

이 절차는 새 플랫폼 DB에 IP pool 한 건을 등록하고, 이미 등록된 `MAINTENANCE` 노드에
VM 방화벽 정책 opt-in label을 추가합니다. 두 도구 모두 PostgreSQL 호스트의 로컬 socket과
`postgres` 계정만 사용합니다. 기본 실행은 읽기 전용 preview이며 `--apply`가 있어야 한 종류의
행을 변경합니다. 노드나 이미지를 활성화하지 않고 Proxmox 설정도 바꾸지 않습니다.

## 공통 DB 보호 조건

설정에는 DB 이름, DB 호스트의 짧은 hostname, `pg_control_system()`에서 확인한 system
identifier와 로컬 socket 경로를 적습니다. 도구는 root, 정확한 hostname, primary,
PostgreSQL 18 server version과 system identifier, 로컬 접속과 `postgres` role을 모두 대조합니다. 상속된
`PG*` 환경 변수는 사용하지 않습니다.

적용 기록 디렉터리는 root 소유 mode 0700의 실제 디렉터리여야 합니다. 각 실행은 검토한
snapshot과 요청을 0600 `*-before.json`으로 먼저 기록하고, transaction 성공 뒤 별도
`*-after.json`을 만듭니다. advisory lock과 대상 row lock을 잡은 뒤 preview 전체를 다시
비교하므로 중간 변경을 덮어쓰지 않습니다.

```bash
sudo -u postgres psql -X -h /var/run/postgresql -d pickle_example -Atc \
  'SELECT current_database(), system_identifier::text FROM pg_control_system();'

sudo install -d -m 0700 -o root -g root /root/inventory-registration-records
```

## IP pool 한 건 등록

`examples/ip-pool-registration.json`을 보호 경로에 복사해 실제 값으로 바꿉니다. CIDR은
canonical IPv4 network여야 하고 gateway와 모든 reserved range는 usable 주소 안에 있어야
합니다. DNS는 canonical IPv4 주소 1~8개입니다. reserved range는 inclusive이며 정렬된
비중첩 배열로 적습니다.

최초 등록에서는 `existing_public_id`가 `null`입니다. 같은 이름의 행이 이미 있으면 그 행의
공개 UUID를 넣어야 하며 CIDR, gateway, DNS, reserved range가 모두 같을 때만 no-op으로
확인합니다. 기존 pool 변경은 이 도구의 범위가 아닙니다.

```bash
sudo python3 -I scripts/register-ip-pool.py \
  --config /root/ip-pool-registration.json

sudo python3 -I scripts/register-ip-pool.py \
  --config /root/ip-pool-registration.json --apply \
  --backup-dir /root/inventory-registration-records
```

preview는 다른 pool과 CIDR이 겹치거나, 새 CIDR 안에 기존 allocation이 있으면 거부합니다.
기존 pool의 node 참조와 allocation 전체를 snapshot에 포함하고 적용 transaction에서 다시
대조합니다. 적용은 새 `ip_pools` 행 하나만 INSERT할 수 있으며 기존 node, allocation과 pool
행을 수정하지 않습니다. pool 등록 뒤 `register-node.py`가 그 이름과 CIDR, gateway를 다시
확인해 노드에 연결합니다.

## VM 방화벽 노드 opt-in

먼저 cluster firewall, 지원 backend, immutable barrier group, API token의 read/write 권한과
격리된 packet 시험에서 사용할 경로를 실제 환경에서 확인합니다. 그 검증 기록의 식별자를
`capability_evidence_id`에 넣습니다. 자리표시자나 빈 값은 받지 않습니다. 이 값은 label에
들어가지 않고 적용 전후 기록에 남습니다.

`examples/vm-firewall-node.json`에서 node 공개 UUID, API URL, bridge, storage와 MTU를 실제
등록 결과에 맞춥니다. 도구는 node가 `MAINTENANCE`이고 아래 NIC label이 정확한지 확인합니다.

```json
{"schema_version": 1, "mtu": 1370, "firewall": true}
```

preview와 적용은 다음과 같습니다.

```bash
sudo python3 -I scripts/arm-vm-firewall-node.py \
  --config /root/vm-firewall-node.json

sudo python3 -I scripts/arm-vm-firewall-node.py \
  --config /root/vm-firewall-node.json --apply \
  --backup-dir /root/inventory-registration-records
```

적용은 기존 labels를 모두 보존하고 아래 key 하나만 추가합니다.

```json
{"vm_firewall_policy": {"schema_version": 1}}
```

이미 같은 label이면 no-op입니다. 다른 schema이거나 node UUID, API URL, bridge, storage,
NIC 요구사항, `MAINTENANCE` 상태가 바뀌면 중단합니다. 이 도구는 node를 `ACTIVE`로 바꾸거나
API 기능 flag를 켜지 않습니다. 노드와 이미지 활성화, API worker 시작은 방화벽 설정과
packet 검증 계획을 다시 확인한 뒤 별도 단계에서 수행합니다.

## 검증과 실패 처리

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/tests/test_inventory_readiness.py
bash scripts/verify.sh
```

DB 오류나 결과 파일 기록 오류가 나면 성공을 추정하지 않습니다. `*-before.json`은 DB 쓰기
전에 만들어지므로 파일 존재만으로 적용 여부를 판정할 수 없습니다. 같은 DB identity로
pool 또는 node 행을 다시 읽어 `*-after.json`과 비교합니다. 자동 삭제나 과거 snapshot 전체
복원은 하지 않습니다. 다른 작업이 만든 참조나 label을 지우지 말고 대상 한 건을 별도로
검토합니다.
