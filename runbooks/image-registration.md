# 노드별 OS 이미지 등록

`scripts/register-image.py`는 한 노드의 중지된 QEMU template을 읽고, 이미 등록된
MAINTENANCE 노드에 OS 이미지 한 행만 추가해요. 새 행은 DISABLED예요.
다른 노드의 이미지나 node, IP pool, relay, certificate는 갱신하지 않아요.
register는 기본적으로 DB를 읽는 preview이며, `--apply`가 있어야 INSERT해요.

## 실행 전 조건

기존 `apply-os-catalog.sh`는 레거시 전역 unique `(name, version)`만 있는 스키마에서 실행해요.
node-scoped unique가 있거나 두 제약이 함께 있거나 둘 다 없으면 첫 쓰기 전에 중단해요.
이때는 이 런북의 `register-image.py` 흐름으로 전환해 주세요. 전역 writer를 새 노드에 재사용하면
다른 노드의 같은 이름/revision 행을 upsert할 수 있어요.

- DB에 `(node_id, name, version)` unique 제약이 있고 이전 전역 `(name, version)` 제약은 없어야 해요.
  도구는 실제 schema를 읽어 확인하며 schema를 변경하지 않아요.
- 대상 node의 UUID와 cluster, API URL, storage를 확인해요. node는 MAINTENANCE이고
  `vm_nic_requirements`에 `schema_version: 1`, 측정한 `mtu`, `firewall: true`가 있어야 해요.
  새 노드의 이 metadata는 `register-node.py`가 기록해요. 기존 노드에 없는 값은 재등록으로
  자동 주입하지 않으므로 기존 정책을 바꾸려면 별도로 검토해야 해요.
- 새 template의 net0에 해당 MTU와 `firewall=1`이 명시돼 있어야 해요. 값이 없거나 다르면
  수집을 거부해요. 이 설정만으로 실제 방화벽 규칙 적용이나 트래픽 차단 시험을 증명할 수는 없어요.
- template은 기대 노드의 중지된 QEMU template이어야 해요. 플랫폼 관리 VMID 범위인
  100000 이상은 거부해요. scsi0의 실제 volume 크기가 선언한 최소 디스크 이하여야 해요.
- PVE 측에는 Python 3와 `pvesh`, DB 측에는 Python 3, `psql`과 postgres peer 인증이 필요해요.
  collect와 register는 각각 정확한 hostname의 root로 실행해요. DB는 지정한 로컬 socket과 이름,
  system identifier, primary 상태를 확인해요. 상속된 PG 환경 변수는 제거해요.

## 설정과 수집

`examples/image-registration.json`을 별도 보호 경로에 복사해 실제 값으로 작성해 주세요.
`node_public_id`는 기존 등록 결과의 값이에요. 새 UUID를 임의로 넣지 마세요. `existing_public_id`는
처음에는 null이에요. 같은 노드와 이름/revision이 이미 있으면 그 이미지의 정확한 UUID를 지정해요.
revision과 최소 디스크, OS family/release, SSH 계정을 함께 검토해 주세요.

image-builder의 `manifests/<profile>-<vmid>.json`이 있으면 `build_manifest_file`에 절대 경로를 지정해요.
template VMID와 OS, SSH 계정이 일치하는지 검사하고 manifest SHA256과 upstream image checksum,
알고리즘, recipe revision, build time을 등록 증거에 보존해요. 이 checksum은 upstream 이미지의 값이며,
가공된 최종 PVE 디스크 전체 hash는 아니에요. 자료가 없으면 명시적으로 null을 사용해요.
checksum을 추정하거나 만들어 넣지 마세요.

배치할 때는 wrapper와 두 library를 상대 경로 그대로 함께 둬요.

```text
scripts/register-image.py
scripts/lib/image_registration.py
scripts/lib/node_registration.py
```

PVE 노드에서:

```bash
umask 077
sudo python3 -I scripts/register-image.py collect \
  --config /root/image-registration/config.json \
  --output /root/image-registration/template.json
```

수집은 PVE 상태를 바꾸지 않아요. 현재 cluster와 template 상태, NIC, scsi0 volume을 확인하고
선택한 항목만 JSON에 기록해요. cloud-init password 등 전체 VM config를 복사하거나 기존 출력 파일을
덮어쓰지 않아요. 수집 결과와 SHA256을 확인한 뒤 DB 호스트의 보호 경로로 전달해 주세요.
보고서는 15분 동안만 유효하므로 시간이 지나면 다시 수집해요.

## Preview와 적용

DB 호스트에서 먼저 preview해요. 아래 hash 자리표시자는 수집한 파일의 실제 값으로 바꿔 주세요.

```bash
sudo python3 -I scripts/register-image.py register \
  --inventory /root/image-registration/template.json \
  --inventory-sha256 REVIEWED_SHA256
```

같은 이름/revision의 모든 기존 replica는 OS family/release와 SSH 계정, 최소 디스크가 같아야 해요.
새 replica를 추가해도 기존 행의 node_id와 VMID, UUID, 상태는 바꾸지 않아요.
이미 있는 행은 모든 등록 항목과 UUID가 동일할 때만 no-op으로 확인하며 원래 상태와 수정 시각을 보존해요.
VMID나 metadata를 교체하려면 새 revision을 등록해 주세요.

출력이 정확하면 root 소유 0700 backup 디렉터리를 준비하고 적용해요.

```bash
sudo python3 -I scripts/register-image.py register \
  --inventory /root/image-registration/template.json \
  --inventory-sha256 REVIEWED_SHA256 --apply \
  --backup-dir /root/image-registration/registration-records
```

적용은 advisory lock과 node/image row 잠금을 잡고 preview의 행 전체를 다시 대조해요.
다른 작업이 바꾼 값은 덮어쓰지 않아요. DB의 새 행은 DISABLED이며 node는 MAINTENANCE로 남아요.
전후 자료는 0600 파일로 보존해요. 이미지나 node를 자동 활성화하지 않아요.

등록된 template의 같은 VMID를 재빌드하거나 기존 revision metadata를 바꾸지 마세요.
이미 승인된 VM은 원래 logical image UUID와 고정 clone metadata를 유지해요. 이름이나
OS/SSH/minimum-disk metadata가 바뀌면 재시도에서 거부해요. 이 논리 metadata hash는
hypervisor root가 디스크 내용만 바꾼 것은 검출하지 못해요. 새 build에는 새 VMID/revision과
새 build manifest를 사용하고 별도 생성과 접속 시험 뒤 활성화해 주세요.

## 실패와 회수

- 적용 전 실패하면 inventory는 바뀌지 않아요. SQL의 비교 실패는 전체 transaction을 되돌려요.
  재시도 전에 수집과 DB 상태를 다시 확인해 주세요.
- 오류가 나도 template이나 기존 catalog 행을 자동 삭제하지 마세요. 받은 JSON과 source manifest,
  기존 디스크를 보존해 주세요.
- 새 이미지를 사용하지 않기로 했으면 해당 UUID의 DISABLED 상태를 확인해요. 다른 노드의
  같은 이름/revision을 함께 비활성화하지 마세요. 이미 참조한 요청과 VM의 UUID는 유지해요.
- 실제 API schema/consumer 연결과 template의 부팅, 접속, 정책 검증은 별도예요.
  등록 완료를 다중 노드 생성이나 방화벽 집행 완료로 기록하지 마세요.

## 검증

```bash
python3 -B scripts/tests/test_image_registration.py
python3 -B scripts/tests/test_node_registration.py
```

기본 검사는 host 명령과 DB 응답을 대체해요. 실제 SQL 검사는 이미 설치된 PostgreSQL 이미지 digest를
`PICKLE_TEST_POSTGRES_IMAGE`에 지정해 별도로 실행해요. 네트워크가 차단된 일회용 컨테이너에서
새 DB만 만들고 끝나면 제거해요. 운영 DB나 PVE는 사용하지 않아요.

최종 갱신: 2026-11-03
