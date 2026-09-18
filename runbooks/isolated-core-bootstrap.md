# 격리 API와 PostgreSQL LXC 준비

`scripts/bootstrap-isolated-core.sh`는 기존 게스트와 DB를 재사용하지 않고 Debian 13
LXC 두 개를 새로 만든다. 하나는 PostgreSQL 18, 다른 하나는 Java 25와 nginx를
설치하는 API 및 콘솔 자리다. 기본 실행은 dry-run이며 `--apply`가 있어야 변경한다.

이 단계에서 API JAR, 콘솔 번들, 애플리케이션 스키마와 노드 인벤토리는 설치하지
않는다. 데이터 이관, 공개 도메인, NAT, 기존 proxy나 relay 연결도 수행하지 않는다.
API 유닛은 비활성화되고 `/etc/pickle/allow-api-start`가 없으면 시작할 수 없다.
JobRunr와 정책 producer는 꺼진 상태이며 정상 기동은 개발 시더가 없는 `isolated`
프로파일을 쓴다. 이 프로파일의 메일 구현은 외부 전송과 로컬 spool을 모두 거부한다.

## 실행 전 조건

- 대상 노드와 클러스터 이름을 명시한다. root와 hostname, 현재 quorum이 일치해야 한다.
- infra LXC 번호는 100–999 안에서 지정한다. 예시는 app 201, DB 204다. 번호는 예약이
  아니며 매번 클러스터 전체 QEMU/LXC 목록과 설정, 대상 스토리지의 잔여 볼륨을 검사한다.
- 기존 SDN bridge에 gateway와 측정한 MTU가 적용돼 있어야 한다. 이 스크립트는
  호스트 주소, bridge, forwarding, 방화벽이나 DNS를 변경하지 않는다.
- 전용 인프라망의 라우팅, 반환 경로와 사용자 VM망 격리가 먼저 준비돼 있어야 한다.
  예시 후보는 `pinfra`, `100.65.0.0/16`, gateway `100.65.0.1`이다.
- 호스트에 `python3`, `pvesh`, `pvesm`, `pct`, `ip`, `arping`, `openssl`이 필요하다.
  없는 도구를 호스트에 자동 설치하지 않는다. app/DB 주소는 ARP 중복 검사도 통과해야 한다.
- 공식 PVE Debian 13 amd64 템플릿을 별도로 받아 출처와 checksum을 확인한다.
  `pveam available --section system`으로 현재 항목을 고르고 다운로드와 검증을 마친 뒤
  volume ID와 SHA-256을 입력한다. 스크립트는 템플릿을 내려받거나 원본을 변경하지 않는다.
- API와 DB용 새 자격증명 및 DB TLS 자료를 외부 보호 파일로 준비한다. 기존 서비스의
  env 파일, DB dump나 archive는 이 도구의 입력이 아니다.

## 버전 기준

2026-09-16 공식 자료 확인 기준:

| 구성 | 확인 결과 |
|---|---|
| Debian | 13.7, 2026-09-12 발표. 정규 지원 2028-08-09, LTS 2030-06-30까지 |
| PostgreSQL | 최신 stable major 18, minor 18.6. 지원 종료 2030-11-14 |
| PGDG trixie amd64 | `postgresql-18`, `postgresql-client-18` 모두 `18.6-1.pgdg13+2`. 공식 키로 InRelease 서명을 확인하고 Packages SHA-256과 대조 |
| Java runtime | Debian security의 `openjdk-25-jre-headless` `25.0.4.1+1-1~deb13u1`. 애플리케이션의 Java 25 빌드 기준과 맞춤 |
| nginx | nginx.org stable `1.30.5`, trixie package `1.30.5-1~trixie` |

[Debian release](https://www.debian.org/releases/trixie/),
[PostgreSQL 지원 정책](https://www.postgresql.org/support/versioning/),
[PGDG 설치 방법](https://www.postgresql.org/download/linux/debian/),
[PGDG Release](https://apt.postgresql.org/pub/repos/apt/dists/trixie-pgdg/Release),
[Debian Java 패키지](https://packages.debian.org/trixie/openjdk-25-jre-headless),
[nginx Linux packages](https://nginx.org/en/linux_packages.html),
[nginx stable package index](https://nginx.org/packages/debian/pool/nginx/n/nginx/).

실제 설치 직전 다시 확인한다. 새 게스트는 Debian의 서명된 APT를 갱신하고 보안
업데이트를 설치한다. Debian 패키지 `postgresql-common`이 제공하는 공식 PGDG 설치기로
서명된 PGDG APT를 구성한다. PostgreSQL과 Java의 APT candidate가 입력 버전과 다르면
멈춘다. App LXC는 nginx.org stable repo key를 HTTPS로 받아 공식 fingerprint
`573BFD6B3D8FBC641079A6ABABF5BD827BD9BF62`가 포함됐는지 확인한 뒤 격리 keyring으로
dearmor하고 `signed-by`가 지정된 새 source file만 만든다. 공식 문서대로 다른 signing
key가 함께 있는 것은 허용한다. nginx candidate의 버전과 nginx.org origin이 모두 맞아야
설치하며 package receipt에도 남긴다. 새 major로 자동 전환하거나 서명 검사를 끄지 않는다.

## 입력 파일

보호된 입력 디렉터리와 실행 기록의 부모 디렉터리를 준비한다. 기록 부모는 root 소유
0700이어야 하고, 개별 실행 디렉터리는 존재하지 않아야 한다. private key, DB password,
API env는 root 소유 0600 파일로 준비한다. symlink는 거부한다.

DB 비밀번호 파일은 새로 생성한 base64 문자 32–128자의 한 줄이다. 값은 화면이나
명령 인수로 출력하지 않는다. `api.env`에는 아래 두 키만 실제 새 값으로 채운다.
주석과 빈 줄 외에는 `KEY=VALUE` 한 줄 형식이며 중복이나 다른 키는 거부한다.

```text
PICKLE_JWT_SECRET=<32자 이상의 새 값>
PICKLE_CREDENTIALS_KEY=<32바이트의 새 키를 base64로 인코딩한 값>
```

자동 생성 값에는 공백, quote, backslash, dollar와 backtick을 사용하지 않는다.
DB URL과 password, 실행 프로파일은 별도 `core.env`에 작성되며 이 입력으로 덮을 수 없다.
SMTP나 Proxmox token 등 다른 서비스 자격증명도 이 단계에는 넣지 않는다.

관리자 계정 입력은 별도 root 소유 0600 regular file에 아래 두 키만 둔다. 초기 LXC
준비에는 읽지 않고, `--bootstrap-admin --apply` 한 번에서만 app LXC에 임시 설치한 뒤
즉시 삭제한다. 값은 명령 인수와 journal에 넣지 않는다.

```text
PICKLE_BOOTSTRAP_ADMIN_EMAIL=<검증용 관리자 이메일>
PICKLE_BOOTSTRAP_ADMIN_PASSWORD=<16자 이상의 새 비밀번호>
```

DB server certificate는 `db_hostname`에 대한 인증서여야 한다. CA chain, hostname,
server 목적과 private key 일치를 검사한다. CA private key는 전달하지 않는다.
앱에는 CA 공개 인증서만, DB에는 server certificate와 0600 private key만 복사한다.
CA key와 재발급 절차는 두 LXC 밖에서 보관한다.

설정 예시다. `REVIEWED` 템플릿과 0으로 채운 checksum은 반드시 실제 확인값으로 바꾼다.
주소, 용량과 번호도 적용 대상의 계획값과 비교한다.

```json
{
  "expected_node": "pve-node-2",
  "expected_cluster": "example-prod",
  "bridge": "pinfra",
  "subnet": "100.65.0.0/16",
  "gateway": "100.65.0.1",
  "app_ip": "100.65.1.20",
  "db_ip": "100.65.1.21",
  "proxy_ip": "100.65.1.10",
  "nameserver": "8.8.8.8",
  "mtu": 1370,
  "app_ctid": 201,
  "db_ctid": 204,
  "app_hostname": "pickle-app",
  "db_hostname": "pickle-db",
  "app_cores": 4,
  "db_cores": 2,
  "app_memory_mb": 4096,
  "db_memory_mb": 4096,
  "app_disk_gb": 32,
  "db_disk_gb": 64,
  "storage_reserve_gb": 16,
  "storage": "local-lvm",
  "template": "local:vztmpl/debian-13-standard_REVIEWED_amd64.tar.zst",
  "template_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "postgresql_version": "18.6-1.pgdg13+2",
  "jre_version": "25.0.4.1+1-1~deb13u1",
  "nginx_version": "1.30.5-1~trixie",
  "db_name": "pickle_verify",
  "db_role": "pickle_verify",
  "api_env_file": "/root/isolated-core/inputs/api.env",
  "db_password_file": "/root/isolated-core/inputs/db-password",
  "db_ca_file": "/root/isolated-core/inputs/db-ca.crt",
  "db_cert_file": "/root/isolated-core/inputs/db-server.crt",
  "db_key_file": "/root/isolated-core/inputs/db-server.key",
  "state_dir": "/root/isolated-core/runs/first-bootstrap"
}
```

## 실행과 확인

```bash
# 어떤 위치에서 실행해도 기본은 계획 출력이다. 자격증명 내용이나 호스트 상태를 읽지 않는다.
bash scripts/bootstrap-isolated-core.sh --config /root/isolated-core/config.json

# 모든 사전 조건이 갖춰진 대상 노드에서만 실행한다.
bash scripts/bootstrap-isolated-core.sh --config /root/isolated-core/config.json --apply
```

실행은 다음 경계를 지킨다.

1. 기존 CTID, guest config, target volume, 기록 디렉터리를 발견하면 첫 생성 전에 거부한다.
2. 새 LXC에 `isolated-core:<run UUID>` description을 달고 작업 단계마다 소유권을 확인한다.
   CT의 onboot는 0으로 유지한다. 실패 뒤 자동 재실행이나 자동 삭제는 하지 않는다.
3. 새 게스트의 패키지 자동 서비스 시작을 임시로 억제한다. DB를 열기 전에 TLS와
   SCRAM HBA를 설치하고 app `/32`만 허용한다. localhost 관리 접속은 postgres peer다.
4. 게스트마다 전용 nft table을 적용한다. DB는 app의 TCP 5432, 앱은 전용 proxy의
   TCP 80만 새 연결로 받는다. API TCP 8080은 loopback에만 bind한다.
5. stock `nftables.service`는 새 게스트에서 mask하고 전용 firewall unit만 table을
   관리한다. nginx, PostgreSQL, API는 이 unit을 Requires/After로 요구한다. nginx에도
   proxy socket 주소 allow/deny를 두어 원본 IP header 신뢰가 방화벽 하나에만 의존하지 않는다.
6. app에서 `sslmode=verify-full`과 SCRAM으로 새 DB에 실제 연결하고 TLS 세션임을 확인한다.
   이 검사는 애플리케이션 테이블을 만들지 않는다.
7. `manifest.json`에 시도한 CTID, 생성 성공 CTID, run UUID, 입력 계획과 설치 package
   receipt, 각 LXC machine ID와 PostgreSQL system identifier를 남긴다. DB system
   identifier는 DB LXC의 postgres operator 경로로 읽으며 app 역할에 추가 권한을 주지
   않는다. credential 값은 넣지 않는다.

성공은 LXC 기반 준비와 private DB 연결까지만 뜻한다. API/콘솔 배포, schema와
MAINTENANCE 노드 등록, agent 연결, 사용자 VM, PBS 복구, RPO/RTO 검증은 별도다.
`allow-api-start`를 만들거나 JobRunr를 켜는 것도 그 다음 실행의 명시적 단계다.
DB가 별도 LXC이므로 app 컨테이너 안의 PostgreSQL을 가정하는 운영 명령을 재사용하지 않는다.

## 최초 관리자 one-shot

검증할 API jar를 `/opt/pickle/api/current.jar`에 설치한 뒤에도 정상 API 유닛은 disabled이고
marker는 없어야 한다. 다음 dry-run은 자격증명 내용을 읽거나 행을 쓰지 않고, exact host와
cluster quorum, manifest run UUID, 두 CTID/hostname/description/machine ID, privileged DB
system identifier, 빈 public schema와 app 역할의 verify-full TLS 연결을 확인한다.

```bash
bash scripts/bootstrap-isolated-core.sh --config /root/isolated-core/config.json \
  --bootstrap-admin --admin-env-file /root/isolated-core/inputs/bootstrap-admin.env

bash scripts/bootstrap-isolated-core.sh --config /root/isolated-core/config.json \
  --bootstrap-admin --admin-env-file /root/isolated-core/inputs/bootstrap-admin.env --apply
```

Apply는 root-only 입력을 app LXC에 임시 복사하고, secret이 없는 고정 argv로 transient
one-shot을 실행한다. Systemd manager가 세 보호 파일을 `EnvironmentFile`로 읽고 pickle
사용자로 직접 실행하므로 JDBC URL의 `&`나 이후 값이 shell 문법으로 해석되지 않는다.
App 역할의 TLS preflight도 strict Python loader가 env file을 데이터로 읽은 뒤 `psql`을
exec하며 비밀번호를 argv나 출력에 넣지 않는다. one-shot은 `isolated,isolated-bootstrap` profile과 명시 opt-in을 함께
요구하고, 한 transaction의 advisory lock 안에서 Flyway/JobRunr metadata 외 모든 app table이
비었는지 확인한다. 성공 시 verified ACTIVE SYS_ADMIN 한 명과 그 계정의 PERSONAL workspace,
OWNER membership만 존재해야 한다. 다른 settings, 기관, node/pool/image, relay, domain/route,
VM/신청, audit 행은 0이어야 한다. V87이 schema registry로 넣는 `openai`, `openrouter`,
`dgx` 세 `llm_upstreams` 행만 exact tuple로 허용하며 endpoint나 credential을 담지 않는다.
현재 bootstrap 계정 경로는 audit을 쓰지 않는다.

Postcheck가 exact 1/1/1 account/workspace/member와 나머지 0행, one-shot 종료 및 정상 API
유닛의 inactive/disabled 상태를 다시 확인한 뒤에만 `/etc/pickle/allow-api-start`를 만든다.
정상 API 서비스는 시작하지 않는다. 재실행은 schema-empty 검사에서 거부되며, 실패 뒤에는
행이나 marker를 임의로 지우지 말고 manifest와 DB를 먼저 조사한다.

## 실패 보존과 되돌리기

- `manifest.json`의 `attempted`에는 생성 도중 실패한 번호도 남는다. `created` 목록만으로
  부분 생성물이 없다고 판단하지 않는다.
- 먼저 해당 번호의 `pct config`를 읽고 hostname과 description의 run UUID를 모두
  대조한다. 불일치하면 멈추고 그 리소스를 변경하지 않는다.
- 이 실행이 만든 것으로 확인한 LXC만 `pct shutdown <확인한 CTID> --timeout 60`으로
  정지한다. 실패했다고 강제 파기하지 않는다. onboot는 처음부터 0이며 디스크와 DB,
  인증서는 보존한다.
- 원본 템플릿, 외부 입력 파일, 기존 게스트와 기존 DB는 되돌리기 대상이 아니다.
  새 역할과 DB의 삭제, LXC/볼륨 파기는 별도의 실제 대상 검토 뒤에 수행한다.
- 초기 입력과 manifest의 보관만으로 재해 복구가 검증되지는 않는다. CA key, DB,
  API 암호화 키와 platform 설정의 off-host 백업 및 수동 서비스 복구를 별도로 검증한다.

## 로컬 검증

```bash
python3 scripts/tests/test_isolated_core.py
shellcheck scripts/bootstrap-isolated-core.sh
bash scripts/verify.sh
```

자체 검사는 잘못된 노드와 quorum, 사용 중인 ID, 자격증명 입력 범위, 기본 dry-run,
private TLS HBA, host/CT/machine/DB identity와 schema-empty bootstrap guard, 비밀의 argv
비노출, marker 순서와 부분 생성 보존을 검증한다. 실제 LXC 생성이나
호스트 재부팅을 수행하는 검사가 아니다.

최종 갱신: 2026-09-16
