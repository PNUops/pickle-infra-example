# DB 복원: pickle_dev(LXC 101의 PostgreSQL)

`db-backup.sh`가 만든 덤프(`pickle_dev-YYYYMMDD-HHMMSS.sql.gz`, plain 형식 pg_dump)를 복원해요.
백업은 양쪽에 있어요. 호스트 `/srv/pickle/backup/db/`와 LXC 101
`/var/backups/pickle/`이고 보존 기간은 14일이에요.

이 문서는 위에 적힌 Proxmox 호스트의 개발 DB에 적용하는 plain SQL dump 복구와 빈 DB 부트스트랩 절차예요.
별도 DB LXC의 암호화 PBS dump 복구는 [DB-PBS 백업과 복구 절차](db-pbs-backup.md)를 따라요.
이 문서의 drop/create나 개발용 seed 정리를 운영 DB나 기존 동의 기록에 그대로 적용하지 마세요.

## 언제 쓰는가

- 이후 수정으로 해결할 수 없는 잘못된 배포나 마이그레이션(V20 이후 세대의 jar와 DB는 짝을 맞춰야 해요.
  jar가 자기보다 오래된 스키마의 DB 위에서 돌면 안 돼요).
- 데이터 손상, 또는 실수로 실행한 파괴적 조작.
- **빈 상태로 되돌리는 용도로는 쓰지 않아요.** 그때는 drop/create 후 Flyway가 V1부터 최신까지 다시 적용하게 해요.
  스키마만 생기고 이 호스트의 인벤토리는 생기지 않아요. 이후 필요한 행은
  [빈 데이터베이스 부트스트랩](#빈-데이터베이스-부트스트랩)에서 확인해요.

## 절차(약 2~5분, api 전체 중단)

아래 `qm list`는 실행 호스트의 로컬 VM 목록을 읽어요. 여러 노드가 VMID를 공유한다면
이 예시만으로 번호를 확정하지 마세요. 전체 중복 여부를 별도로 확인한 뒤 진행해요.

```bash
# 0) 덤프 선택 (호스트 기준, LXC 사본도 동일)
ls -lt /srv/pickle/backup/db/ | head
DUMP=/srv/pickle/backup/db/pickle_dev-YYYYMMDD-HHMMSS.sql.gz

# 1) 변경 전 덤프 헤더 확인 (gzip -t 검사와 구분)
zcat "$DUMP" | head -5 | grep -q "PostgreSQL database dump" && echo dump-ok

# 2) api 정지 (같은 프로세스의 JobRunr 워커와 폴러도 정지)
pct exec 101 -- systemctl stop pickle-api

# 3) 현재 상태의 안전 덤프 보관 (손상 여부와 관계없이 항상 수행)
bash /srv/pickle/infra/scripts/db-backup.sh   # 또는 파일 이름에 "-prerestore" 추가

# 4) drop 후 create (소유자는 `pickle`)
pct exec 101 -- runuser -u postgres -- psql -qc \
  "select pg_terminate_backend(pid) from pg_stat_activity where datname='pickle_dev' and pid<>pg_backend_pid()"
pct exec 101 -- runuser -u postgres -- dropdb pickle_dev
pct exec 101 -- runuser -u postgres -- createdb -O pickle pickle_dev

# 5) 복원 (덤프를 LXC에 넣고 postgres로 psql 실행)
pct push 101 "$DUMP" /tmp/restore.sql.gz
pct exec 101 -- bash -c "set -eo pipefail; zcat /tmp/restore.sql.gz | runuser -u postgres -- psql -q -d pickle_dev"
pct exec 101 -- rm -f /tmp/restore.sql.gz

# 6) api 기동 전 vmid_seq 조정. 덤프는 시퀀스를 백업 시점 값으로 되돌려요.
#    백업 이후 프로비저닝된 게스트는 더 높은 vmid로 클러스터에 남아 있을 수 있어요.
#    이 단계를 건너뛰면 다음 프로비저닝이 이미 점유된 번호를 뽑아 충돌로 주차돼요.
#    파이프라인은 상주 게스트를 건드리지 않아요. 아래 값은 이 호스트 목록에서 확인한
#    사용자 대역 vmid 최댓값보다 뒤로 설정해요. 다중 노드 전체 검증을 대신하지 않아요.
MAXVMID=$(qm list | awk '$1 ~ /^[0-9]+$/ && $1+0 >= 100000 {print $1}' | sort -n | tail -1)
# greatest()는 복원된 시퀀스 값도 유지해요. 덤프가 남긴 지점보다 시퀀스를 되돌리지 않아요.
# 재사용 금지 정책을 유지해요.
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select setval('vmid_seq', greatest((select last_value from vmid_seq), 100000, ${MAXVMID:-100000} + 1))"

# 7) api 기동. Flyway가 검증하고 덤프보다 새로운 마이그레이션을 적용해요.
#    시더는 없을 때만 넣어요.
pct exec 101 -- systemctl start pickle-api
for i in $(seq 1 30); do sleep 2; pct exec 101 -- curl -fsS http://127.0.0.1:8080/actuator/health/readiness >/dev/null 2>&1 && { echo readiness-ok; break; }; done
```

## 복원 후 확인

```bash
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select version, success from flyway_schema_history order by installed_rank desc limit 3"
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select key, value from settings where key='ssh_gateway_enabled'"
# ^ 복원한 스위치 값을 현재 승인된 환경과 운영자 의도에 대조해요.
#   검증 뒤 의도한 값만 관리 설정 화면이나 API로 다시 적용하고 의도적인 OFF를 켜지 않아요.
#   초기화 때는 apply-settings.sh가 없는 행을 만들면서 값을 꺼진 상태로 써요.
# 드리프트: 복원된 `vms` 행이 라이브 Proxmox 게스트와 다를 수 있어요. 10분 주기
#   리코실러는 표시만 하고 파괴하지 않아요. 드리프트 리포트로 해소해요.
```

## 함정

- **jar와 DB 세대 짝**: 덤프에 `flyway_schema_history`가 있으므로 Flyway는 빠진 마이그레이션만 적용해요.
  V20 이전 덤프에는 당시 enum 값이 있고, 보존 기간이 14일이어서 정기 백업에는 남아 있지 않아요.
  수동으로 가져온 옛 덤프에는 그 시대의 jar를 짝지어 주세요. jar가 자기보다 오래된 스키마의 DB 위에서 돌면 안 돼요.
- `audit_logs`의 append-only REVOKE는 덤프에 포함돼요(plain pg_dump는 ACL을 소유자 GRANT/REVOKE 문으로 담고 postgres로 실행해요).
  복원 후 role `pickle`로 `UPDATE audit_logs`를 시도해 거부되는지 확인해요.
- JobRunr 테이블도 덤프 안에 있어요. Proxmox에 더 이상 없는 VM을 참조하는 오래된 큐 작업은
  NEEDS_ADMIN으로 주차돼요(파괴하지 않아요).

## 빈 데이터베이스 부트스트랩

복원은 호스트의 인벤토리도 덤프와 함께 되돌려요. **Flyway 재적용은 스키마만 만들고 데이터를 넣지 않아요.**
노드와 IP 풀, 릴레이, 플랫폼 인증서, 런타임 설정, OS 카탈로그, 법적 문서는 아래 다섯 스크립트가 기록해요.

카탈로그 writer는 전역 `(name, version)` 유니크 제약만 있는 레거시 스키마용이에요.
노드별 `(node_id, name, version)` 제약이 있거나 두 제약이 함께 있거나 둘 다 없으면
`apply-os-catalog.sh`는 쓰기 전에 중단해요. 해당 환경에서는
[노드별 OS 이미지 등록](image-registration.md)의 스키마 조건을 먼저 확인해 주세요.

| 스크립트 | 쓰는 것 | 재실행 동작 |
|---|---|---|
| `apply-platform-inventory.sh` | 실측 용량을 가진 노드, IP 풀, 릴레이, 와일드카드 인증서 행 | 다시 측정해 바로잡아요 |
| `apply-settings.sh` | 런타임 설정 행 | 없는 키만 넣고 값을 덮어쓰지 않아요 |
| `apply-terms.sh` | 이용약관과 개인정보처리방침 | 새 버전을 게시해요. 효력이 시작됐거나 동의 기록이 있는 문안은 수정하지 않아요 |
| `apply-os-catalog.sh` | OS 카탈로그 행 | 레거시 전역 제약에서 upsert해요. 행의 status는 바꾸지 않아요 |
| `apply-relay-token.sh` | 릴레이 동기화 토큰을 양쪽에 동시에 | 재발급해요. 이전 토큰으로 새 동기화를 인증할 수 없어요 |

drop/create 후 Flyway 재적용만 마친 DB에는 VM 배치와 설정, 가입에 필요한 행이 없어요.
아래는 해당 개발 환경의 초기 구성 순서예요. 카탈로그 스키마가 위 조건과 다르면 그대로 실행하지 마세요.

### 순서

| # | 단계 | 순서를 어기면 |
|---|---|---|
| 1 | 호스트 브리지와 NAT, 방화벽이 올라와 있고 호스트가 게스트 네트워크 게이트웨이 주소로 응답해요 | `apply-platform-inventory.sh`가 거부해요. 게스트에게 알려 줄 게이트웨이는 이 호스트가 게스트 브리지에서 실제로 들고 있는 주소여야 해요. 우회하면 게스트가 라우팅할 수 없는 게이트웨이를 받아요 |
| 2 | 앱 컨테이너 구축(`create-app-lxc.sh`). PostgreSQL 과 데이터베이스, 그리고 Proxmox 노드 이름을 인프라 브리지 주소로 매핑하는 컨테이너의 hosts 항목 | 인벤토리 스크립트가 거부해요. api는 Proxmox API 인증서를 핀하고 호스트명 검증을 건너뛰지 않아요. `api_host`는 인증서의 SAN이면서 컨테이너 안에서 resolve되는 이름이어야 해요. 브리지 주소는 SAN이 아니에요 |
| 3 | api를 한 번 기동해 Flyway가 V1부터 최신까지 적용하게 해요 | 인벤토리 스크립트가 이름을 대며 거부해요. "database … has no nodes table" |
| 3b | **`dev` 프로필의 빈 DB 부트스트랩에서는 첫 기동 직후, 7번과 8번 전에 `settings`와 `terms_versions`를 비워요** | 3번의 같은 기동에서 개발용 시더가 두 테이블을 채워요. 예약어 7개와 빈 연락처 주소, 자리표시자 법률 문안이 들어가므로 7번과 8번이 큐레이션한 내용을 넣을 수 없어요. `apply-settings.sh`는 기존 키를 두고, `apply-terms.sh`는 게시된 문안과 파일이 달라 거부해요. 짧은 예약어 목록과 재사용하지 않는 slug, 실제 동의를 받는 법률 문안이므로 개발용 초기값을 그대로 게시하지 마세요. 시드 계정이 자리표시자 약관에 **이미 동의했으므로** `user_consents`부터 지워야 해요. 동의 행을 남긴 채 두 delete를 같은 트랜잭션으로 실행하면 외래 키 오류로 settings도 함께 되돌아가요. 시더의 **OS 카탈로그 행**도 다른 버전으로 중복되므로 `delete from user_consents; delete from terms_versions; delete from settings; delete from os_images;` 전체를 적용해요. 나머지 인벤토리 행은 비울 필요가 없어요. `apply-platform-inventory.sh`가 노드와 풀, 릴레이, 인증서를 제자리에서 갱신해요 |
| 4 | 리버스 프록시 컨테이너를 세우고, **플랫폼 루트 도메인마다** Let's Encrypt 와일드카드 계열을 DNS-01 로 `/etc/letsencrypt/live/<루트>/` 에 발급해요 | 인벤토리 스크립트가 거부해요. 설치된 인증서에서 `not_after`를 읽으므로 만료일을 추정하지 않아요. 발급을 건너뛰고 행만 넣지 마세요. DB에는 ACTIVE 인증서가 있어도 프록시는 그 루트의 모든 apply를 거부해요. 이후 호스트의 `pickle-wildcard-cert-row.timer`가 갱신된 만료일을 매일 행에 다시 써요 |
| 5 | 릴레이 호스트를 프로비저닝하고 공인 주소를 정해요 | `PICKLE_RELAY_PUBLIC_HOST`는 필수예요. `public_host`가 비면 사용자에게 접속 주소 없는 전달 포트를 주게 돼요. 이 컬럼을 쓰는 API는 없어요 |
| 6 | **`bash scripts/apply-platform-inventory.sh`**. 실측 용량을 가진 노드, IP 풀, 릴레이, 와일드카드 인증서 행 | |
| 7 | **`bash scripts/apply-settings.sh`**. 런타임 설정 행 | 스칼라 값과 달리 없는 LIST 키는 빈 목록으로 읽혀요. `allowed_root_domains` 행이 없으면 플랫폼 루트를 지정한 VM 요청은 "허용되지 않은 루트 도메인" 으로 거부되고 AUTO 서브도메인은 루트를 찾지 못해요. `reserved_subdomains`와 `profanity_subdomains` 행이 없으면 예약어와 금칙어 검사가 통과해요. 플랫폼 존 보호는 앞의 허용 루트 목록을 기준으로 판단해요. slug는 재활용하지 않으므로 이미 claim한 이름을 되돌릴 수 없어요. 설정 화면에는 존재하는 행만 나오며 없는 키를 편집하면 404예요. 6번과 같은 `PICKLE_ROOT_DOMAIN`을 넘겨야 인증서가 덮는 루트 도메인을 제시해요. 두 번째 플랫폼 루트는 나중에 관리 콘솔에서 추가해 주세요 |
| 8 | **`bash scripts/apply-terms.sh`**. 법적 문서 | 가입은 현재 버전에 대한 동의를 요구하므로 행이 없으면 가입할 수 없어요. 약관을 게시하지 않은 상태에서 계정을 모으도록 우회하지 마세요 |
| 9 | `apply-os-catalog.sh`. OS 카탈로그 행 | 레거시 전역 제약에서 노드를 이름으로 resolve해요. 노드 행이 없으면 쓰지 않고 알리며 새 행은 DISABLED로 들어와요 |
| 10 | **`bash scripts/apply-relay-token.sh`**. 토큰을 발급하고 릴레이에 설치해요 | 그때까지 6번은 `token_issued f`로 보고하고 릴레이 동기화를 거부해요. 발급과 설치 사이에는 이전 토큰을 쓰는 새 동기화 인증이 거부돼요. 이미 인증된 요청이나 설치된 전달 규칙이 토큰 교체만으로 취소되지는 않아요. 릴레이에 env 파일이 없으면 거부해요. 에이전트의 다른 변수에는 코드 기본값이 없어 스크립트가 대신 정하지 않아요 |
| 11 | 관리 콘솔에서 OS를 활성화하고 **기능 스위치를 현재 승인된 운영 의도와 대조해요** | 카탈로그가 비면 요청 폼에서 고를 수 없어요. 초기화 때 `apply-settings.sh`는 `ssh_gateway_enabled`와 `web_terminal_enabled`, `port_forwarding_enabled`를 **꺼진 상태로** 넣어요. 실제 동작 검증 전에는 켜지 않아요. 초기화 뒤에는 SSH와 웹 터미널 등의 새 접속이 차단돼요. 복원한 값은 현재 승인된 환경과 운영자 의도에 대조하고 검증 뒤 의도한 값만 다시 적용해요. 의도적으로 끈 기능을 일괄 다시 켜지 마세요 |
| 12 | 스모크 테스트 | 실제 게스트를 프로비저닝하므로 위의 모든 행이 필요해요 |

DNS 존은 DB 백업에 포함되지 않아요. 복원한 serving 이름의 개별 A와 수동 ingress 이름은
[플랫폼 DNS 런북](platform-dns.md)대로 별도 대조해요. EXTERNAL 레코드나 승인 대기 신청에 ingress A를 임의로 넣지 마세요.
와일드카드 인증서 행 복구와 DNS wildcard A 생성은 별개예요. 전체 resync 성공만으로 DNS 복구를 판단하지 마세요.

이 개발 환경에서 호스트 용량(RAM이나 CPU)이 바뀌거나 와일드카드 인증서를 재발급하면 6번을 다시 실행해요.
실측해 기존 행을 바로잡고 두 번째 행을 만들지 않아요. MAINTENANCE로 주차한 노드와 릴레이의 토큰,
`enabled` 플래그, 세대 카운터는 그대로 둬요. CIDR 변경으로 살아 있는 할당이 고립되거나 포트 대역 축소로
살아 있는 매핑이 고립되면 거부해요.

등록한 메모리는 배치의 하드 필터이며 게스트에 할당한 양만 세요. 호스트와 인프라 컨테이너의 실제 사용량은 포함하지 않아요.
`PICKLE_NODE_MEMORY_RESERVE_MB`로 그만큼 예약해 주세요. 기본값은 0으로, 실측한 그대로 등록해요.

### 환경 변수: `apply-platform-inventory.sh`

처음 둘은 기본값이 없어 반드시 설정해야 해요. 나머지는 이 개발 환경의 값을 기본값으로 써요.
이 writer는 노드와 IP 풀, 릴레이, 인증서 행을 함께 쓰므로 기존 환경에 노드 하나를 추가하는 절차로 사용하지 마세요.
개별 노드 추가는 [노드 단독 등록](node-registration.md)과 [노드별 이미지 등록](image-registration.md)의 조건을 확인해 주세요.

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_RELAY_PUBLIC_HOST` | `ssh.example.dev` | **필수.** 스킴과 포트 없이 호스트만 적어요. 뻔한 자리표시자는 거부해요 |
| `PICKLE_ROOT_ORG` | `예시 기관` | **필수.** `PICKLE_ROOT_DOMAIN` 아래 발급되는 이름이 이 기관에 속하고 관리자 도메인 목록도 이 기관으로 좁혀져요. `orgs.name`과 정확히 한 행이 일치해야 해요. 기본값으로 기관을 임의로 고르지 않아요 |
| `PICKLE_APP_CTID` | `101` | PostgreSQL 과 api 가 도는 컨테이너 |
| `PICKLE_PROXY_CTID` | `100` | 와일드카드 인증서 자료를 가진 컨테이너 |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_NODE` | `pve-node` | Proxmox API로 대조해요. 이름이 틀리면 쓰기 전에 실패해요 |
| `PICKLE_NODE_API_HOST` | `https://pve-node:8006` | 기본값은 `https://<node>:8006`. API 인증서의 SAN이어야 해요 |
| `PICKLE_PVE_CERT` | `/etc/pve/local/pve-ssl.pem` | SAN 을 검사할 인증서 |
| `PICKLE_NODE_BRIDGE` | `vmbr2` | 호스트에 존재해야 해요 |
| `PICKLE_NODE_STORAGE` | `local-lvm` | 존재하고 active여야 해요 |
| `PICKLE_NODE_MEMORY_RESERVE_MB` | `0` | 실측 총량에서 떼어 둘 양 |
| `PICKLE_POOL_NAME` | `guest-private` | |
| `PICKLE_POOL_CIDR` | `198.19.0.0/16` | |
| `PICKLE_POOL_GATEWAY` | `198.19.0.1` | CIDR 안에 있고 호스트가 브리지에서 들고 있어야 해요 |
| `PICKLE_POOL_DNS` | `["8.8.8.8"]` | JSON 배열 |
| `PICKLE_POOL_RESERVED` | `[{"from": "198.19.0.0", "to": "198.19.0.255"}]` | 양끝을 포함하는 범위의 JSON 배열. 각각 CIDR 안에 있어야 해요 |
| `PICKLE_RELAY_NAME` | `lightsail-1` | |
| `PICKLE_RELAY_SOURCE_IP` | `100.64.0.1` | 릴레이의 터널 쪽 주소. 동기화 호출을 받아 주는 유일한 피어예요 |
| `PICKLE_RELAY_PORT_BAND` | `10000-19999` | 1024-65535 안 |
| `PICKLE_ROOT_DOMAIN` | `pusan.dev` | 인증서 범위가 `*.<root>`가 돼요 |
| `PICKLE_WILDCARD_CERT` | `/etc/letsencrypt/live/pusan.dev/fullchain.pem` | 루트 도메인에서 기본값이 정해져요. `*.<root>`를 덮어야 해요. `refresh-wildcard-cert-row.sh`도 같은 변수와 기본값을 읽어요 |

```bash
PICKLE_RELAY_PUBLIC_HOST=ssh.example.dev \
  PICKLE_ROOT_ORG='예시 기관' \
  bash /srv/pickle/infra/scripts/apply-platform-inventory.sh
```

실행이 끝나면 실측한 노드와 사용량을 포함한 풀, 공인 호스트가 있는 릴레이, 모든 플랫폼 와일드카드 행과
통과/실패 목록을 출력해요. 변경 전 행은 쓰기 전에
`/srv/pickle/backup/platform-inventory-<timestamp>/inventory-before.sql`(데이터만 담은 insert)로
덤프돼요.

### 환경 변수: `apply-settings.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_CONTACT_EMAIL` | `ops@example.org` | **필수.** 콘솔 푸터와 점검 화면, 오류 화면, 두 법적 문서의 연락처예요. 설정하지 않으면 터미널에서 묻고 터미널이 없으면 거부해요. 무인 실행이 값을 대신 정하지 않아요. 비운 채 게시하려면 `none`이라고 답해요 |
| `PICKLE_ROOT_DOMAIN` | `pusan.dev` | `allowed_root_domains`의 유일한 항목이 돼요. 인벤토리 스크립트와 같은 변수예요. 플랫폼 루트가 둘 이상이면(4번에서 루트마다 와일드카드 쌍 설치) 이 단계 뒤에 나머지를 관리 콘솔에서 추가해요. 재실행은 기존 행을 다시 쓰지 않아요 |
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_DATA_DIR` | `<repo>/data` | 큐레이션된 단어 목록이 있는 곳 |

```bash
PICKLE_CONTACT_EMAIL=ops@example.org \
  bash /srv/pickle/infra/scripts/apply-settings.sh
```

설정 둘은 운영자가 큐레이션하는 목록으로 `data/`에 한 줄에 하나씩 둬요.
`reserved-subdomains.txt`는 조직이 이미 게시해 claim할 수 없는 이름이고, `profanity-subdomains.txt`는 금칙어 목록이에요.
실행할 때마다 파일을 읽지만 행이 없을 때만 써요. 행이 생긴 뒤에는 관리 콘솔에서 목록을 늘리고
파일은 부트스트랩 사본으로 유지해요. 재구축 때 큐레이션을 잃지 않도록 두 사본을 맞춰 주세요.

### 환경 변수: `apply-terms.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_DATA_DIR` | `<repo>/data` | 문서는 `<data>/terms/*.md`에서 읽어요 |

```bash
bash /srv/pickle/infra/scripts/apply-terms.sh
```

문서 하나가 파일 하나예요. 네 줄짜리 헤더(`doc_type`, `version`, `title`, `effective_at`), `---` 줄과 Markdown 본문으로 구성해요.
`effective_at`은 실제 타임스탬프예요. 날짜가 지난 버전 중 가장 높은 것을 현재 버전으로 취급하므로 개정본을 미리 게시할 수 있어요.

효력이 시작됐거나 동의 기록이 있는 문서를 개정할 때는 **다음 버전 번호를 가진 파일을 추가해요**.
스크립트는 이런 버전의 본문이나 제목이 DB와 다르면 거부해요. 효력 시작 전이고 동의 기록이 없는 버전만
순서 조건과 쓰기 시점의 상태를 다시 확인해 수정할 수 있어요. 사용자가 이미 동의한 문안을 덮어써 동의 기록의 원문을 잃지 마세요.

### 환경 변수: `apply-os-catalog.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_NODE` | `pve-node` | 레거시 전역 제약에서 카탈로그 행의 노드를 이름으로 resolve해요. 노드 행이 없으면 쓰지 않고 알려요 |

템플릿은 스크립트 안의 목록이에요. 쓰기 전에 각각이 이 호스트에 이름과 VMID로 존재하는지 확인해요.
실제 템플릿이 없는 카탈로그 행을 제공하면 신청 후 clone이 실패해요. 위의 스키마 조건도 먼저 확인해 주세요.

### 환경 변수: `apply-relay-token.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_ADMIN_EMAIL` | | **설정하지 않으면 터미널에서 물어요.** 토큰을 관리 API로 발급하므로 SYS_ADMIN 자격이 필요해요 |
| `PICKLE_ADMIN_PASSWORD` | | 설정하지 않으면 터미널에서 묻고 화면에 표시하지 않아요 |
| `PICKLE_RELAY_NAME` | `lightsail-1` | 어느 릴레이 행에 발급할지 |
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_TUNNEL_CTID` | `102` | 릴레이 터널 주소에 닿는 컨테이너. 릴레이 admin 포트에 Proxmox 호스트가 닿지 않아 TCP 연결만 프록시해요. ssh는 호스트에서 실행하며 개인키는 컨테이너에 넣지 않아요 |
| `PICKLE_RELAY_SSH_KEY` | `$VAULT/lightsail-ssh.pem` | |
| `PICKLE_RELAY_SSH_USER` | `admin` | |
| `PICKLE_RELAY_SSH_PORT` | `22` | |

`deploy-relay.sh`는 같은 SSH 값 셋을 `RELAY_SSH_KEY`와 `RELAY_SSH_PORT`, `RELAY_HOST`라는 이름으로 읽어요.
새 환경에서는 두 벌을 모두 설정해 주세요.

최종 갱신: 2026-11-08
