# 분리 운영 container의 상태 점검과 와일드카드 갱신

운영 호스트에서 root로 수행해요. 실제 컨테이너 이름과 unit, DB, 인증서 경로를 먼저
확인해 선택해요. 원본 application과 DB writer의 hold는 유지해요.

## 정기 상태 점검

`scripts/production-health.py --config <root-owned JSON>`은 선택한 호스트와 컨테이너의
서비스, root filesystem, `eth0` MTU, API readiness, console 정적 응답, gateway snapshot health,
DB와 Flyway, JobRunr worker/정기 작업, nginx 설정과 인증서 유효기간을 읽어요.
`hosts/production/health.example.json`에 config 형식이 있어요.
Gateway health는 proxy 컨테이너에서 실제 bind 주소로 요청해요.
상태 endpoint 성공만으로 실제 모델 생성이나 공개 통신 성공을 판단하지 않아요.
`<CTID>:mtu`는 `pct config`의 `net0` `mtu=`, 호스트 `veth<CTID>i0`, guest `eth0`을 대조해요.
`mtu=`가 없으면 veth와 guest만 비교해요. guest만 1500으로 돌아가면 health 응답이
정상이어도 큰 패킷이 버려질 수 있어요. 실패하면 [후보 LLM 게이트웨이 런북](candidate-llm-gateway.md)의 「guest MTU 확인」 절에서 hook을 확인해요.

1. 새 versioned root 디렉터리에 `production-health.py`, 기존 `cron-wrap.sh`,
   `refresh-wildcard-cert-row.sh`와 `lib/ct.sh`를 보관해요. 기존 파일은 덮어쓰지 않아요.
2. 실제 config를 root0600으로 `/etc/pickle-production-health.json`에 작성해요.
   새 `/etc/pickle-production-ops.env`의 `PICKLE_OPS_RELEASE`는 해당 versioned 경로예요.
3. baseline을 한 번 실행해 결과와 실패를 기록하고, 실패 원인을 확인한 뒤 타이머를 활성화해요.
   예시 config의 34 probes와 명령당 12초 상한에 맞춰 unit600초를 사용해요.
   MTU 점검은 명령을 두 번(`pct config`, `pct exec`) 부르므로 최악 합계는 약 468초예요.
   Config의 점검 항목을 늘리면 이 상한도 다시 대조해요.
4. 검토한 `pickle-production-health.service/timer`를 새로 설치하고 문법 검사 뒤
   타이머를 활성화해요. 기존 `pickle-health` unit을 재사용하거나 source hold를 풀지 않아요.
5. 첫 자연 timer 실행의 rc와 journal, `/var/lib/pickle-ops/production-health.ok/fail`을 확인해요.
   `cron-wrap.sh`의 optional notify hook은 그대로 두며, 이 결과를 SMTP 전달이나 메일 수신의
   검증으로 기록하지 않아요.

원복은 자신이 설치한 target timer만 stop/disable하고 새 unit과 config의 선택을 회수해요.
원본 서비스나 source writer를 다시 켜지 않아요.

## Managed VM 포트 포워딩의 실행 설정

신규 포트 할당에는 DB `port_forwarding_enabled=true`와 API의
`PICKLE_RELAY_RETIREMENT_ENABLED=true`가 모두 필요해요. Consumer의 armed/ledger/
namespace/fresh 상태도 대조해요. env에 추가한 것만으로는 실행 옵션이 적용되지 않으므로
실제 process 값을 확인해요. 기존 env 복사본의 unset/default-off를 정상 운영 설정으로 가정하지 않아요.
변경 전에 웹 터미널과 SSH 연결, gateway 요청, 메일 외부효과 작업이 유휴인지 확인하고
필요한 API 재기동만 수행해요. 같은 JAR도 기동 때 Flyway를 검증하므로 현재 DB와 JAR의
migration 목록과 검증 이력을 확인해요. 다른 설정과 key, JAR은 보존해요.
새 mapping이나 retirement/외부효과가 발생한 뒤에는 producer를 끄거나 source를 다시 켜서
되돌리지 않고 대상에서 복구해요.

## DNS-01 갱신 소유와 expiry row

와일드카드 static 사본은 다음 갱신을 자동으로 받지 않아요. 운영 proxy가 certbot lineage를
소유하고 렌더링한 vhost가 그 live 파일을 따르게 설정해요.

- Google DNS credential은 기존 DNS-01 전용 key를 유지해요. API DNS key와 교환하거나
  vendor key를 회전하지 않아요. 평문은 Mac 파일과 Git, 출력에 남기지 않아요.
- 현재 certbot과 ABI가 같은 Debian stable DNS plugin을 선택해요.
  [Debian13 plugin4.0.0-2](https://packages.debian.org/trixie/python3-certbot-dns-google)는 certbot ABI4를 요구해요.
  비교한 upstream 릴리스는 [5.8.0](https://github.com/certbot/certbot/releases/tag/v5.8.0)이며, 기존 distro certbot4와의 ABI 호환을
  유지하기 위해 plugin4를 선택해요. 설치 전 실제 버전과
  `apt-get -s --no-install-recommends --no-remove install`의 목록을 대조하고,
  기존 daemon restart나 certbot 전체 교체가 필요하면 별도로 판단해요.
- source lineage의 archive/live/renewal과 DNS credential을 SSH 보호 채널로 대상의 새
  root0700 staging에 전송해요. 구성원의 경로/type/bytes를 검증하고 기존 target lineage나
  key 파일을 덮어쓰지 않아요. 대상의 기존 ACME account를 선택하면 source ACME account의
  private key는 옮기지 않아요.
- target lineage의 제한된 `renew --cert-name example.com --dry-run`으로 DNS-01과 TXT 정리를 확인해요.
  실제 발급과 키 회전을 dry-run과 구분해 기록해요.
- 격리 config-dir만으로 기본 CLI ini의 hook 상속이 차단되지는 않아요.
  `/etc/letsencrypt/cli.ini`와 root/XDG의 CLI 파일, renewal inline hook을 확인하고
  dry-run에서는 directory/deploy hook을 실행하지 않게 선택해요. 검토한 설정의 SHA와
  기존 target account 경로도 실제 선택 직전에 대조해요.
- source certbot이 idle인지 확인하고 wildcard renewal config만 보호된 새 폴더로 옮겨요.
  기존 certificate/key와 다른 lineage, certbot timer는 보존하고 대상만 wildcard renewal
  owner인지 전후로 확인해요. Source와 target의 변경 구간은 Certbot native config-directory
  lock으로 보호하며 충돌하면 효과 전에 멈춰요. 기존 timer를 둔 채 inactive 상태만 보고
  경쟁이 없다고 판단하지 않아요.
- 대상의 기존 static pair를 새 보호 폴더에 보존한 뒤 원 경로를 lineage의 fullchain/key
  symlink로 선택해요. 현재 leaf/key bytes가 같은지 먼저 확인하고, 둘 다 선택한 뒤
  `nginx -t`와 graceful reload를 수행해요. proxy/API restart는 필요 없어요.
- `hosts/production/certbot/pickle-wildcard-nginx-reload.sh`를 별도 deploy hook으로 설치해요.
  다른 lineage의 hook은 보존하고 갱신 때도 nginx 문법 검사를 먼저 해요.

DB expiry 갱신은 source timer 대신 새 target `pickle-production-wildcard-cert-row`를 써요.
`/etc/pickle-production-ops.env`의 다음 선택을 실제 이름과 맞춰요.

```ini
PICKLE_DB_CTID=204
PICKLE_DB_CT_HOSTNAME=example-db
PICKLE_PROXY_CTID=200
PICKLE_PROXY_CT_HOSTNAME=example-proxy
PICKLE_DB=example_prod
PICKLE_ROOT_DOMAIN=example.com
PICKLE_WILDCARD_CERT=/etc/letsencrypt/live/example.com/fullchain.pem
```

기존 `refresh-wildcard-cert-row.sh`의 기본 source 선택은 유지해요. target env가 PostgreSQL
컨테이너와 proxy의 hostname을 각각 확인하게 해요. 기존 row만 갱신하고 새 row를 만들거나
REVOKED 상태를 해제하지 않아요. 첫 직접 실행과 자연 timer 실행, 실제 파일과 DB의 not_after
일치를 확인해요. 새로운 credential과 선택이 다음 암호화 proxy 백업에 포함됐는지도 기록해요.
선택한 leaf가 없으면 기존 row script는 정상 skip하므로 exit0만으로 완료를 판단하지 않아요.
첫 설치에서는 live leaf와 SAN, 유효기간, 기존 ACTIVE row, 갱신 뒤 실제 expiry 동등성을
확인한 다음 타이머를 활성화해요.

문제가 생기면 target wildcard 갱신을 중단하고 보존한 static pair를 다시 선택해요.
소유를 source로 돌릴지는 대상의 갱신과 DB 효과를 확인한 뒤 정해요. source DB writer를
자동 재개하는 절차는 아니에요.

## TLS 암호군과 로그 보존

운영 호스트에서 root로 수행해요. 두 스크립트는 정기 상태 점검과 같은 versioned root
디렉터리에 `lib/ct.sh`와 함께 둬요. 백업은 `PICKLE_ROOT/backup/` 아래에 남으므로
`PICKLE_ROOT=/srv/pickle`을 지정해요.

**TLS 암호군.** 공개 플랫폼 이름(`pickle.pusan.ac.kr`, `llm.pcl.kr`, 발행된 사용자 도메인)의
브라우저 TLS는 운영 proxy 컨테이너가 종단해요. pve-node LXC 100은 SNI로 불투명하게
넘기므로 그쪽 `pickle-tls.conf`는 이 이름들을 덮지 않아요.

```sh
PICKLE_ROOT=/srv/pickle bash <release>/scripts/apply-tls-ciphers.sh production
```

스크립트는 `/etc/nginx`를 백업하고 `conf.d/pickle-tls.conf`(http{} 전역)를 넣은 뒤
`nginx -t`와 reload를 수행해요. 전후로 두 공개 이름의 응답 코드를 확인하고, TLS 1.2 협상
suite가 허용 목록 안에 있는지와 **CBC만 제시한 TLS 1.2 handshake가 실패하는지**를 확인해요.
reload는 기존 연결을 끊지 않아요. 실패하면 출력된 백업을 풀거나 해당 파일을 지운 뒤 reload해요.
적용 전 기준선은 실제 suite예요. 접속 로그에는 suite가 없으므로 proxy 컨테이너 network
namespace의 loopback `:8443`에서 서버 쪽 ServerHello만 캡처해(`tcp src port 8443`, handshake
record) 버전과 suite를 세요.

**로그 보존.**

```sh
bash <release>/scripts/apply-log-retention.sh --dry-run production   # 변경 내용 미리 확인
bash <release>/scripts/apply-log-retention.sh production
# 대기 노드
bash <release>/scripts/apply-log-retention.sh host
```

각 대상에 journald drop-in `/etc/systemd/journald.conf.d/pickle.conf`를 쓰고 바뀐 곳만
`systemd-journald`를 재시작해요. 서비스의 stdout 스트림은 journald fdstore로 유지돼요.
값은 스크립트 머리말이 기준이에요. 상한을 현재 사용량보다 낮추면 재시작 즉시 오래된 journal이
지워지므로, drop-in이 바뀌는 대상 중 이런 곳이 있으면 쓰기 전에 멈춰요. 같은 값인 대상은
건너뛰며, 실제로 줄이려면 `ALLOW_SHRINK=1`을 사용해요. 대상마다 상한과 현재 사용량을
출력하고 `--dry-run`은 인자 순서와 상관없이 받아요. 적용 전 대상마다 `journalctl --disk-usage`와
가장 오래된 항목 시각을 기록해요.

최종 갱신: 2026-11-03
