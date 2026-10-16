# 개발·운영 배포 경로 예시

예시 CT 번호·RFC 2544 주소·example.com 이름과 commit 자리표시자는 실제 환경 값으로 교체해야 합니다.

현재 배포 경로는 사전에 빌드한 서비스 commit의 artifact를 받습니다. `develop`은
example-dev-host의 새 개발 CT901, 릴리스된 `main`은 example-prod-host의 운영 CT911/203을 대상으로 합니다.
기존 example-dev-host CT904/103 원본 서비스와 DB hold는 해제하지 않습니다. 공개 CT900 중계와
CT903 SSH transit도 이 절차에서 바꾸지 않습니다.

## 대상 준비

두 호스트의 root 소유 `/etc/pickle/deploy-targets/{dev,prod}.json`은 이 레포의
`config/deploy-target-{dev,prod}.json`에서 각각 설치합니다. 실제 CTID·hostname,
systemd unit의 effective `ExecStart`·`EnvironmentFiles`, API profile과 DB URL,
nginx root를 읽어 설정 파일과 일치하는지 확인합니다. 자격증명 값은 출력하지 않습니다.
`allowed_commits` 세 SHA는 함께 검증한 API·console·gateway 조합입니다. 조합이
바뀌면 세 값을 함께 재검토한 뒤 host 설정을 갱신합니다. 지금 개발 gateway 값은
Chat 권한 집행 전판이므로 권한 집행판 검증 후 다시 고정해야 합니다. 이 값만으로
최종 호환 검증이 완료됐다고 판단하지 않습니다.

개발 API unit의 effective `ExecStart`는
`/usr/local/libexec/pickle-api-start.py /opt/pickle/api/current-dev.jar dev`입니다.
이 레포의 `scripts/pickle-api-start.py`를 root 소유 0755로 설치하고 설정의
`startup_wrapper_sha256`와 대조합니다. Wrapper는 root 소유·다른 사용자 쓰기 불가
release JAR와 active link의 상위 디렉터리 전체가 root 소유·다른 사용자 쓰기 불가이며
symlink가 아닌지 확인합니다. JAR를 FD9로 열어 유지하고 `java -jar /proc/self/fd/9`로 실행합니다.
Spring 설정은 JAR의 `classpath:/application.yml`만 읽고, 검토된 `PICKLE_*` 변수만
전달하므로 외부 config·Spring/JVM·loader override가 실행 설정을 바꾸지 못합니다.
개발은 `-Xmx1g`·`--server.address=127.0.0.1`, 운영은 기존 `-Xmx2g`·
`--server.address=0.0.0.0`과 JobRunr server true/dashboard false, network-policy true,
vm-firewall true, DNS google의 명시 CLI 선택을 보존합니다. `PICKLE_DEV_*` inventory·JobRunr·실연동
opt-in·기대 DB identity 변수도 whitelist에 포함합니다. 원격 개발 DB는
`PICKLE_DEV_DB_HOST=198.18.0.60`, `NAME=pickle_dev`, `USER=pickle_dev_api`,
`PORT=5432`이며 실제 `PICKLE_DB_USER`도 `pickle_dev_api`입니다. 초기 개발 VMID
start는 `PICKLE_DEV_VMID_START=900000`입니다. 초기 mock의 root domain은
`.test`/`.invalid`를 사용하며 공개 개발 console URL과 구분합니다.
새 서비스 설정 변수를 쓰려면 wrapper allowlist와 hash도 같은 배포 조합에서 갱신합니다.
Gateway unit은
`/opt/pickle/llm-gateway/bin/current-dev`를 실행해야 합니다. 대응 `llm-keygen-dev`도
관리형 symlink입니다. 개발 DB는 별도 CT105의 `pickle_dev`이고 개발 API URL은
`198.18.0.60`의 전용 DB를 `verify-full`과 개발 CA로 가리켜야 합니다. 개발 nginx root는
`/var/www/pickle-console-dev`입니다. 개발 환경의 mock 메일·외부 연동과
Proxmox/IPAM/LLM 사용량 격리는 별도로 검증한 뒤 배포를 열어야 합니다. 개발
`check`는 단일 env 파일, DNS noop, 비어 있는 Proxmox token, loopback mock
proxy/terminal/OpenRouter와 gateway control/upstream 주소를 비밀값 출력 없이 검사합니다.
개발 unit의 `Environment`, `PassEnvironment`, `UnsetEnvironment`가 비어 있지 않거나
manager가 application 변수를 주입하면 거부합니다. 선택 env 파일은 root 소유이며
다른 사용자 쓰기와 symlink를 허용하지 않습니다. 개발 API env 이름은 명시 allowlist로
제한하고 Spring/JVM/loader generic 변수는 거부합니다. Loopback URL은 prefix만
비교하지 않고 scheme·실제 hostname·port·userinfo 부재까지 검사합니다.
Console artifact에 운영 주소가 포함되면 배포를 거부합니다. `pickle-dev.example.com`의
공개 TLS와 CT900 경로는 별도 승인 전까지 확인된 접속 경로로 취급하지 않습니다.

현재 운영 API unit은 고정 `production-example/pickle-api.jar`를, gateway unit은 drop-in
`40-release-example.conf`로 고정 `llm-gateway-example`을 실행합니다(2026-10-16 수동 배포).
이전 판의 고정 경로도 남아 있습니다. 운영 API의 준비 대상 명령은
`/usr/local/libexec/pickle-api-start.py /opt/pickle/api/production-current.jar prod`이며,
gateway는 관리 symlink `current-production`과 기존 명령 경로 `llm-keygen`을
선택해야 합니다. 운영 `apply`는 고정 artifact unit을 허용하지 않습니다. 관리형 unit 준비 외에도
artifact에 결합된 root 보호 승인 창과 실제 PBS 독립 파일 복원 검증을 모두 요구합니다.
운영 unit은 관리형 경로로 전환하지 않은 고정 경로 상태입니다. 운영 호스트에는 아직 이 도구, `/etc/pickle/deploy-targets/prod.json`,
승인과 호환 기록 경로가 설치되지 않았습니다(2026-10-16 확인). 설치하면 `check`는 현재 고정 ExecStart와 기록된
artifact SHA를 읽기 전용으로 대조할 수 있습니다. 이 준비가 서비스 재시작이나 공개 중계 변경을
요구한다면 대상·영향·복구 방법과 작업 창을 정해 별도로 승인받습니다.

## 제한된 실연동 개발 후보

초기 mock `config/deploy-target-dev.json`은 보존합니다. 별도
`config/deploy-target-dev-real.json`이 승인된 실연동 후보이며 선택 host의 `dev.json`을
바꾸기 전에 기존 파일 bytes/소유/권한/hash와 되돌릴 경로를 별도 보호 폴더에 보존합니다.
CT901 방화벽, template9101, `example-dev@pve!dev-api`의 정확한 ACL과 DB 범위가 실제로
준비된 뒤에만 이 manifest를 선택합니다. Token 값은 파일에 담지 않으며 env의 값이
비어 있지 않은지만 확인합니다.

실연동 후보는 전용 DB `198.18.0.60:5432/pickle_dev`·`pickle_dev_api`·verify-full CA,
`PICKLE_DEV_REAL_INTEGRATIONS_ENABLED=true`, `PICKLE_DEV_JOBRUNR_ENABLED=true`,
PVE URL `https://example-dev-host:8006`과 `/etc/pickle/example-dev-proxmox-ca.crt`, node example-dev-host·vmbr3·
local-lvm, template9101을 고정합니다. 새 IPAM은 `198.18.10.0/24`, gateway `.1`,
예약 `.0–.99`·`.200–.255`이며 `PICKLE_DEV_IP_POOL_RESERVED_RANGES`는
`from`/`to` 두 객체의 single-quoted JSON입니다. 실제 DB의 JSON `dev_environment_marker=true`, node의 pool 연결, node/pool/image metadata와
`vmid_seq MINVALUE 900000 MAXVALUE 900009 NO CYCLE`을 읽어 manifest와 비교합니다.
실연동에도 `PICKLE_DEV_ROOT_DOMAIN=dev.pickle.test`, `PICKLE_DEV_ENABLE_GATEWAYS=false`,
DNS noop, SSH host `ssh.example.com`와 port22를 고정합니다. 새 platform public key는
비어 있지 않아야 하며 private key 값은 manifest에 담지 않습니다.
운영 DNS·메일·OpenRouter 자동 polling은 여전히 열지 않습니다. 공개 중계는 이 후보가
변경하지 않습니다. 현재 개발 pin은 검증했던 0.99 snapshot을 보존합니다. 승인된 0.100/V140 guard 재통합과 matching console의 검증·packaging 이후 최종 source pin을 갱신합니다. 이후의 별개 기능을 이 cutoff에 자동 포함하지 않습니다.

## 운영 DB 사본을 사용하는 개발 환경

`config/deploy-target-dev-copy.json`은 `development_mode=production-copy`와
API의 `PICKLE_DEV_DATA_MODE=production-copy`가 함께 있어야 선택됩니다. 배포 대상은
계속 dev이며 CT901/902·`pickle_dev`·전용 DB 로그인과 verify-full TLS를 사용합니다.
API profile은 `isolated`로 고정하고 관리자 MFA를 CLI로 켭니다. 이 profile은 개발 시더를
실행하지 않으며 메일 발송과 spool 기록을 거부합니다. 운영 계정·비밀번호 hash·MFA·
기관 권한·멤버십·이력·설정·node/VM/relay endpoint를 사본 안에 그대로 보존합니다.
운영 API보다 새 스키마의 기존 migration이 추가 필드와 Chat 권한을 변환할 수 있으므로
원본 dump의 row 검증과 migration 후 검증을 구분합니다.

운영 writer는 계속 실행하고 PostgreSQL의 일관된 snapshot dump를 별도 개발 DB에
복원합니다. 개인정보·ciphertext·암호화 key는 서버의 보호된 경로와 메모리에서만
처리하고 Mac·Git·로그에 저장하지 않습니다. 기존 합성 DB는
`pickle_dev_synthetic_20261013`으로 보존하고 incoming DB를 검증·migration한 뒤
개발 API만 정지한 창에서 `pickle_dev`를 선택합니다. 원본 운영 DB와 기존 개발 VM은
삭제하지 않습니다. 일반 배포 명령은 이 DB 복제나 이름 변경을 수행하지 않습니다.

원래 비밀번호는 DB의 hash로 유지합니다. 같은 MFA와 암호화한 VM 자격을 읽으려면
보호 전송한 `PICKLE_CREDENTIALS_KEY`가 필요합니다. LLM 본문과 OpenRouter 관리 자격은
각각 `PICKLE_LLM_BODY_READ_KEYS`·`PICKLE_OPENROUTER_CREDENTIAL_READ_KEYS`가
관련 row를 실제로 열 수 있는지 내용 출력 없이 확인합니다. 개발 JWT는 독립 key를
유지하므로 개발 주소에서 새로 로그인합니다. Google identity 연결 row는 보존하지만
개발 Google OAuth client는 별도 승인·구성 전까지 비활성입니다. 비밀번호 없는 Google
전용 계정의 로그인 시험은 그 구성을 마련한 뒤 진행합니다.

JobRunr worker·dashboard·direct LLM rollup/quota·OpenRouter polling·VM 제어와
외부 control bearer는 CLI에서 비활성으로 고정합니다. DNS는 `none`, proxy·terminal·
OpenRouter client는 loopback으로 고정하고 production-copy의 gateway 배포는 거부합니다.
CT901의 `inet pickle_dev_app`는 input CT900 HTTP만, output 전용 CT902 DB5432만
허용하며 IPv4/IPv6 기본 정책은 DROP입니다. `check`/`apply`는 이 실제 ruleset,
선택 process의 isolated/MFA/worker/control 인자, V140/failed0, fresh worker0,
보존 합성 DB 존재를 대조합니다. 복제한 JobRunr heartbeat는 2분 동안 과거 worker가
살아 있는 것처럼 보일 수 있으므로 row를 지우지 않고 시간이 지난 뒤 다시 확인합니다.

복구할 때는 개발 API를 정지하고 복제 DB를 별도 이름으로 보존한 뒤 기존 합성 DB를
`pickle_dev`로 다시 선택합니다. 보호 보관한 이전 개발 env·wrapper·manifest·nft bytes를
같은 조합으로 되돌리고 DB TLS·스키마·실행 artifact·readiness를 확인한 후 개발 API만
시작합니다. 복제 이후 시험으로 생긴 데이터도 보존하며 기존 백업·key는 삭제하지 않습니다.
운영 프로세스와 공개 중계는 이 절차에서 변경하지 않습니다.

## 기존 파일명의 명시 환경 진입점

`deploy-api.sh`, `deploy-console.sh`, `deploy-llm-gateway.sh`는 현재 같은 보호 target·artifact 도구를 호출합니다.
인자가 없는 예전 호출이나 `CTID`, `EXPECTED_CT_HOSTNAME`, `API_DIR`, `CONSOLE_DIR`,
배포 Vite flag로 대상을 선택하는 호출은 빌드·대상 조회·재시작 전에 거부합니다.
개발 manifest의 `forbidden_ctids`는 보존 source를 선택하지 못하도록 고정합니다.
API target health는 local actuator readiness만 허용하며 aggregate health로 바꾸면 거부합니다.

```bash
bash scripts/deploy-api.sh check dev <commit> <staged-JAR> <SHA>
bash scripts/deploy-console.sh check dev <commit> <staged-console-archive> <SHA>
bash scripts/deploy-llm-gateway.sh check dev <commit> <staged-daemon-keygen-archive> <SHA>
```

`apply`도 같은 인자 형식을 사용하며 운영 승인·실제 백업·관리형 unit 조건을 우회하지 않습니다.
호스트 체크아웃의 빌드나 기존 release 삭제는 세 진입점에서 수행하지 않습니다.

## 고정 artifact와 실행

수동 GitHub `deploy` workflow에 환경, 서비스, 40자리 commit SHA를 입력합니다.
workflow는 서비스 checkout의 SHA와 `origin/develop` 또는 `origin/main` 포함 관계를
확인하고 해당 트리의 `scripts/verify.sh`를 통과한 후 깨끗이 빌드합니다. API는
`clean package`로 오래된 migration 리소스를 제거합니다. artifact SHA를 기록하고
별도 runner의 배포 job에 artifact만 전달합니다. 배포 job은 선택한 GitHub
`dev` 또는 `prod` environment를 사용하고 배포 tool은 infra `main`에서 checkout합니다.
개발 job에는 example-prod-host 운영 키·주소·신뢰 핀을 주입하지 않습니다. 서비스 소스의 빌드·시험은 root SSH
키를 받는 runner에서 실행되지 않습니다. 전달된 SHA를 다시 확인하고 선택 호스트의
root 전용 `/root/pickle-deploy-staging/<commit>/<run-id>-<attempt>/`에 전송합니다.
2026-10-12 확인한 공식 GitHub Actions 릴리스 기준으로 Java setup은
[`actions/setup-java` v6](https://github.com/actions/setup-java/releases/tag/v6.0.1)을
선택했습니다. checkout·Node·Go setup은 기존 검증 workflow와 같은 v7 계열이고
job 사이 산출물에는 [upload-artifact v7](https://github.com/actions/upload-artifact/releases/tag/v7.0.2)과
[download-artifact v8](https://github.com/marketplace/actions/download-a-build-artifact)을 씁니다.
호스트에서 `deploy-environment.py check`가 CT·unit·env·profile·DB·artifact를
대조합니다. 개발 workflow는 API·console만 `apply`까지 실행합니다. Gateway는
활성 키 권한과 snapshot generation을 확인한 뒤 별도 적용하며, 운영 workflow는
모든 대상을 stage/check에서 멈춥니다. 운영 SSH는 기존 example-dev-host 신뢰 핀을 통한
ProxyJump와 example-prod-host 자체 신뢰 핀·키를 사용합니다. 이 경로의 도달성과 GitHub 비밀 설정은
실제 stage/check 전에 별도 확인해야 합니다. 공개 중계 설정은 workflow가 바꾸지 않습니다.

`config/deploy-target-prod.json`은 실제 운영 실행본의 source SHA를 사용합니다. 2026-10-16부터
0.104/V141 판이고, 산출물은 고정 artifact 수동 배포로 올렸습니다. 별도
`config/deploy-target-prod-release-candidate.json`은 그 전의 미활성 0.100/V140 미집행 후보이며
이번 배포에 쓰지 않았습니다. 파일에 남은 0.99 API·console SHA는 과거 검증한 snapshot이며 최종 0.100 source로 확인된 값이 아닙니다. API·console 새 source, 검증된 Chat 집행 gateway source와 세 artifact
SHA를 함께 검증하기 전에는 `release_candidate_ready=false`이며 apply를 거부합니다.
Gateway placeholder와 미확인 digest를 제거하지 않고 READY로 바꿔도 거부합니다.
운영 workflow는 `check`까지만 실행합니다. `<SHA>`는 artifact SHA-256,
`<commit>`은 실제 서비스 commit입니다. `--backup-point` 문자열이나
`PICKLE_PRODUCTION_RELEASE_APPROVED`는 운영 적용을 허용하지 않습니다.

`config/deploy-backup-reader.json`은 example-backup-host 독립 DB reader의 현재 선택 profile,
배치된 기존 native library SHA, 실제 DB instance/system ID와 현재 스키마 버전(2026-10-16부터 V141)을 고정한 후보입니다.
백업 reader 호스트에 설치한 사본도 레포 판과 같아야 하므로, 이 파일이 바뀌면 호스트 사본을 같은 소유와 권한(root 0600)으로 다시 설치합니다.
이 파일과 `verify-production-backup.py`를 별도 root 보호 경로에 stage하고, example-prod-host→example-backup-host
reader 및 example-backup-host→example-prod-host의 native PostgreSQL 읽기 actor를 각각 기존 신뢰 핀·키의 새 전용
SSH 설정으로 연결합니다. 설정의 `postgres_restore_command`는 기존 CT204의
`pg_restore --list`와 dump의 Flyway 데이터 출력만 사용합니다. SQL을 실행하거나
DB를 만들지 않습니다. `/root/pickle-release-backup-readback`은 root0700으로 준비합니다.
현재 배치된 monitor/profile/library는 수정하지 않습니다.

독립 verifier는 실제 PBS의 최신 complete data/proof pair를 조회하고 암호화 manifest와
receipt를 대조한 뒤 같은 point를 새 임시 폴더로 복원합니다. `database.dump`·`roles.sql`·
`manifest.json`의 실제 SHA, native PostgreSQL TOC와 dump에 들어 있는 Flyway COPY를
검사합니다. 임시 plaintext는 성공·실패 모두 제거합니다. 최신 point는 600초 이내여야 하고,
root 임의 JSON이나 과거의 SQL 복원 기록만으로 이 확인을 대신하지 않습니다. 호출자는
매번 새 challenge와 verifier SHA를 대조하고 실제 CT204의 system ID·현재 스키마 버전·failed0도 읽습니다.
이 경로는 독립 파일 복원·TOC·Flyway 데이터 검증이며 전체 SQL clone/service RTO 시험이
아닙니다. 기존 완료한 full restore는 보존합니다. 운영 manifest의 `historical_full_sql_restore`는
이미 완료된 native SQL producer 경로와 SHA를 연결합니다. 이 파일은 root0600으로
존재하는 것을 읽기 전용 확인했으며 이번에 SQL 복원을 반복하지 않았습니다. 과거 drill이
현재 point의 full SQL 복원을 의미하지 않으므로 다음 승인된 릴리스 실행안에서 필요한
전체 SQL 복원 범위를 따로 정합니다. 실제 연결·선택적 readback은 부모 실행 단계에 남아 있습니다.

```bash
python3 scripts/deploy-environment.py check prod api <commit> \
  /root/pickle-deploy-staging/<commit>/<run-id>-<attempt>/artifact.jar <SHA>
# Independent native PBS file-recovery path, after its protected actor is wired.
python3 scripts/deploy-environment.py check prod api <commit> \
  /root/pickle-deploy-staging/<commit>/<run-id>-<attempt>/artifact.jar <SHA> --backup-check
```

향후 운영 `apply`에는 `/etc/pickle/deploy-approvals/prod.json`이 필요합니다. operator가
별도 승인한 실제 작업 창을 `authorization_reference`로 연결하고 `environment=prod`,
`host`, `target`, `commit`, `artifact_sha256`, `approved_by=operator`, Unix timestamp
`approved_at`·`expires_at`와 명시적으로 수용한 `accepted_recovery_scope`를 기록합니다.
창은 최대 15분이고 현재 artifact·복구 범위와 같아야 합니다. 파일 자체가 인간 승인을
만드는 것은 아니며, 이 준비 작업은 그 파일을 작성하지 않습니다. apply는 lock 안에서
fresh 실제 backup verifier를 다시 호출하고, 검증 후에도 승인 창이 유효한지 확인합니다.
현재 fixed production unit은 이 조건과 별개로 apply를 거부합니다.

이 릴리스의 V139는 이전 API가 신규 키 신청을 저장하지 못할 수 있으므로 개발·운영
설정 모두 `api_rollback_policy`를 `fix-forward`로 고정합니다. API 적용에서 정책이
없거나 CLI 지정이 manifest와 다르면 거부합니다.

Gateway는 `artifact.tar.gz`에 `llm-gateway`와 `llm-keygen` 두 바이너리를 함께
넣습니다. Console은 `index.html`을 포함한 `dist/` tarball을 받습니다. 해당 대상의
명령에서도 `check`를 먼저 실행합니다. API·gateway는 versioned release를 남기고
관리형 symlink를 바꾸며 console은 이전 webroot를 별도 이름으로 보존합니다.
API는 MainPID의 열린 JAR FD9, gateway는 `/proc/<MainPID>/exe`의 inode·SHA를
선택 artifact와 대조하며 PID start time도 앞뒤로 확인합니다. Readiness만 응답하는
구판 프로세스는 성공 receipt를 받지 못합니다. Console archive의 uid/gid·mode를
그대로 적용하지 않고 root 소유, directory 0755, file 0644로 정규화합니다.
특수 mode·중복 경로·link entry는 archive 검증에서 거부합니다.
API readiness와 gateway health는 대상 내부,
운영 gateway는 CT200에서 198.19.0.40:8081로 검사합니다. 실패 시 호환성이 확인된
API·gateway는 이전 symlink로 복구하고 console은 이전 webroot로 복구합니다.
`fix-forward`인 개발·운영 API는 이전 JAR로 자동 복구하지 않고 작업 창의 수동 복구안을
따릅니다. 기존 백업과 원본 release는 지우지 않습니다. 외부 공개 응답과 실제 기능은
별도 확인합니다.

Chat 권한이 포함된 릴리스는 API·console을 먼저 적용하고 snapshot 호환성을
검증한 다음 gateway를 적용합니다. 성공 receipt의 manifest SHA와 실제 실행 API
artifact, console index SHA, V139 success 및 applied generation을 대조합니다.
모든 ACTIVE 키에 `chat`을 요구하지 않습니다. 신규 API에서 의도적으로 Chat 없이
발급한 정상 키도 유지해야 하기 때문입니다.

독립 검증 후 호스트 root 소유·다른 사용자 쓰기 불가
`/var/lib/pickle-deploy/<env>/chat-compatibility.json`을 작성합니다. 이 결과는
migration 전 기존 키 baseline의 grant 보존, 현재 gateway snapshot과 DB grant
동일성, 의도적으로 Chat 없는 신규 키의 결과를 실제로 확인한 뒤에만 작성합니다.
파일 자체는 그 확인을 대신하지 않습니다. 필드는 `environment`, manifest 세 SHA의
`commits` 객체, receipt의 `api_sha256`·`console_index_sha256`, 확인한 `generation`,
Unix timestamp `checked_at`, 실제 확인 후 true인 `existing_key_grants_preserved`·
`snapshot_grants_match_database`·`chat_free_keys_verified`입니다. 결과는 15분 이내,
현재 commit·artifact·generation과 같아야 적용을 허용합니다. 파일 누락·미확인·stale
결과는 거부합니다. 현재 gateway manifest는 권한 집행 전판이므로, 집행판 commit을
검증·고정한 뒤 이 순서를 실행합니다.

V139 이후 예전 API는 `chat`을 snapshot에서 빠뜨릴 수 있으므로 호환되는 복구가
필요할 때 gateway를 먼저 구판으로 돌립니다. API migration은 jar rollback으로
되돌아가지 않습니다.

`sshgw`, `proxy-agent`, `relay`는 이 workflow의 대상이 아닙니다. 각자의 기존
수동 절차와 별도 영향 검토를 따릅니다.

## 개발 CI 키와 frozen receiver 준비

개발 호스트에 고정 receiver·tool·digest pin과 신규 제한 key row를 설치하고 호스트 내부 실제 check 및 명령·digest 거부를 검증했습니다. Key의 추가 custody와 GitHub secret 등록은 미완료이며 외부 CI 연결을 검증했다고 표현하지 않습니다.
`dev-deploy-receiver.py`는 root 보호 고정 설정과 설치한 `deploy-environment.py`의 SHA를
검사합니다. 새 key row는 `restrict,command="/usr/bin/python3 -I /opt/pickle/dev-deploy/receiver.py"`
만 허용하며 기존 운영 key와 authorized row는 그대로 보존합니다. PTY·port/agent/X11 forwarding·
user rc는 key의 restrict 조건으로 차단합니다. Shell·SCP·SFTP·임의 도구 upload는 받지 않습니다.

SSH 원격 명령은 정확히 `pickle-dev-deploy-v1`입니다. stdin 첫 줄의 strict JSON은
version1·action check/apply·target·commit·sha256·size만 받습니다. 중복/추가 key, prod 환경,
잘못된 commit·size·trailing payload는 거부합니다. 고정 CT·DB와 source 제외 목록이 선택
manifest와 같아야 합니다. 수신은120초, 압축 크기는 target별 상한, console/gateway의
expanded size·entry 수도 제한합니다. 같은 commit·SHA 경로의 기존 artifact는 덮어쓰지
않고 digest를 대조합니다. 전용 lock과 소유한 incoming temp만 정리합니다. 설치된 trusted
도구만 최대300초 실행하며 timeout에 전체 process group을 종료합니다. Gateway Chat 순서
검증은 이 도구에 그대로 남습니다.

준비 순서는 frozen code/config·root0700 staging/lock 경로 확인 → 새 host-only key 생성 →
기존 authorized rows의 byte/hash/소유/권한 보존과 새 restricted row 단독 append → 제한 호출
negative test → GitHub `dev` environment 준비와 새 secret 등록입니다. 실제 적용은 독립
검토 후 수행합니다. `prod` environment는 main deployment branch와 operator review를
요구하도록 설정해야 합니다. 이번 코드 준비는 기존 environment·secrets를 수정하지 않았습니다.

`prepare-dev-deploy-key.py generate`는 새 `/root/pickle-dev-deploy-key`가 이미 있으면
거부합니다. Private key 원문은 example-dev-host에만 남기고 Mac·Git·로그로 가져오지 않습니다.
`install-row`는 읽어 확인한 authorized-row SHA와 receiver SHA를 요구하며 기존 bytes를
`/pickle/backup/dev-deploy-key/`의 별도 폴더에 보존하고 새 row만 append합니다. 이 backup은
보존 기한이 있는 복구 사본이며 private key의 유일한 보관 위치가 아닙니다.

GitHub의 [environment public-key/secret API](https://docs.github.com/en/rest/actions/secrets#create-or-update-an-environment-secret)
metadata를 Mac에서 읽어 root 보호 public-key JSON으로만 example-dev-host에 전달합니다. example-dev-host에서는
libsodium sealed box로
private key를 암호화하고 `key_id`·`encrypted_value` JSON만 Mac으로 가져옵니다. Vendor PAT는
호스트로 복사하지 않습니다. Mac은 ciphertext JSON을 API body로 등록하며 plaintext를 읽지 않습니다.

현재 repository admin metadata는 true이나 environments 목록은 비어 있고 `dev` public-key는
404였습니다. 이는 신규 환경 준비 전 상태이며 secret write 성공을 뜻하지 않습니다. example-dev-host의
기존 libsodium도 없으므로 global package 설치 대신 signed Debian stable package를 protected
stage에 download/extract하고 library SHA를 고정하는 후보입니다.
[Debian trixie의 libsodium23](https://packages.debian.org/trixie/libsodium23)은 확인 시
1.0.18-1+deb13u1이며 실제 실행 직전 APT candidate·signed Release/package hash를 다시
확인합니다. `seal --libsodium-library <protected-so> --libsodium-sha256 <SHA>`는 이 pin과
root 보호 파일을 검증합니다. Dependency·환경 권한이 준비되지 않으면 키를 외부로 복사하여
우회하지 않고 등록 단계의 구체적 남은 결정을 operator에게 전달합니다.

## 운영 release·rollback 실행안

현재 운영 baseline의 파일을 별도 root 보호 staging으로 복사한 check는 운영 변경 승인이
아닙니다. Console snapshot과 gateway archive의 SHA는 설치 파일 복사본의 SHA이며,
commit과의 연결은 보존된 baseline metadata입니다. 독립 rebuild provenance로 표현하지 않습니다.

API·gateway의 현재 unit은 고정 artifact를 직접 실행합니다. `check`는 manifest의 고정 경로와
SHA를 대조하여 허용하지만 `apply`는 managed artifact 경로를 선택하지 않는 unit을 거부합니다.
Startup wrapper 설치·unit 전환·restart는 운영 창에서 별도 승인한 실행안의 작업입니다.
현재 운영의 JVM 옵션·환경 파일·gateway 설정을 이 전환에서 보존하고, 실제 process artifact를
API의 열린 JAR FD/inode와 gateway의 `/proc/PID/exe`로 다시 확인해야 합니다.

실행 전에는 새 release source와 artifact digest를 확정하고, API/console과 호환되는 Chat
강제 gateway 구현을 먼저 병합·검증합니다. 비활성 future manifest의 placeholder와 readiness
false를 임의로 해제하지 않습니다. 승인 파일은 정확한 source/artifact·작업 scope·짧은 승인
창에 결합합니다. 기존 고정 unit의 전환과 실제 apply는 이 승인에 명시해야 합니다.

Backup reader의 보호된 SSH config와 private temporary root를 준비한 후 fresh paired point에서
실제 payload 3개를 selective restore하고 SHA, native TOC, 복구된 Flyway history를 검증합니다.
이 검증은 full SQL clone restore가 아닙니다. 과거 SQL drill receipt의 파일과 SHA를 재확인하고,
현재 point의 SQL clone gate가 필요한 release에서는 승인 실행안에 따로 넣습니다. 현재 경로에
과거 receipt가 없으면 drill 통과라고 주장하지 않고 자료 위치를 확인할 때까지 gate를 남깁니다.

API rollback은 manifest의 fix-forward 정책을 따릅니다. Migration을 적용한 DB에 이전 JAR를
자동 재활성화하지 않습니다. 실패 시 approved rollback 계획의 DB 복구 point·single-writer
중지·호환 artifact 조합을 확인한 뒤 실행합니다. Console/gateway도 호환성을 확인한 이전
artifact만 선택하며 Chat enforcement의 후퇴를 자동 허용하지 않습니다. 실행 전후 PID와
artifact/env hash, schema, compatibility proof, readiness를 기록합니다.

### Dev artifact pin 갱신

Dev manifest의 `approved_artifacts[target][source_commit]`는 승인된 artifact SHA-256을
고정합니다. Receiver는 header의 digest와 optional size를 이 pin과 대조하고 deploy tool도
check/apply 진입 시 digest pin을 요구합니다. 같은 commit label을 붙인 다른 파일은 거부합니다.
Gateway의 검토된 archive pin이 없으면 check/apply 모두 거부합니다.

Pin 갱신은 실제 배포할 artifact의 hash·source provenance와 필요한 검증 결과를 검토한 뒤
호스트 root 보호 manifest를 수동 갱신하는 절차입니다. CI rebuild가 다른 digest를 만들면
자동 허용하지 않습니다. 실제 CI artifact를 확보해 검증하고 그 digest를 명시적으로 승인해야
합니다. Commit label만으로 재현 가능한 build provenance를 증명했다고 표현하지 않습니다.
Tool/receiver 코드가 변경되면 새 SHA의 독립 검토와 설치 config pin을 대조하며, 기존 SSH
authorized row bytes를 보존한 CAS 절차로 필요한 변경만 적용합니다.
