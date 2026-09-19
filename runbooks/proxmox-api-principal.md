# Proxmox API principal 등록 런북

이 런북은 기존 Proxmox 클러스터에 Pickle 서비스용 API principal을 추가하는 절차입니다.
실제 등록 전에는 예시 값을 승인된 환경 inventory로 바꾸고 별도 보호 작업 디렉터리에서 실행합니다.
이 문서는 등록 완료를 주장하지 않습니다.

## 적용 범위와 중단 조건

- 작업자는 예상한 Proxmox 노드의 root 세션에서 실행합니다. hostname, 기대 cluster 이름,
  quorum과 online node 집합이 다르면 중단합니다.
- 예시 환경은 pve-node.example.test, pickle-prod-example, 198.51.100.20,
  198.51.100.21, SDN zone prodtest, storage local-lvm을 사용합니다. 실제 값은
  승인된 환경 inventory에서 넣으며 이 문서와 Git에는 실값을 기록하지 않습니다.
- 작업 전 /etc/pve/user.cfg를 root 소유 0600 보호 파일로 복사하고 SHA-256을 기록합니다.
  기존 user, role, ACL, token, cluster 설정은 삭제하거나 덮어쓰지 않습니다.
- principal, custom role 또는 대상 ACL이 이미 존재하면 기존 상태를 보존한 채 중단합니다.
  이 절차는 새 identity를 만드는 작업입니다.
- pve1이나 다른 클러스터의 계정·ACL에는 접근하지 않습니다.

## 권한 모델

서비스 user와 API token은 같은 네 개 ACL을 받고 token은 privilege separation을 사용합니다.

| 이름 | 권한 |
|---|---|
| PickleProvisioner | Datastore.AllocateSpace, Datastore.Audit, SDN.Use, VM.Allocate, VM.Audit, VM.Clone, VM.Config.CPU, VM.Config.Cloudinit, VM.Config.Disk, VM.Config.Memory, VM.Config.Network, VM.Config.Options, VM.GuestAgent.Unrestricted, VM.PowerMgmt |
| PickleClusterAudit | Sys.Audit |

ACL 대상은 아래 네 개로 고정합니다.

| 경로 | 역할 |
|---|---|
| / | PickleClusterAudit |
| /vms | PickleProvisioner |
| /storage/local-lvm | PickleProvisioner |
| /sdn/zones/prodtest | PickleProvisioner |

Group.*, Permissions.Modify, User.Modify, Sys.Modify와 cluster-wide 변경 권한은 추가하지
않습니다. 루트에는 감사 권한만 둡니다.

## 사전 검사와 보호 기록

1. 작업별 root 소유 0700 디렉터리를 새로 만들고 manifest를 0600으로 기록합니다.
   기존 디렉터리나 manifest가 있으면 덮어쓰지 않습니다.
2. cluster status에서 기대 cluster, quorum, online node 집합을 확인합니다.
3. pveum user list, pveum role list, pveum acl list를 읽고 새 이름과 충돌하지 않는지
   확인합니다.
4. SDN zone prodtest의 존재와 type을 읽습니다. zone 자체는 만들거나 수정하지 않습니다.
5. user.cfg 보호 사본과 SHA-256, 읽은 user/role/ACL 목록, preflight 시각을 manifest에
   남깁니다. 비밀번호·token·key 값은 manifest에 넣지 않습니다.

## 생성 순서

각 단계가 성공할 때마다 manifest를 원자적으로 갱신합니다. 실패하면 이미 생성된 항목을
자동으로 지우지 않고 실패 상태와 소유 범위를 보존합니다.

1. PickleProvisioner와 PickleClusterAudit custom role을 정확한 권한 목록으로 생성합니다.
2. pickle@pve user를 enable 상태와 expire 0으로 생성합니다. 비밀번호는 만들지 않습니다.
3. ACL 네 개를 user에 propagate 1로 부여합니다.
4. pickle@pve!pickle-api token을 privsep=1, expire 0으로 생성합니다. token 생성 명령의
   stdout은 root 소유 0600 파일로 직접 redirect하고 화면, shell 변수, journal, manifest에는
   출력하지 않습니다. 생성 뒤 JSON의 full-tokenid와 privsep, expire는 privsep의 숫자 1 또는
   문자열 1, expire의 숫자 0 또는 문자열 0만 각각 허용 목록으로 확인합니다. 이 metadata
   오류로 token을 재발급하지 않습니다.
5. 같은 ACL 네 개를 token에 propagate 1로 부여합니다. token 값은 보호된 파일에서만 읽습니다.

## 단계별 pveum 명령 예시

사전 검사와 보호 기록이 끝난 뒤 아래 단계를 각각 실행합니다. 각 명령이 성공한 뒤
manifest를 갱신하고 다음 단계로 진행합니다. 실제 값은 승인된 inventory로 바꾸며 이
블록 전체를 한 번에 붙여넣지 않습니다.

```bash
set -eu
USER_ID='pickle@pve'
TOKEN_ID='pickle-api'
FULL_TOKEN_ID="${USER_ID}!${TOKEN_ID}"
TOKEN_FILE='/root/principal-bootstrap/token.json'
PROVISIONER='Datastore.AllocateSpace,Datastore.Audit,SDN.Use,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.GuestAgent.Unrestricted,VM.PowerMgmt'
pveum role add PickleProvisioner --privs "$PROVISIONER"
pveum role add PickleClusterAudit --privs Sys.Audit
pveum user add "$USER_ID" --comment 'Pickle isolated platform service account' --enable 1 --expire 0
pveum acl modify / --users "$USER_ID" --roles PickleClusterAudit --propagate 1
pveum acl modify /vms --users "$USER_ID" --roles PickleProvisioner --propagate 1
pveum acl modify /storage/local-lvm --users "$USER_ID" --roles PickleProvisioner --propagate 1
pveum acl modify /sdn/zones/prodtest --users "$USER_ID" --roles PickleProvisioner --propagate 1
umask 077
( set -o noclobber; pveum user token add "$USER_ID" "$TOKEN_ID" --privsep 1 --expire 0 --comment 'Isolated platform API' --output-format json > "$TOKEN_FILE" )
chmod 600 "$TOKEN_FILE"
# Metadata only; never select or print .value.
jq -e --arg full "$FULL_TOKEN_ID" '(."full-tokenid" == $full) and (.info.privsep == 1 or .info.privsep == "1") and (.info.expire == 0 or .info.expire == "0")' "$TOKEN_FILE" >/dev/null
pveum acl modify / --tokens "$FULL_TOKEN_ID" --roles PickleClusterAudit --propagate 1
pveum acl modify /vms --tokens "$FULL_TOKEN_ID" --roles PickleProvisioner --propagate 1
pveum acl modify /storage/local-lvm --tokens "$FULL_TOKEN_ID" --roles PickleProvisioner --propagate 1
pveum acl modify /sdn/zones/prodtest --tokens "$FULL_TOKEN_ID" --roles PickleProvisioner --propagate 1
```

## 등록 후 검증

- user와 token의 ACL 집합이 동일한 네 ACL과 두 역할이고 propagate가 1인지 확인합니다.
- PickleProvisioner가 정확히 14개 권한만 가지고 PickleClusterAudit가 Sys.Audit만 가지는지
  확인합니다.
- token이 privsep=1이고 user/token identity가 기대한 이름과 일치하는지 확인합니다.
- API token으로 cluster status, cluster firewall options, firewall group 목록, 대상 node
  firewall options와 대상 template config를 GET합니다.
- cluster firewall options의 현재 digest를 읽은 뒤 동일한 enable 값과 digest를 PUT으로
  보내는 harmless probe가 403을 반환하는지 확인합니다. 성공하면 권한 분리가 실패했으므로
  즉시 중단합니다.
- 검증 결과, user.cfg 새 SHA, token 파일 mode, manifest 경로만 보고합니다. token 값과
  전체 response에는 secret이나 개인 데이터를 넣지 않습니다.

이 검증은 API 권한과 읽기 범위를 확인합니다. VM packet acceptance, public ingress,
사용자 VM lifecycle과 복구 목표를 증명하지 않습니다.

## 부분 실패와 소유 자원 복구

부분 실패 시 보호 디렉터리와 manifest를 보존하고 새 token이나 role을 임의로 다시 만들지
않습니다. 복구가 승인되었을 때도 manifest가 이번 실행에서 생성했다고 명시한 자원만 처리합니다.

1. 새 token을 revoke합니다.
2. 새 token ACL을 제거합니다.
3. 새 user ACL을 제거합니다.
4. 새 user를 삭제합니다.
5. 새 custom role을 삭제합니다.
6. 복구 후 user.cfg SHA와 user/role/ACL 목록을 독립적으로 다시 읽습니다.

ownership이나 user.cfg가 예상과 다르면 복구를 멈추고 운영자가 판단합니다. 기존 role·ACL·
user·token은 이 절차가 소유하지 않으므로 삭제하지 않습니다. token 생성 응답이 불완전하거나
파일 mode가 0600이 아니면 값을 출력하지 말고 파일과 manifest를 보존한 채 중단합니다.

## 실행 전 체크리스트

- [ ] hostname, cluster quorum, online node 확인
- [ ] user.cfg 0600 보호 사본과 SHA-256 기록
- [ ] 기존 user·custom role·ACL·token 충돌 없음
- [ ] Provisioner 14 privileges와 ClusterAudit=Sys.Audit 고정
- [ ] user/token 네 ACL과 두 역할, propagate 1 고정
- [ ] token stdout 직접 0600 저장, 값 비출력
- [ ] token metadata의 숫자/문자열 1·0 허용과 duplicate guard 확인
- [ ] 지정 GET 성공과 동일 값 PUT 403 확인
- [ ] partial failure 시 소유 기록 보존 및 새 token만 복구 대상으로 제한
