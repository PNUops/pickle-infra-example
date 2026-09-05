# DB 복원: pickle_dev(LXC 101의 PostgreSQL)

`db-backup.sh`가 뜬 덤프(`pickle_dev-YYYYMMDD-HHMMSS.sql.gz`, plain 형식 pg_dump)를
복원한다. 백업은 양쪽에 있다. 호스트 `/srv/pickle/backup/db/`와 LXC 101
`/var/backups/pickle/`이고 보존 기간은 14일이다.

## 언제 쓰는가

- 앞으로 고칠 수 없는 잘못된 배포나 마이그레이션(V20 이후 세대의 jar와 DB는 짝을 맞춰야
  한다. jar가 자기보다 오래된 스키마의 DB 위에서 돌면 안 된다).
- 데이터 손상, 또는 실수로 실행한 파괴적 조작.
- **빈 상태로 되돌리는 용도로는 쓰지 않는다.** 그때는 덤프 복원이 아니라 drop/create 후
  Flyway가 V1부터 최신까지 다시 적용하게 한다. 다시 적용하면 스키마만 생기고 이 호스트에
  관한 것은 하나도 생기지 않는다. 그 뒤에 넣어야 할 행은
  [빈 데이터베이스 부트스트랩](#빈-데이터베이스-부트스트랩)에 있다.

## 절차(약 2~5분, api 전체 중단)

```bash
# 0) 덤프를 고른다 (호스트 쪽 기준. LXC 사본도 같다)
ls -lt /srv/pickle/backup/db/ | head
DUMP=/srv/pickle/backup/db/pickle_dev-YYYYMMDD-HHMMSS.sql.gz

# 1) 무엇을 건드리기 전에 덤프를 검증한다 (gzip -t 가 아니라 내용 검사)
zcat "$DUMP" | head -5 | grep -q "PostgreSQL database dump" && echo dump-ok

# 2) api 를 정지한다 (단일 프로세스 설계라 JobRunr 워커와 폴러도 함께 멈춘다)
pct exec 101 -- systemctl stop pickle-api

# 3) 지금의 (망가진) 상태를 먼저 안전 덤프로 뜬다. 예외 없이 항상 한다
bash /srv/pickle/infra/scripts/db-backup.sh   # 또는 파일 이름에 "-prerestore" 를 붙인다

# 4) drop 후 create (소유자는 `pickle`이어야 한다)
pct exec 101 -- runuser -u postgres -- psql -qc \
  "select pg_terminate_backend(pid) from pg_stat_activity where datname='pickle_dev' and pid<>pg_backend_pid()"
pct exec 101 -- runuser -u postgres -- dropdb pickle_dev
pct exec 101 -- runuser -u postgres -- createdb -O pickle pickle_dev

# 5) 복원 (덤프를 LXC 안으로 넣고 postgres 로 psql 에 먹인다)
pct push 101 "$DUMP" /tmp/restore.sql.gz
pct exec 101 -- bash -c "set -eo pipefail; zcat /tmp/restore.sql.gz | runuser -u postgres -- psql -q -d pickle_dev"
pct exec 101 -- rm -f /tmp/restore.sql.gz

# 6) api 가 기동하기 전에 vmid_seq 를 다시 맞춘다. 덤프는 시퀀스를 백업 시점 값으로
#    되돌리는데, 백업 이후 프로비저닝된 게스트는 그보다 높은 vmid 로 클러스터에 남아
#    있다. 이 단계를 건너뛰면 다음 프로비저닝이 이미 점유된 번호를 뽑아 vmid 충돌로
#    주차된다 (파이프라인은 상주 게스트를 건드리지 않는다). 클러스터에 살아 있는 사용자
#    대역 vmid 의 최댓값보다 뒤로 설정한다:
MAXVMID=$(qm list | awk '$1 ~ /^[0-9]+$/ && $1+0 >= 100000 {print $1}' | sort -n | tail -1)
# greatest() 가 복원된 시퀀스 값 자체도 유지하므로, 복원이 시퀀스를 덤프가 남긴 지점보다
# 뒤로 되돌리는 일은 생기지 않는다 (재사용 금지 정책)
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select setval('vmid_seq', greatest((select last_value from vmid_seq), 100000, ${MAXVMID:-100000} + 1))"

# 7) api 를 기동한다. Flyway 가 검증하고 덤프보다 새로운 마이그레이션을 적용한다.
#    시더는 멱등이다 (없을 때만 넣는다)
pct exec 101 -- systemctl start pickle-api
for i in $(seq 1 30); do sleep 2; pct exec 101 -- curl -fsS http://127.0.0.1:8080/actuator/health/readiness >/dev/null 2>&1 && { echo readiness-ok; break; }; done
```

## 복원 후 확인

```bash
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select version, success from flyway_schema_history order by installed_rank desc limit 3"
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select key, value from settings where key='ssh_gateway_enabled'"
# ^ 덤프가 운영자의 킬 스위치 전환보다 앞서면 관리 설정 화면이나 API 로 다시 켠다.
#   초기화는 같은 함정의 더 날카로운 형태다. 그쪽은 apply-settings.sh 가 행을 다시
#   만들면서 값을 꺼진 상태로 쓴다.
# 드리프트: 복원된 `vms` 행이 라이브 Proxmox 게스트와 어긋날 수 있다. 10분 주기
#   리코실러가 표시만 하고 파괴하지 않는다. 드리프트 리포트로 해소한다.
```

## 함정

- **jar 와 DB 세대 짝**: 덤프는 `flyway_schema_history`를 담고 있으므로 Flyway 는 빠진
  마이그레이션만 적용한다. 다만 V20 이전 덤프는 그 시대의 enum 값을 담고 있고, 보존
  기간이 14일이라 그런 덤프는 정기 백업에 남아 있지 않다. 즉 수동으로 가져온 덤프에서만
  생기는 문제다. 그런 덤프를 들여온다면 세대 짝 규칙이 적용된다. jar 는 자기보다 오래된
  스키마의 DB 위에서 돌면 안 되므로, 옛 덤프에는 그 시대의 jar 를 짝지어 준다.
- `audit_logs`의 append-only REVOKE 는 덤프에 포함된다(plain pg_dump 는 ACL 을 소유자
  GRANT/REVOKE 문으로 담고 postgres 로 실행된다). 복원 후 role `pickle`로
  `UPDATE audit_logs`를 시도해 거부되는지 확인한다.
- JobRunr 테이블도 덤프 안에 있다. Proxmox 에 더 이상 없는 VM 을 참조하는 오래된 큐 작업은
  NEEDS_ADMIN 으로 주차된다(파괴하지 않는다).

## 빈 데이터베이스 부트스트랩

복원은 호스트의 인벤토리를 덤프와 함께 되돌린다. 반면 **Flyway 재적용은 스키마만 만들고
데이터를 하나도 넣지 않는다.** 노드도, IP 풀도, 릴레이도, 플랫폼 인증서도, 런타임 설정도,
OS 카탈로그 행도, 법적 문서도 없다. 그 행들은 다섯 개의 스크립트가 쓴다.

| 스크립트 | 쓰는 것 | 재실행 동작 |
|---|---|---|
| `apply-platform-inventory.sh` | 실측 용량을 가진 노드, IP 풀, 릴레이, 와일드카드 인증서 행 | 다시 측정해서 바로잡는다 |
| `apply-settings.sh` | 런타임 설정 행 | 없는 키만 넣는다. 값을 덮어쓰지 않는다 |
| `apply-terms.sh` | 이용약관과 개인정보처리방침 | 새 버전을 게시한다. 게시된 것을 고쳐 쓰기는 거부한다 |
| `apply-os-catalog.sh` | OS 카탈로그 행 | upsert 한다. 행의 status 는 바꾸지 않는다 |
| `apply-relay-token.sh` | 릴레이 동기화 토큰을 양쪽에 동시에 | 재발급한다. 이전 토큰은 즉시 무효가 된다 |

그래서 drop/create 후 Flyway 재적용을 마친 시점(또는 새 환경을 처음 세운 시점)의
데이터베이스는 스키마는 완전하지만 VM 을 배치할 수 없고, 설정할 수 없으며, 아무도 가입할
수 없다. 아래 순서대로 진행한다.

### 순서

| # | 단계 | 순서를 어기면 |
|---|---|---|
| 1 | 호스트 브리지와 NAT, 방화벽이 올라와 있고 호스트가 게스트 네트워크 게이트웨이 주소로 응답한다 | `apply-platform-inventory.sh`가 거부한다. 게스트에게 알려 줄 게이트웨이는 이 호스트가 게스트 브리지에서 실제로 들고 있는 주소여야 한다. 우회하면 게스트가 라우팅 불가 게이트웨이를 받고, 증상은 템플릿 문제처럼 보인다 |
| 2 | 앱 컨테이너 구축(`create-app-lxc.sh`). PostgreSQL 과 데이터베이스, 그리고 Proxmox 노드 이름을 인프라 브리지 주소로 매핑하는 컨테이너의 hosts 항목 | 인벤토리 스크립트가 거부한다. api 는 Proxmox API 인증서를 핀하고 호스트명 검증을 건너뛸 수 없으므로, `api_host`는 그 인증서의 SAN 이면서 컨테이너 안에서 resolve 되는 이름이어야 한다. 브리지 주소는 SAN 이 아니다 |
| 3 | api 를 한 번 기동해 Flyway 가 V1 부터 최신까지 적용하게 한다 | 인벤토리 스크립트가 이름을 대며 거부한다. "database … has no nodes table" |
| 3b | **`dev` 프로필로 도는 환경에서는 그 첫 기동 직후, 7번과 8번 전에 `settings`와 `terms_versions`를 비운다** | 3번은 스키마만 만드는 것이 아니다. 같은 기동에서 api 의 개발용 시더가 돌면서 두 테이블을 채운다. 큐레이션된 목록 대신 예약어 7개, 빈 연락처 주소, 자리표시자 법률 문안이 들어가고, 시더는 **비어 있기 때문에** 채운다. 그러면 7번과 8번은 이미 행이 있는 것을 보고 설계대로 넘겨받지 않는다. `apply-settings.sh`는 그냥 둔 키가 몇 개인지 보고하고, `apply-terms.sh`는 게시된 문안이 파일과 다르므로 아예 거부한다. 피해는 표면적이지 않다. 짧은 예약어 목록은 수백 개 이름을 claim 가능한 상태로 남기고, slug 는 재활용되지 않으므로 누가 하나를 가져가면 영구히 가져간 것이다. 자리표시자 약관은 아무도 쓰지 않은 문서를 가리키는 실제 동의를 모은다. 실행할 때 걸리는 것 둘: 시드 계정이 자리표시자 약관에 **이미 동의해 두었으므로** `user_consents`부터 지우지 않으면 외래 키가 문장 전체를 거부한다(두 delete 를 한 트랜잭션에 넣으면 settings 도 함께 살아남아 실패가 약관만의 문제로 보인다). 그리고 시더는 **OS 카탈로그 행**도 쓰는데 카탈로그 스크립트가 쓰지 않는 버전이라 중복으로 남고 요청 폼이 같은 OS 를 두 번 제시한다. 그래서 비우는 문장은 `delete from user_consents; delete from terms_versions; delete from settings; delete from os_images;` 전체다. 나머지 인벤토리 행은 비울 필요가 없다. `apply-platform-inventory.sh`가 노드와 풀, 릴레이, 인증서를 제자리에서 갱신한다 |
| 4 | 리버스 프록시 컨테이너를 세우고, **플랫폼 루트 도메인마다** 와일드카드 Origin CA 쌍을 `/etc/nginx/pickle-certs/<루트, 점을 대시로>.{crt,key}`에 설치한다 | 인벤토리 스크립트가 거부한다. 설치된 인증서에서 `not_after`를 읽으므로 아무도 확인하지 않은 만료일을 단언하지 않는다. 쌍을 건너뛰고 행을 손으로 넣는 것은 더 나쁘다. 데이터베이스는 ACTIVE 인증서가 있다고 보고하는데 프록시는 그 루트에 대한 모든 apply 를 이름을 대며 거부한다 |
| 5 | 릴레이 호스트를 프로비저닝하고 공인 주소를 정한다 | `PICKLE_RELAY_PUBLIC_HOST`를 정직하게 채울 수 없는데 이 값은 필수다. `public_host`가 비면 사용자는 접속할 주소 없는 전달 포트를 받는다. 이 컬럼을 쓰는 API 는 없다 |
| 6 | **`bash scripts/apply-platform-inventory.sh`**. 실측 용량을 가진 노드, IP 풀, 릴레이, 와일드카드 인증서 행 | |
| 7 | **`bash scripts/apply-settings.sh`**. 런타임 설정 행 | 스칼라 값은 api 에 컴파일된 기본값으로 떨어지지만 LIST 키는 그렇지 않다. 없는 목록은 빈 목록으로 읽힌다. `allowed_root_domains` 행이 없으면 플랫폼 루트를 지정한 모든 VM 요청이 "허용되지 않은 루트 도메인" 으로 거부되고 AUTO 서브도메인은 루트를 하나도 찾지 못한다. `reserved_subdomains`와 `profanity_subdomains` 행이 없으면 그 검사들이 전부 통과되고, claim 된 이름은 영구히 claim 된다(slug 는 재활용되지 않는다). 커스텀 도메인이 플랫폼 존을 스쿼팅하는 것을 막는 검사도 아무것도 매치하지 않는다. 즉 게시 기능이 조용히 나빠지는 것이 아니라 아예 망가진다. 설정 화면은 존재하는 행만 보여 주고, 행이 없는 키를 편집하면 404 가 온다. 6번과 같은 `PICKLE_ROOT_DOMAIN`을 넘긴다. 아니면 요청 폼이 어떤 인증서도 덮지 않는 루트 도메인을 제시한다. 두 번째 플랫폼 루트는 이 스크립트로 넣을 수 없고 나중에 관리 콘솔에서 추가한다 |
| 8 | **`bash scripts/apply-terms.sh`**. 법적 문서 | 가입은 현재 버전에 대한 동의를 요구하므로 행이 없으면 아무도 가입할 수 없다. 이것은 우회할 버그가 아니라 의도된 실패다. 약관을 게시하지 않은 서비스는 계정을 모으면 안 된다 |
| 9 | `apply-os-catalog.sh`. OS 카탈로그 행 | upsert 가 노드를 이름으로 resolve 하므로 노드 행이 없으면 아무것도 쓰지 않고 그렇다고 알린다. 새 행은 DISABLED 로 들어온다 |
| 10 | **`bash scripts/apply-relay-token.sh`**. 토큰을 발급하고 릴레이에 설치한다 | 그때까지 6번은 `token_issued f`로 보고하고 모든 릴레이 동기화가 실패로 닫힌다. 이 스크립트가 두 절반을 한 번에 하는 이유는 그것이 한 행위이기 때문이다. 발급은 옛 토큰을 즉시 무효로 만들므로, 발급과 설치 사이의 간격은 릴레이가 잠긴 구간이다. 릴레이에 env 파일이 아직 없으면 거부한다. 에이전트의 다른 변수들에는 코드 기본값이 없고 스크립트가 그것을 지어내지 않기 때문이다 |
| 11 | 관리 콘솔에서 OS 를 활성화하고, **기능 스위치를 다시 켠다** | 카탈로그가 비어 있으면 요청 폼에서 고를 것이 없다. 함정은 스위치 쪽이다. `apply-settings.sh`는 `ssh_gateway_enabled`와 `web_terminal_enabled`, `port_forwarding_enabled`를 **꺼진 상태로** 쓴다. 새 배포는 그중 어느 것도 동작한다고 아직 증명하지 않았기 때문이다. 그래서 이 스위치들이 켜져 있던 시스템을 복원하면 SSH 와 웹 터미널이 죽은 채로 돌아오고 아무도 알려 주지 않는다. 운영자가 킬 스위치를 내린 것과 똑같이 동작한다 |
| 12 | 스모크 테스트 | 실제 게스트를 프로비저닝하므로 위의 모든 행이 필요하다 |

호스트의 용량(RAM 이나 CPU)이 바뀌거나 와일드카드 인증서를 재발급할 때마다 6번을 다시
실행한다. 다시 측정해서 행을 바로잡을 뿐 두 번째 행을 만들지 않는다. 운영자에게 속한 것은
건드리지 않는다. MAINTENANCE 로 주차한 노드는 그대로 주차돼 있고, 릴레이의 토큰과 `enabled`
플래그, 세대 카운터도 그대로 둔다. CIDR 변경이 살아 있는 할당을 고아로 만들거나 포트 대역을
좁히는 것이 살아 있는 매핑을 고립시키면 아예 거부한다.

등록된 메모리 수치는 배치의 하드 필터이고 게스트의 의도만 센다. 그래서 이 호스트 자신의
사용량도, 같은 RAM 을 나눠 쓰는 인프라 컨테이너도 계산에 넣지 않는다.
`PICKLE_NODE_MEMORY_RESERVE_MB`가 그만큼을 떼어 둔다. 기본값은 0, 즉 실측한 그대로
등록한다.

### 환경 변수: `apply-platform-inventory.sh`

첫 번째만 기본값이 없어 반드시 설정해야 한다. 나머지는 이 환경의 값을 기본값으로 쓴다.
전부 설정 값이므로 이것들을 덮어쓰는 것이 이 스크립트로 두 번째 호스트를 다루는 방법이다.

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_RELAY_PUBLIC_HOST` | `ssh.example.dev` | **필수.** 호스트만 적는다(스킴과 포트 없이). 뻔한 자리표시자는 거부한다 |
| `PICKLE_APP_CTID` | `101` | PostgreSQL 과 api 가 도는 컨테이너 |
| `PICKLE_PROXY_CTID` | `100` | 와일드카드 인증서 자료를 가진 컨테이너 |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_NODE` | `pve-node` | Proxmox API 로 대조한다. 이름이 틀리면 아무것도 쓰기 전에 실패한다 |
| `PICKLE_NODE_API_HOST` | `https://pve-node:8006` | 기본값은 `https://<node>:8006`. API 인증서의 SAN 이어야 한다 |
| `PICKLE_PVE_CERT` | `/etc/pve/local/pve-ssl.pem` | SAN 을 검사할 인증서 |
| `PICKLE_NODE_BRIDGE` | `vmbr2` | 호스트에 존재해야 한다 |
| `PICKLE_NODE_STORAGE` | `local-lvm` | 존재하고 active 여야 한다 |
| `PICKLE_NODE_MEMORY_RESERVE_MB` | `0` | 실측 총량에서 떼어 둘 양 |
| `PICKLE_POOL_NAME` | `guest-private` | |
| `PICKLE_POOL_CIDR` | `198.19.0.0/16` | |
| `PICKLE_POOL_GATEWAY` | `198.19.0.1` | CIDR 안에 있고 호스트가 브리지에서 들고 있어야 한다 |
| `PICKLE_POOL_DNS` | `["8.8.8.8"]` | JSON 배열 |
| `PICKLE_POOL_RESERVED` | `[{"from": "198.19.0.0", "to": "198.19.0.255"}]` | 양끝을 포함하는 범위의 JSON 배열. 각각 CIDR 안에 있어야 한다 |
| `PICKLE_RELAY_NAME` | `lightsail-1` | |
| `PICKLE_RELAY_SOURCE_IP` | `100.64.0.1` | 릴레이의 터널 쪽 주소. 동기화 호출을 받아 주는 유일한 피어다 |
| `PICKLE_RELAY_PORT_BAND` | `10000-19999` | 1024-65535 안 |
| `PICKLE_ROOT_DOMAIN` | `pusan.dev` | 인증서 범위가 `*.<root>`가 된다 |
| `PICKLE_WILDCARD_CERT` | `/etc/nginx/pickle-certs/pusan-dev.crt` | 루트 도메인에서 기본값이 정해진다. `*.<root>`를 덮어야 한다 |

```bash
PICKLE_RELAY_PUBLIC_HOST=ssh.example.dev \
  bash /srv/pickle/infra/scripts/apply-platform-inventory.sh
```

실행이 끝나면 방금 측정한 수치와 대조한 노드, 사용량을 포함한 풀, 공인 호스트를 포함한
릴레이, 모든 플랫폼 와일드카드 행을 출력하고 마지막에 통과/실패 목록을 낸다. 변경 전 행은
무엇을 쓰기 전에
`/srv/pickle/backup/platform-inventory-<timestamp>/inventory-before.sql`(데이터만 담은 insert)로
덤프된다.

### 환경 변수: `apply-settings.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_CONTACT_EMAIL` | `ops@example.org` | **필수.** 콘솔 푸터와 점검 화면, 오류 화면에 노출되고 두 법적 문서가 연락처로 지목한다. 설정하지 않으면 스크립트가 터미널에서 묻는다. 물어볼 터미널이 없을 때만 거부하므로 무인 실행이 값을 지어내는 일은 없다. 비운 채로 게시하려면 `none`이라고 답한다 |
| `PICKLE_ROOT_DOMAIN` | `pusan.dev` | `allowed_root_domains`의 유일한 항목이 된다. 인벤토리 스크립트가 읽는 것과 같은 변수라 이 배포가 어느 도메인으로 게시하는지에 대해 둘이 어긋날 수 없다. 플랫폼 루트가 둘 이상인 배포에서는(4번이 루트마다 와일드카드 쌍을 설치한다) 이 단계 뒤에 나머지 루트를 관리 콘솔에서 추가한다. 다른 키와 마찬가지로 이미 있는 행은 재실행이 다시 쓰지 않는다 |
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_DATA_DIR` | `<repo>/data` | 큐레이션된 단어 목록이 있는 곳 |

```bash
PICKLE_CONTACT_EMAIL=ops@example.org \
  bash /srv/pickle/infra/scripts/apply-settings.sh
```

설정 둘은 기본값이 공급할 수 있는 값이 아니라 운영자가 큐레이션하는 목록이라 `data/`에
한 줄에 하나씩 둔다. `reserved-subdomains.txt`(조직이 이미 게시하고 있어 아무도 claim 할 수
없는 이름)와 `profanity-subdomains.txt`다. 이름을 추가하는 것이 리뷰어가 읽을 수 있는 한 줄
diff 가 된다. 실행할 때마다 새로 읽지만 다른 키와 마찬가지로 아직 행이 없는 경우에만
쓴다. 행이 생긴 뒤에는 목록을 늘리는 일을 관리 콘솔에서 하고, 파일은 부트스트랩용 사본으로
남는다. 손으로 둘을 맞춰 두지 않으면 재구축이 몇 달치 큐레이션을 조용히 되돌린다.

### 환경 변수: `apply-terms.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_DATA_DIR` | `<repo>/data` | 문서는 `<data>/terms/*.md`에서 읽는다 |

```bash
bash /srv/pickle/infra/scripts/apply-terms.sh
```

문서 하나가 파일 하나다. 네 줄짜리 헤더(`doc_type`, `version`, `title`, `effective_at`),
`---` 줄, 그다음 Markdown 본문이다. `effective_at`은 실제 타임스탬프이고 애플리케이션은
날짜가 지난 것 중 가장 높은 버전을 현재 버전으로 취급한다. 그래서 개정본을 미리 게시해 두고
스스로 발효되게 할 수 있다.

문서를 개정한다는 것은 게시된 것을 편집하는 것이 아니라 **다음 버전 번호를 가진 파일을
추가하는 것**이다. 스크립트가 이것을 강제한다. 파일을 데이터베이스의 내용과 비교해서 게시된
버전의 본문이나 제목이 달라졌으면 거부한다. 사용자가 게시된 문안에 이미 동의했으므로,
조용히 고쳐 쓰면 그들의 동의 기록이 더 이상 존재하지 않는 문서를 가리키게 된다.

### 환경 변수: `apply-os-catalog.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_NODE` | `pve-node` | 카탈로그 행을 이 노드에 이름으로 resolve 한다. 노드 행이 없으면 아무것도 쓰지 않고 그렇다고 알린다 |

템플릿 자체는 환경 변수가 아니라 스크립트 안의 목록이다. 무엇을 쓰기 전에 각각이 이
호스트에 이름과 VMID 로 존재하는지 확인한다. 여기 없는 템플릿의 행은 요청 폼에서 선택
가능한 채로 clone 시점에 실패하기 때문이다.

### 환경 변수: `apply-relay-token.sh`

| 변수 | 예시 | 비고 |
|---|---|---|
| `PICKLE_ADMIN_EMAIL` | | **설정하지 않으면 터미널에서 묻는다.** 토큰을 관리 API 로 발급하므로 SYS_ADMIN 자격으로 실행해야 한다 |
| `PICKLE_ADMIN_PASSWORD` | | 설정하지 않으면 터미널에서 묻고 화면에 표시하지 않는다. 토큰 발급이 재인증 뒤에 있어 한 번의 실행에서 비밀번호를 두 번 증명한다 |
| `PICKLE_RELAY_NAME` | `lightsail-1` | 어느 릴레이 행에 발급할지 |
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_TUNNEL_CTID` | `102` | 릴레이의 터널 주소에 닿을 수 있는 컨테이너. 릴레이의 admin 포트는 Proxmox 호스트에서 닿지 않으므로 TCP 연결만 이 컨테이너로 프록시하고 ssh 자체는 호스트에서 돈다. 개인키는 컨테이너에 들어가지 않는다 |
| `PICKLE_RELAY_SSH_KEY` | `$VAULT/lightsail-ssh.pem` | |
| `PICKLE_RELAY_SSH_USER` | `admin` | |
| `PICKLE_RELAY_SSH_PORT` | `22` | |

이름에 주의한다. `deploy-relay.sh`는 같은 SSH 값 셋을 `RELAY_SSH_KEY`와 `RELAY_SSH_PORT`,
`RELAY_HOST`라는 이름으로 읽는다. 새 환경에서는 두 벌을 모두 설정해야 한다.
