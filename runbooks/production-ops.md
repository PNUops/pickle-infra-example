# 분리 운영 container의 상태 점검과 와일드카드 갱신

운영 host에서 root로 수행합니다. 실제 container 이름과 unit, DB, 인증서 경로를 먼저
읽고 선택합니다. 원본 application과 DB writer의 hold는 유지합니다.

## 정기 상태 점검

`scripts/production-health.py --config <root-owned JSON>`은 선택 host와 container의
서비스, root filesystem, `eth0` MTU, API readiness, console 정적 응답, gateway snapshot health,
DB와 Flyway, JobRunr worker/정기 작업, nginx 설정과 인증서 유효기간을 읽습니다.
`hosts/production/health.example.json`에 config 형식이 있습니다.
Gateway health는 실제 bind 주소를 사용하며 proxy container에서 요청합니다.
상태 endpoint 성공만으로 실제 모델 생성이나 공개 통신 성공을 판단하지 않습니다.
`<CTID>:mtu`는 `pct config`의 `net0` `mtu=`, 호스트 `veth<CTID>i0`, guest `eth0` 세 값이 같은지
봅니다. `mtu=`가 없으면 veth와 guest만 비교합니다. guest만 1500으로 돌아간 상태는 연결과
health endpoint가 성공해도 큰 패킷이 버려지므로 이 점검이 유일한 신호입니다. 실패하면
[후보 LLM 게이트웨이 런북](candidate-llm-gateway.md)의 「guest MTU 확인」 절로 hook을 확인합니다.

1. 새 versioned root 디렉터리에 `production-health.py`, 기존 `cron-wrap.sh`,
   `refresh-wildcard-cert-row.sh`와 `lib/ct.sh`를 보관합니다. 기존 파일은 덮어쓰지 않습니다.
2. 실제 config를 root0600으로 `/etc/pickle-production-health.json`에 작성합니다.
   새 `/etc/pickle-production-ops.env`의 `PICKLE_OPS_RELEASE`는 해당 versioned 경로입니다.
3. baseline을 한 번 실행하고 결과/실패를 기록합니다. 실제 실패가 있으면 타이머를
   활성화하기 전에 원인을 확인합니다.
   예시 config 기준 34 probes의 각12초 상한보다 unit600초가 길게 잡혀 있습니다.
   MTU 점검은 컨테이너마다 `pct config`와 `pct exec`를 한 번씩 더 부릅니다. Config의 점검 항목을
   늘리면 이 상한도 다시 대조합니다.
4. 검토한 `pickle-production-health.service/timer`를 새로 설치하고 문법 검사 뒤
   타이머를 활성화합니다. 기존 `pickle-health` unit을 재사용하거나 source hold를 풀지 않습니다.
5. 첫 실제 timer 실행의 rc, journal과 `/var/lib/pickle-ops/production-health.ok/fail`을
   확인합니다. `cron-wrap.sh`의 기존 optional notify hook은 현 상태대로 두며
   별도 SMTP 전달이나 메일 수신을 검증한 것으로 기록하지 않습니다.

원복은 자신이 설치한 target timer만 stop/disable하고 새 unit/config의 선택을 회수합니다.
원본 서비스나 source writer를 다시 켜지 않습니다.

## Managed VM 포트 포워딩의 실행 설정

신규 포트 할당에는 DB `port_forwarding_enabled=true`와 API의
`PICKLE_RELAY_RETIREMENT_ENABLED=true`가 모두 필요합니다. Consumer의 armed/ledger/
namespace/fresh 상태도 대조합니다. API 실행 옵션은 env에 추가한 것만으로 적용되지
않으므로 실제 process 값을 확인합니다. 기존 env 복사본의 unset/default-off를
정상 운영 설정으로 가정하지 않습니다. 변경 전에 웹 터미널/SSH 연결과 gateway 요청,
메일 외부효과 작업이 유휴인지 확인하고 필요한 API 재기동만 수행합니다.
동일 JAR을 재기동해도 기존 Flyway startup 검증은 동작하므로 현재 DB와 JAR의
migration 목록/검증 이력을 확인합니다. 다른 설정과 key, JAR은 보존합니다.
새 mapping이나 retirement/외부효과가 발생한 뒤에는 producer를 끄거나 source를
다시 켜서 되돌리지 않고 대상에서 복구합니다.

## DNS-01 갱신 소유와 expiry row

와일드카드 static 사본은 다음 갱신을 자동으로 받지 않습니다. 운영 proxy가 certbot
lineage를 소유하고 렌더된 vhost가 그 live 파일을 따라가도록 설정합니다.

- Google DNS credential은 기존 DNS-01 전용 key를 유지합니다. API DNS key와
  교환하거나 vendor key를 회전하지 않습니다. 평문은 Mac 파일/Git/출력에 남기지 않습니다.
- 대상의 현재 certbot과 ABI가 같은 Debian stable DNS plugin을 선택합니다.
  [Debian13 plugin4.0.0-2](https://packages.debian.org/trixie/python3-certbot-dns-google)는
  certbot ABI4를 요구합니다. Upstream stable은 [5.8.0](https://github.com/certbot/certbot/releases/tag/v5.8.0)이며,
  기존 distro certbot4와의 ABI 호환을 유지하기 위해 해당 plugin4를 선택합니다.
  설치 전 실제 버전과 `apt-get -s --no-install-recommends --no-remove install`의
  목록을 대조하며 기존 daemon restart나 certbot 전체 교체가 필요하면 별도 판단합니다.
- source lineage의 archive/live/renewal과 DNS credential을 SSH 보호 채널로 대상의
  새 root0700 staging에 전송합니다. 구성원의 경로/type/bytes를 검증하고 기존 target
  lineage나 key 파일을 덮어쓰지 않습니다. 대상 기존 ACME account를 선택하면 source
  ACME account의 private key는 옮기지 않습니다.
- target lineage의 제한된 `renew --cert-name example.com --dry-run`으로 DNS-01과 TXT
  정리를 확인합니다. 실제 발급/키 회전과 dry-run을 구분해 기록합니다.
- 격리 config-dir만으로 기본 CLI ini의 hook 상속이 차단되지는 않습니다.
  `/etc/letsencrypt/cli.ini`와 root/XDG의 CLI 파일, renewal inline hook을 확인하고
  dry-run은 directory/deploy hook을 실행하지 않게 선택합니다. 검토한 설정의 SHA와
  기존 target account 경로도 실제 선택 직전에 대조합니다.
- source certbot이 idle인지 확인하고 wildcard renewal config만 보호된 새 폴더로
  이동합니다. 기존 certificate/key와 다른 lineage, certbot timer는 보존합니다.
  대상만 wildcard renewal owner임을 전후 조회로 확인합니다.
  Source와 target의 변경 구간은 Certbot native config-directory lock으로 보호하며,
  충돌하면 효과 전에 멈춥니다. Timer를 그대로 두고 inactive 조회만으로 경쟁을 가정하지 않습니다.
- 대상의 기존 static pair는 새 보호 폴더에 보존한 뒤 원 경로를 해당 lineage의
  fullchain/key symlink로 선택합니다. 현재 leaf/key bytes가 같은지 먼저 확인하고
  둘 다 선택한 뒤 `nginx -t`와 graceful reload를 수행합니다. proxy/API restart는 필요 없습니다.
- `hosts/production/certbot/pickle-wildcard-nginx-reload.sh`를 별도 deploy hook으로
  설치합니다. 다른 lineage의 hook은 보존합니다. 갱신 때도 nginx 문법 검사를 먼저 합니다.

DB expiry 갱신은 source timer 대신 새 target `pickle-production-wildcard-cert-row`를
사용합니다. `/etc/pickle-production-ops.env`에 다음 선택을 실제 이름과 맞춥니다.

```ini
PICKLE_DB_CTID=204
PICKLE_DB_CT_HOSTNAME=example-db
PICKLE_PROXY_CTID=200
PICKLE_PROXY_CT_HOSTNAME=example-proxy
PICKLE_DB=example_prod
PICKLE_ROOT_DOMAIN=example.com
PICKLE_WILDCARD_CERT=/etc/letsencrypt/live/example.com/fullchain.pem
```

기존 `refresh-wildcard-cert-row.sh`의 기본 source 선택은 유지합니다. target env가
PostgreSQL container와 proxy의 hostname을 각각 확인하게 합니다. 기존 row를 갱신하며
새 row를 생성하지 않고 REVOKED 상태를 해제하지 않습니다. 첫 직접 실행과 자연 timer 실행,
실제 파일/DB not_after 일치를 확인합니다. 새로운 credential과 선택은 다음 암호화
proxy 백업에 포함됐는지 연결해서 기록합니다.
선택 leaf가 없으면 기존 row script는 정상 skip하므로 exit0만으로 갱신 완료를 판단하지
않습니다. 첫 설치에서는 live leaf/SAN/유효기간과 기존 ACTIVE row, 갱신 뒤 실제 expiry
동등성까지 확인한 뒤 타이머를 활성화합니다.

문제가 생기면 target wildcard 갱신을 중단하고 보존한 static pair를 다시 선택합니다.
소유를 source로 돌릴지는 대상 갱신/DB 효과를 확인한 뒤 결정합니다. source DB writer를
자동 재개하는 절차는 아닙니다.
