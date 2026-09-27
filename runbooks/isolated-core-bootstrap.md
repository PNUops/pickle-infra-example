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
- 새 게스트를 시작한 직후 소유권을 다시 확인하고 네트워크 manager를 확인한다. `ifupdown2`가
  설치된 경우 `/etc/network/ifupdown2/ifupdown2.conf`에 `addon_scripts_support=1`이 정확히 있어야
  하며, 기존 `ifupdown`이 설치된 경우에도 hook 지원을 확인한다. 알 수 없는 manager나
  비활성 addon 지원이면 중단한다. `/etc/network/if-pre-up.d`가 없을 때만 root:root
  `0755`로 `install -d`하고, 이미 있으면 symlink/non-directory이거나 group/other 쓰기
  권한이 있는지와 소유자를 확인한다. 기존 directory의 권한은 넓히거나 임의로 고치지 않는다.
  그 뒤 `/etc/network/if-pre-up.d/isolated-core-mtu`를 root 소유 실행 파일로 설치한다.
  이 hook은 `IFACE=eth0`일 때만 검증된 guest MTU를 적용하고 다른 인터페이스에서는 아무
  작업도 하지 않는다. 부트스트랩은 hook 설치 뒤 `/usr/sbin/ip -j link show dev eth0`로
  실제 MTU를 즉시 확인한 뒤에만 APT/package 단계로 진행한다. 이 방식은 Debian ifupdown
  hook 규약에 따른다: [interfaces(5) hook scripts](https://manpages.debian.org/trixie/ifupdown/interfaces.5.en.html#HOOK_SCRIPTS).
- 실패 시 자동으로 기존 네트워크 directory를 삭제하거나 chmod하지 않는다. 소유권과 실행
  기록을 확인한 뒤 이번 실행에서 새로 만든 hook만 제거하고, 이번 실행에서 만든 parent가
  비어 있을 때만 함께 제거한다. 사전에 존재한 directory와 다른 파일은 보존한다.
- DB 게스트에는 `networking.service.d/10-isolated-core.conf`를 설치해 private firewall이
  먼저 완료되도록 하고, PostgreSQL drop-in은 `networking.service`를 `Requires`/`After`로
  요구한다. root 소유 `isolated-core-db-network-preflight`가 `/usr/sbin/ip -j address
  show dev eth0` 결과에서 설정된 IPv4와 MTU를 정확히 확인하며, 불일치하면 네트워크를
  고치지 않고 PostgreSQL 시작을 실패시킨다. 부팅 검증은 `systemctl is-active`만으로
  끝내지 말고 private `5432` 연결과 App의 `verify-full` query까지 확인한다.

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

실행 프로세스의 보호용 umask와 PVE helper의 umask를 구분한다. manifest와 입력은
0700/0600을 유지하지만 외부 명령은 0022로 실행한다. 0077이 그대로 전달되면
PVE가 만든 container 경로가 0700이 되어 mapped root의 archive 추출이 실패할 수
있다. 게스트에 전달하는 시크릿은 복사 명령의 `--perms`로 처음부터 0600/0640을
적용하며, 복사 후 chmod에만 의존하지 않는다. 기존 시스템 디렉터리의 권한을 일괄 변경하지 않는다. 실패한 실행의 config와
volume이 없는지 확인하고, 그 실행이 만든 비어 있는 디렉터리만 정리한 뒤 새 실행
기록 디렉터리로 다시 시도한다.

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
   `pct config`는 description의 `:`와 마지막 줄바꿈을 `%3A`, `%0A`로 표시할 수 있다.
   확인기는 이 값을 정확히 한 번만 decode하고 마지막 줄바꿈 하나만 정규화한다. 이중 인코딩,
   중간 줄바꿈, 다른 hostname이나 run UUID는 소유권 불일치로 거부한다.
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

### 후보 콘솔 첫 배포

`deploy-console.sh`는 호스트의 console 체크아웃에서 빌드한 뒤 app LXC의 nginx를
reload하고, 게스트의 `127.0.0.1:80`에서 index와 JavaScript 번들을 확인한다. 후보
설정은 app 전용망 주소와 loopback에서만 80번 포트를 듣는다. 전용망 요청은 지정한
proxy 주소만 허용하고, loopback 요청은 게스트 내부에서만 허용한다.

대상 호스트에 infra와 검증할 console 커밋의 배포 체크아웃, Node.js 24 이상과 npm이
먼저 준비되어야 한다. 대상 노드에 이 체크아웃과 Node/npm, 후보 CT 백업이
없다면 아래 적용 단계로 진행할 수 없다. `deploy-console.sh`는 호스트에서 `npm ci`와
console 전체 검증을 실행한다. LXC에 Node.js를 설치하는 것으로 대신할 수 없다.
배포 루트는 실제 준비한 경로로 정한다. `/pickle`을 쓰려면 먼저 그 경로에 두
체크아웃을 준비해야 한다.
검증 단계는 두 Vite 기능 플래그를 해제해 기본값으로 시험·빌드한다. 검증이 모두
통과한 뒤 요청한 `0` 또는 `1` 값으로 배포용 번들을 다시 빌드한다. 값이 없으면
기존 기본값을 유지하며, 다른 값이면 설치 전에 중단한다.

다음 사전 검사는 하나라도 실패하면 중단한다. `DEPLOY_ROOT`는 대상 호스트에서
실제로 준비한 배포 루트로 지정한다. 후보 생성 때 사용한 config의 실제 경로를
`CONFIG_PATH`, 완료된 실행의 manifest 경로를 `RUN_MANIFEST`로 지정한다. 같은
보호 디렉터리 안에서 아직 존재하지 않는 출력 경로를 `CANDIDATE_CONFIG`로
지정한다. 아래 201은 예시다. 기존 운영 LXC의 번호를 대신 넣지 않는다.

```bash
set -e
: "${DEPLOY_ROOT:?set the existing deployment checkout root}"
: "${CONFIG_PATH:?set the verified bootstrap config path}"
: "${RUN_MANIFEST:?set the matching completed manifest path}"
: "${CANDIDATE_CONFIG:?set a new protected candidate config path}"
APP_CTID=201
: "${APP_HOSTNAME:?set the verified candidate hostname}"
INFRA_DIR="$DEPLOY_ROOT/infra"
CONSOLE_DIR="$DEPLOY_ROOT/console"
test -f "$CONFIG_PATH"
test -f "$RUN_MANIFEST"
test ! -e "$CANDIDATE_CONFIG"
test "$(dirname "$CANDIDATE_CONFIG")" = "$(dirname "$CONFIG_PATH")"
test "$(stat -c %a "$(dirname "$CANDIDATE_CONFIG")")" = 700
test -f "$INFRA_DIR/scripts/deploy-console.sh"
test -f "$INFRA_DIR/scripts/lib/isolated_core.py"
test "$(git -C "$CONSOLE_DIR" rev-parse --is-inside-work-tree)" = true
test -f "$CONSOLE_DIR/package-lock.json"
test -f "$CONSOLE_DIR/scripts/verify.sh"
command -v node
command -v npm
test "$(node -p 'Number(process.versions.node.split(".")[0])')" -ge 24
git -C "$CONSOLE_DIR" rev-parse HEAD
```

기존 부트스트랩 코드의 parser로 config와 manifest의 plan·완료 상태·생성된 app
항목을 검증하고, 실제 `pct config`의 hostname과 description을 맞춘다. 아래는
조회만 수행한다. 하나라도 맞지 않으면 후보 LXC를 변경하지 않는다.

```bash
python3 - "$INFRA_DIR" "$CONFIG_PATH" "$RUN_MANIFEST" "$APP_CTID" "$APP_HOSTNAME" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts/lib'))
from isolated_core import Config, pct_container_identity, plan

config = Config.load(Path(sys.argv[2]))
manifest_path = Path(sys.argv[3])
manifest = json.loads(manifest_path.read_text())
ctid, hostname = int(sys.argv[4]), sys.argv[5]
created = [row for row in manifest.get('created', []) if row.get('role') == 'application']
actual = pct_container_identity(subprocess.check_output(['pct', 'config', str(ctid)], text=True))
if (Path(config.state_dir) != manifest_path.parent or config.app_ctid != ctid
        or config.app_hostname != hostname or manifest.get('completed') is not True
        or manifest.get('plan') != plan(config) or len(created) != 1
        or created[0].get('id') != ctid or created[0].get('hostname') != hostname
        or actual != (hostname, 'isolated-core:' + str(manifest.get('run_id')))):
    raise SystemExit('candidate config, manifest and CT identity disagree')
print('candidate config, manifest and CT identity agree')
PY
```

`deploy-console.sh`는 기본적으로 `pickle-app` hostname만 대상으로 한다. 후보 CT의
실제 hostname이 다른 경우 config, manifest, `pct config`의 값이 일치하는지 확인한
뒤 `EXPECTED_CT_HOSTNAME`에 정확한 값을 지정한다. 값이 비었거나 형식이 잘못됐거나
CT의 hostname과 다르면 배포를 시작하지 않는다.

첫 콘솔 배포 전에 선택한 console HEAD의 생성 API 타입과 호출 경로를 라이브 API
명세에 대조하고, 로그인·인증·재인증 흐름이 해당 API와 맞는지 확인한다.
호환되지 않으면 API와 console의 배포 버전을 맞추거나 호환되는 console
커밋을 선택한다. 첫 배포는 두 정책 UI 플래그를 `0`으로 빌드해 정적 파일을
확인하고, 검증 계정이 준비되면 로그인을 확인한다. 플래그 `0`도 API·console
전체 호환성 검사를 대신하지 않는다.

nginx 설정을 바꾸기 전에 **원본 노드의 후보 CT 201을 새로 백업하고 다른 노드에서
격리 복원**해 실제 파일을 읽을 수 있음을 확인한다. 원본 아카이브를 복원 노드로
옮기지 않은 복원이나 같은 노드에서의 복원은 이 선행 조건을 충족하지 않는다.
양쪽 스토리지의 여유 공간과 클러스터 전체에서 미사용인 복원 CTID를 확인한다.
후보 rootfs가 snapshot 가능한 local-lvm에 있을 때는 아래처럼 `snapshot` 모드를
선택한다. 지원되지 않으면 자동으로 다른 모드로 바꾸지 말고 중단한다. 백업 뒤
원본 CT가 계속 `running`인지 확인한다.

```bash
# 원본 노드 root shell에서만 실행. RUN_TAG와 mapped root UID는 실제 CT 설정을 확인해 정한다.
set -e
APP_CTID=201
: "${RUN_TAG:?set a unique reviewed run tag}"
: "${MAPPED_ROOT_UID:?verify the unprivileged CT root UID mapping}"
DUMP_DIR="/root/pickle-ct-backup-$RUN_TAG"
TMP_DIR="/var/lib/vz/dump/pickle-ct-tmp-$RUN_TAG"
test ! -e "$DUMP_DIR"
test ! -e "$TMP_DIR"
test "$(pct status "$APP_CTID")" = 'status: running'
SOURCE_CONFIG="$(pct config "$APP_CTID")"
SOURCE_NET_KEYS="$(printf '%s\n' "$SOURCE_CONFIG" | sed -nE 's/^(net[0-9]+):.*/\1/p')"
test "$SOURCE_NET_KEYS" = net0 || { echo 'source has unexpected network adapters' >&2; exit 1; }
install -d -m 0700 "$DUMP_DIR" "$TMP_DIR"
setfacl -m "u:${MAPPED_ROOT_UID}:--x" "$TMP_DIR"
getfacl "$TMP_DIR"
cd /
umask 022
vzdump "$APP_CTID" --mode snapshot --compress zstd \
  --dumpdir "$DUMP_DIR" --tmpdir "$TMP_DIR" --remove 0 --lockwait 0
test "$(pct status "$APP_CTID")" = 'status: running'
# ARCHIVE를 위 실행에서 생성된 단 하나의 실제 tar.zst 파일로 설정한 뒤:
: "${ARCHIVE:?set the fresh archive path}"
test -f "$ARCHIVE"
test -s "$ARCHIVE"
test "$(stat -c %a "$ARCHIVE")" = 600
zstd -t "$ARCHIVE"
tar --zstd -tf "$ARCHIVE" >/dev/null
ARCHIVE_CONFIG="$(tar --zstd -xOf "$ARCHIVE" ./etc/vzdump/pct.conf)"
ARCHIVE_NET_KEYS="$(printf '%s\n' "$ARCHIVE_CONFIG" | sed -nE 's/^(net[0-9]+):.*/\1/p')"
test "$ARCHIVE_NET_KEYS" = net0 || { echo 'archive has unexpected network adapters' >&2; exit 1; }
stat -c '%s %n' "$ARCHIVE"
sha256sum "$ARCHIVE"
```

unprivileged CT의 `vzdump`는 mapped root(일반적인 PVE 기본 매핑에서는 UID
100000)가 임시 작업 디렉터리를 통과해야 한다. `/root` 아래 0700 디렉터리를
`tmpdir`로 쓰면 archive 생성 전에 tar가 접근 거부로 실패한다. 위 ACL은 별도의
임시 디렉터리에 mapped root의 **통과 권한만** 주며, 아카이브와 로그는 `/root`의
보호 디렉터리에 둔다. `umask 022`는 PVE helper에만 적용한다. 실패 시 원본 CT
상태, LVM 임시 snapshot, PVE task와 로그를 먼저 확인하고 반복 실행하지 않는다.
작업이 끝나고 임시 디렉터리가 비었으며 관련 task가 없음을 확인한 뒤
`setfacl -b "$TMP_DIR"`, `rmdir "$TMP_DIR"` 순서로 정리한다. 아카이브와 로그는
보호 경로에 보존한다.

방금 출력한 SHA-256과 파일 크기를 기록한다. 승인된 관리 전송 경로로 아카이브를
**복원 노드의 0700 디렉터리와 0600 임시 파일**에 복사하고, 전체 전송과 해시
확인이 끝난 뒤 최종 이름으로 바꾼다. 전송 명령과 경로는 양 노드의 실제
SSH 및 스토리지 설정을 확인해 정한다. 복원 노드에서 다음 검사가 통과하지 않으면
복원하지 않는다. `EXPECTED_SHA256`은 원본 노드에서 얻은 64자리 값이며,
복원 노드의 `ARCHIVE`는 전송 완료된 파일의 절대 경로다.

```bash
# 복원 노드에서만 실행. 파일과 기대 해시를 실제 값으로 설정한 뒤 검사한다.
set -e
: "${ARCHIVE:?set the transferred archive path}"
: "${EXPECTED_SHA256:?set the source-node SHA-256}"
test -s "$ARCHIVE"
printf '%s  %s\n' "$EXPECTED_SHA256" "$ARCHIVE" | sha256sum -c -
zstd -t "$ARCHIVE"
tar --zstd -tf "$ARCHIVE" >/dev/null
```

복원 노드에서 `pct help restore`와 `pct help mount`로 설치된 PVE 버전의 옵션과
오프라인 mount 절차를 확인한다. `RESTORE_CTID`가 클러스터 전체에서 미사용이고
`RESTORE_STORAGE`에 충분한 공간이 있음을 확인한 다음, **기동 금지·onboot 0**
옵션이 실제 도움말에서 확인된 경우에만 새 CTID로 복원한다. 아래 명령은 그
옵션을 확인한 뒤에만 실행한다. 원본 CTID를 덮어쓰는 `--force`는 사용하지 않는다.

```bash
# 복원 노드에서만 실행. 위의 해시 검사가 통과한 아카이브만 사용한다.
set -e
: "${RESTORE_CTID:?set a cluster-wide unused CTID}"
: "${RESTORE_STORAGE:?set verified restore storage}"
: "${RESTORE_HOSTNAME:?set a distinct test hostname}"
: "${RESTORE_MARKER:?set a unique ownership marker}"
pct restore "$RESTORE_CTID" "$ARCHIVE" --storage "$RESTORE_STORAGE" \
  --start 0 --onboot 0 --net0 'name=eth0,ip=manual,link_down=1' \
  --hostname "$RESTORE_HOSTNAME" --description "$RESTORE_MARKER"
test "$(pct status "$RESTORE_CTID")" = 'status: stopped'
RESTORED_CONFIG="$(pct config "$RESTORE_CTID")"
RESTORED_NET_KEYS="$(printf '%s\n' "$RESTORED_CONFIG" | sed -nE 's/^(net[0-9]+):.*/\1/p')"
test "$RESTORED_NET_KEYS" = net0 || { echo 'restore has unexpected network adapters' >&2; exit 1; }
RESTORED_NET0="$(printf '%s\n' "$RESTORED_CONFIG" | sed -n 's/^net0: //p')"
case ",$RESTORED_NET0," in *,ip=manual,*) ;; *) echo 'restore IP is not manual' >&2; exit 1 ;; esac
case ",$RESTORED_NET0," in *,link_down=1,*) ;; *) echo 'restore link is not down' >&2; exit 1 ;; esac
case "$RESTORED_NET0" in *bridge=*|*gw=*|*ip6=*) echo 'restore has a routable NIC' >&2; exit 1 ;; esac
test "$(printf '%s\n' "$RESTORED_CONFIG" | sed -n 's/^onboot: //p')" = 0
printf '%s\n' "$RESTORED_CONFIG"
```

복원본이 `stopped`, `onboot: 0`이며 네트워크 어댑터는 수동 IP와
`link_down=1`인 `net0` **하나만** 있고 bridge·gateway·IPv6 주소가 없어야 한다.
원본 또는 archive에 `net1` 이상이 있으면 복원 전 중단한다. 복원 후 다른 NIC가
나타나도 중단하고 원인을 조사하며 임의로 제거하지 않는다.
`description`과 hostname은 원본과 다른 시험 소유권
표시여야 한다. 복원 노드에서 **시작·exec·네트워크 연결을 하지 않고** 다음과 같이
rootfs를 오프라인으로 검사한다. `pct mount`가 반환한 실제 경로를 `MOUNT_ROOT`와
대조하고, nginx 설정 및 주요 파일의 SHA-256을 원본과 비교한다.

```bash
set -e
MOUNT_ROOT="/var/lib/lxc/$RESTORE_CTID/rootfs"
pct mount "$RESTORE_CTID"
trap 'pct unmount "$RESTORE_CTID"' EXIT
test -f "$MOUNT_ROOT/etc/nginx/conf.d/isolated-core.conf"
sha256sum "$MOUNT_ROOT/etc/nginx/conf.d/isolated-core.conf"
pct unmount "$RESTORE_CTID"
trap - EXIT
if findmnt -rn -M "$MOUNT_ROOT"; then
  echo 'restore rootfs remains mounted' >&2
  exit 1
fi
pct status "$RESTORE_CTID"
pct config "$RESTORE_CTID"
```

unmount 뒤 `pct config`에 lock이 없어야 한다. mount가 실패하거나 잠금이 남으면 중단한다.
복원본은 계속 꺼 둔다. 복원 실패·내용 불일치·동일 IP 노출 가능성이 있으면 nginx
변경을 중단한다. 이 검증은 백업의 교차 노드 복원 가능성을 확인하는 것이며,
DB와 서비스의 전체 복구 검증을 대신하지 않는다. 명령 근거는
[Proxmox VE 9 pct 참조](https://pve.proxmox.com/pve-docs-9-beta/pct.1.html)이며,
실제 적용 전에는 설치 버전의 도움말을 우선한다.

백업과 격리 복원까지 끝난 기존 후보 LXC에만 새 nginx 설정을 반영한다.
부트스트랩 전체를 다시 실행하지 않는다. `CONFIG_PATH`는 위에서 신원을
검증한 이 후보의 생성 입력이고, `CANDIDATE_CONFIG`는 아직 없는 보호 파일이다.

```bash
set -e
cd "$INFRA_DIR"
: "${APP_CTID:?set the verified candidate CTID}"
: "${APP_HOSTNAME:?set the verified candidate hostname from config and pct config}"
: "${CONFIG_PATH:?set the verified bootstrap config path}"
: "${CANDIDATE_CONFIG:?set a new protected candidate config path}"
test ! -e "$CANDIDATE_CONFIG"
umask 077
python3 -c 'import sys; from pathlib import Path; sys.path.insert(0, "scripts/lib"); from isolated_core import Config, nginx; print(nginx(Config.load(Path(sys.argv[1]))), end="")' "$CONFIG_PATH" > "$CANDIDATE_CONFIG"
pct exec "$APP_CTID" -- test ! -e /etc/nginx/conf.d/isolated-core.conf.before-loopback
pct exec "$APP_CTID" -- cp -p /etc/nginx/conf.d/isolated-core.conf /etc/nginx/conf.d/isolated-core.conf.before-loopback
pct push "$APP_CTID" "$CANDIDATE_CONFIG" /tmp/isolated-core.conf.candidate
pct exec "$APP_CTID" -- install -m 0644 /tmp/isolated-core.conf.candidate /etc/nginx/conf.d/isolated-core.conf
pct exec "$APP_CTID" -- nginx -t
pct exec "$APP_CTID" -- systemctl reload nginx
CTID="$APP_CTID" EXPECTED_CT_HOSTNAME="$APP_HOSTNAME" \
  PICKLE_ROOT="$DEPLOY_ROOT" CONSOLE_DIR="$CONSOLE_DIR" \
  VITE_VM_NETWORK_POLICY_ENABLED=0 VITE_PUBLIC_SOURCE_POLICY_ENABLED=0 \
  bash scripts/deploy-console.sh
```

`nginx -t` 또는 reload가 실패하면 console 배포를 시작하지 않는다. localhost의
index와 번들 확인은 실제 정적 파일을 올린 뒤 배포 스크립트가 수행한다.
아래 명령은 이 후보의 nginx 설정만 원래대로 돌린다. 기존 설정에서는 localhost
postcheck가 다시 실패하므로 원인 조사 뒤 재적용한다.

```bash
APP_CTID=201
pct exec "$APP_CTID" -- cp -p /etc/nginx/conf.d/isolated-core.conf.before-loopback /etc/nginx/conf.d/isolated-core.conf
pct exec "$APP_CTID" -- nginx -t
pct exec "$APP_CTID" -- systemctl reload nginx
```

첫 console 배포에는 이전 번들이 없어 postcheck 실패 시 자동으로 되돌릴 대상이
없다. 스크립트는 실패를 보고하고 새 web root를 그대로 남긴다. 실패한 후보의
`/var/www/pickle-console`과 배포 로그를 조사한 뒤 빌드 원인을 고치고 다시 배포한다.
기존 서비스의 console이나 다른 LXC를 rollback 대상으로 사용하지 않는다.

### 정책 UI 활성화 전 검사와 재배포

첫 콘솔 배포가 끝나도 정책 UI는 꺼 둔다. 기능 노출 전에 후보 CT의 **실행 중인
API** OpenAPI에서 아래 7개 정책 경로와 method를 확인한다. 라이브 `paths`
키에는 `/api/v1` prefix가 포함된다. 누락이 있으면 플래그 `1` 배포를 중단하고
API 배포본을 맞춘 뒤 다시 검사한다. 이 검사는 기능 동작을 증명하지 않는다.

```bash
set -e
: "${APP_CTID:?set the verified candidate CTID}"
OPENAPI_JSON="$(mktemp)"
trap 'rm -f "$OPENAPI_JSON"' EXIT
pct exec "$APP_CTID" -- curl -fsS http://127.0.0.1:8080/api/v1/openapi > "$OPENAPI_JSON"
python3 - "$OPENAPI_JSON" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding='utf-8'))
paths = document['paths']
required = {
    '/api/v1/vms/{vmId}/network-policy': {'get', 'put'},
    '/api/v1/admin/vms/{vmId}/network-policy': {'get', 'put'},
    '/api/v1/domains/{domainId}/source-policy': {'get', 'put'},
    '/api/v1/vms/{vmId}/port-forwardings/{portForwardingId}/source-policy': {'get', 'put'},
    '/api/v1/admin/port-mappings/{mappingId}/source-policy': {'get', 'put'},
    '/api/v1/admin/routes/{routeId}/source-policy': {'get', 'put'},
    '/api/v1/source-policy-presets/campus': {'get'},
}
missing = [(path, method) for path, methods in required.items()
           for method in methods if method not in paths.get(path, {})]
if missing:
    raise SystemExit(f'live API lacks policy operations: {sorted(missing)}')
print(f"live policy operations present; API version {document.get('info', {}).get('version', 'unknown')}")
PY
rm -f "$OPENAPI_JSON"
trap - EXIT
```

선택한 console 커밋의 생성 API 타입·인증 흐름이 실제 API와 맞는지 다시 확인한다.
후보 node label과 agent 대상을 확인하고, PVE VM NIC 방화벽의 규칙 쓰기·조회,
기본 차단·명시 허용·위조·IPv6 우회 방지, 실패 후 재처리를 시험 VM으로 실측한다.
공개 출발지 정책은 proxy·relay의 원본 주소와 HTTP·TCP·UDP 허용·거부를
검증한다. API runtime 설정은 파일 값만 읽지 말고 실제 정책 API의 저장·반영
상태와 PVE/진입점 결과로 확인한다. 조건이 하나라도 미충족이면 두 UI 플래그를
`0`으로 유지한다. 모두 통과한 뒤에만 같은 console 커밋을 플래그 `1`로
다시 빌드·배포하고 해당 화면과 접근 권한을 확인한다.

```bash
set -e
: "${APP_CTID:?set the verified candidate CTID}"
: "${APP_HOSTNAME:?set the verified candidate hostname}"
: "${DEPLOY_ROOT:?set the deployment checkout root}"
: "${CONSOLE_DIR:?set the selected console checkout}"
: "${INFRA_DIR:?set the selected infra checkout}"
CTID="$APP_CTID" EXPECTED_CT_HOSTNAME="$APP_HOSTNAME" \
  PICKLE_ROOT="$DEPLOY_ROOT" CONSOLE_DIR="$CONSOLE_DIR" \
  VITE_VM_NETWORK_POLICY_ENABLED=1 VITE_PUBLIC_SOURCE_POLICY_ENABLED=1 \
  bash "$INFRA_DIR/scripts/deploy-console.sh"
```

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
운영자가 `systemctl stop pickle-api.service`로 정상 종료하면 JVM의 SIGTERM 종료 코드
143은 `SuccessExitStatus`로 정상 처리되며, 수동 중지 뒤 inactive가 되는 것은 오류가 아니다.

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

최종 갱신: 2026-09-27
