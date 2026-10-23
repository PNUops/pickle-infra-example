# 외부 HTTPS 감시와 제한된 결과 수집

이 패키지는 외부 호스트에서 공개 console/API의 HTTPS 응답을 관측하고, 고정 SSH reader의
결과를 collector 호스트로 옮기는 준비 도구입니다. 로컬 생성 감시의 배포와 독립이며 코드와
템플릿만으로 AWS 감시나 교차 heartbeat가 설치됐다고 기록하지 않습니다. 공통 절차는
[자체 서빙 LLM 감시](llm-health-monitor.md)를 따릅니다.

## 외부 공개 경로

`public-service-health-probe.py`는 root 소유 0600 설정에 명시한 HTTPS 호스트만 요청합니다.
실제 observer hostname도 설정과 같아야 합니다. CA/hostname 검증을 끄거나 redirect를
따라가지 않습니다. console은 HTTP200·HTML content type과 root marker를 확인하고,
API는 HTTP200·JSON content type과 지정한 필드 값/타입을 확인합니다. SPA HTML이 API
응답으로 잘못 돌아오는 상황을 정상 처리하지 않습니다. 인증 실패 401/403은 정상 생성의
증거가 아닙니다.

공개 GET은 DNS/TLS/응답 읽기를 포함해 별도 자식 프로세스에서 총 10초로 제한합니다.
DNS resolver가 멈춰도 이 상한에 종료됩니다. 인증서가 7일 이내 만료되면 별도 TLS 실패로
기록합니다. 요청마다 새 TLS 연결을 열며 비밀 header와 body를 결과로 내보내지 않습니다.
기존 캠퍼스 경로 결과와 외부 공개 결과를 별도 source로 수집해야 감시 위치의 경로 장애를
운영 서비스 전체 장애와 구분할 수 있습니다.
DNS 조회 오류나 resolver 단계 timeout은 `DNS_OBSERVATION_UNAVAILABLE`, phase DNS와
해당 target의 OBSERVER_ERROR로 보수적으로 구분합니다. 단일 resolver 결과로 authoritative
NXDOMAIN이나 전체 서비스 다운을 확정하지 않습니다. 실제 TLS chain/hostname 오류와
예상 HTTP/JSON 불일치는 해당 공개 target의 FAILED로 남깁니다.

인증서 보안 오류는 HTTP 경로 장애와 독립적인 `kind=tls` source로 추가합니다.
`public-tls-production`·`public-tls-development`·`public-tls-gateway`는 실제 HTTP 요청 없이
CA/hostname 검증과 7일 만료 기준만 확인합니다. 인증서 chain/SAN 검증 실패나 확인된
7일 이내 만료만 FAILED이고 DNS/TCP/인증서 확인 전 timeout·handshake 불가는
OBSERVER_ERROR입니다. 별도 HTTP source가 이미 장애여도 새 TLS 보안 incident를 숨기지
않으며 reason/IP가 바뀔 때마다 사건을 만들지 않습니다.

`collector-critical-tls.example.json`처럼 `component=critical-tls`, `expected_kind=tls`를
지정한 source만 `failure_count=1`을 허용합니다. collector가 고정 보안 reason을 다시
확인하며 다른 kind나 TCP/HTTP 오류는 observer로 처리합니다. 일반 source의 2~5회 기준은
유지합니다. 설정은 `public-tls-<target>.json`, unit은 `pickle-public-health@tls-<target>.timer`를
사용합니다. 초기 통지는 계속 off이며 배치된 TLS source만 reader의 `tls_sources`에 넣습니다.

`hosts/llm-monitor/`의 public JSON은 예시 도메인이므로 실제 대상과 readiness JSON 경로를
읽기 확인한 뒤 변경합니다. `public-console.json`, `public-api.json`,
`public-development.json`으로 설치하면 각각 `pickle-public-health@console.timer`,
`@api.timer`, `@development.timer`가 1분마다 실행합니다. 기본 파일 hash와 권한, Linux
`systemd-analyze verify`를 확인하고 통지 off collector에 먼저 연결합니다. 각 결과는
`/var/lib/pickle-public-<instance>/result.json`에 root 0600으로 남습니다.

## 공개 실제 생성 canary

`public-generation`은 전용 비학생 진단 key 로 공개 gateway 에 실제 Chat 요청을 보내 고정
정답을 확인합니다. 모델이 어느 upstream 으로 route 되든 호출합니다. 자체 서빙이든 유료
외부 API 든 같습니다(운영자 결정 2026-10-23: 「luna일 때도 호출해도 됨. 자체 서빙 제약 조건
걸지 말자.」). 2026-10-24 이전에는 아래 policy receipt 가 「자체 서빙 route·유료 0」을
증명해야만 호출했고, 학생 모델이 유료 route 로 바뀌자 canary 가 통째로 멈췄습니다.
그 receipt 는 더 이상 canary 의 조건이 아니며, 설정에 `policy_attestation` 이 남아 있으면
probe 가 설정 오류로 거부합니다. 이때 보이는 증상은 journal 의
`PUBLIC_CONFIGURATION_OR_STATE_INVALID` 와 exit 2 뿐이고(내부 사유 `PUBLIC_CANARY_INVALID` 는
출력되지 않습니다) `result.json` 이 다시 쓰이지 않으므로, collector 는 신선도 720초가 지난
뒤 그 source 를 stale(UNKNOWN)로 봅니다. 클라이언트 header 나 설정으로 학생 무과금이나 서버 라우팅이
보장되지 않으며 이 도구는 서버 정책을 만들거나 바꾸지 않고 학생 키를 대용하지 않습니다.

기본 `canary_enabled=false`에서는 credential도 읽지 않고 생성 요청도 보내지 않습니다.
켜기 전에 전용 키의 실제 인증/Chat 인가, 학생 차감 0, 진단 통계/사용량 적재를 확인합니다.

비용의 상한은 두 겹입니다. 아래 서버 쪽 경계는 모델 행이 TOKEN 축에 있는 동안의
이야기입니다. 일 토큰 quota 는 TOKEN 축 모델에만 걸리고, upstream 을 외부 API 로 바꿔도 축은
움직이지 않습니다. 모델을 CREDIT 축으로 옮기면 진단 key 에 credential 이 필요해지고 64k 는 더
이상 경계가 아니므로 이 절을 다시 검토합니다. probe 쪽은 최소 간격 300초, UTC 하루 312시도(실패·중단 포함,
전송 전에 durable 저장), 요청당 `max_completion_tokens` 128, 전체 20초입니다. 서버 쪽은
진단 key 자체의 한도(Chat 전용, rpm 1, 동시 1, 일 64k 토큰)입니다. 2026-10-24 실측(유료
추론 모델 route): 입력 19토큰, 출력 17~20토큰(그중 추론 10~13), 요청당 약 $0.00001,
지연 약 2초. 5분 간격 288회면 하루 약 $0.003 이고 128토큰을 매번 다 써도 하루 약 $0.02
입니다. 하루 토큰은 약 11k 로 key 한도 64k 안입니다.

출력 상한 8 은 추론 모델에서 쓸 수 없습니다. 같은 실측에서 8토큰은 추론에 다 쓰여
`finish_reason=length`, content 없음으로 끝났습니다. 판정은 정답 `4`, `finish_reason=stop`,
`0 < completion_tokens <= 128` 입니다. completion 에는 추론 토큰이 포함되므로 직접 DGX
probe 의 8 과 다른 상한을 씁니다. 공개 요청의 `reasoning_effort` 는 gateway 가
`unsupported_parameter` 로 거부하므로 보내지 않습니다. 자체 서빙 upstream 의 thinking 기본값이
켜지면 128 을 넘을 수 있으니, 그 기본값을 바꿀 때 이 상한을 함께 다시 잽니다.

토큰은 `/etc/pickle-llm-health/diagnostic-token`의 root 0600 파일에서 systemd credential로
넘깁니다. 공개 생성 wire에는 model/messages/temperature/top_p/max_completion_tokens/stream의
허용된 6개 필드만 사용합니다. 공개 생성 401/403은 전용 credential/정책 문제인
CREDENTIAL_REJECTED observer 오류이며 생성 다운으로 표현하지 않습니다. 인증 없는 models
조회의 401도 실제 생성 정상의 증거가 아닙니다.

결과는 reader 를 거쳐 collector 로 갑니다. 전송 형식의 `completion_tokens` 상한은 128 입니다.
이 상한이 8 이던 전송 코드는 128 토큰까지의 결과를 받으면 거부합니다. observer 쪽 reader 는
source 마다 따로 처리하므로 `public-generation` 하나만 `REMOTE_RESULT_UNAVAILABLE` 로 떨어지지만,
collector 쪽 importer 는 **그 bundle 전체를** 형식 오류로 버려 AWS observer 의 모든 source 가
함께 사라집니다. 그래서 배포 순서는 전송 라이브러리(`lib/health_result_transport.py`, observer 와
collector 양쪽) 먼저, probe 와 설정이 그다음입니다.

## 고정 policy receipt 전달

이 receipt 는 2026-10-24 부터 공개 canary 의 조건이 아닙니다(위 절). 전송 형식과 reader·
importer 의 처리는 남아 있지만 소비자가 없습니다. 운영 host 의 producer 를 은퇴시킨 뒤에는
receipt 가 `valid=false` 로 고정되며, 이는 장애가 아닙니다.

`read-health-v1` bundle은 opt-in `include_policy_attestation=true`일 때만 top-level
`policy_attestation`을 포함합니다. 새 SSH 명령이나 추가 임의 파일 경로는 받지 않습니다.
reader의 `gateway-host` 역할은 `/var/lib/pickle-public-policy/attestation.json`만 읽고,
`collector-host` 역할은 `/var/lib/pickle-health-import-gateway/policy-attestation.json`만 읽습니다.
다른 역할에서 이 옵션을 켜면 거부합니다.

`import-gateway-policy.example.json`을 collector 호스트의 `import-gateway.json`으로
선택하면 receipt는 `/var/lib/pickle-health-import-gateway/policy-attestation.json`에 게시됩니다.
다음 `collector-host` reader가 같은 파일을 전달하고, 외부 호스트의 `import-collector.json`은
`/var/lib/pickle-health-import-collector/policy-attestation.json`에 게시합니다. 이 마지막
경로는 2026-10-24 전까지 공개 probe 설정의 `policy_attestation`으로 지정하던 자리입니다.
**지금의 canary 설정에는 `policy_attestation`을 지정하지 않습니다.** 지정하면 probe 가
설정 오류(journal 의 `PUBLIC_CONFIGURATION_OR_STATE_INVALID`, exit 2)로 거부합니다. 각 호스트명·IP·key·pin은 실제 검증값으로
대체하며 공개 예시를 그대로 활성화하지 않습니다.

importer는 key와 SSH 검증 전에 기존 허가를 원자적으로 `valid=false`로 바꿉니다. bundle의
health와 policy가 모두 검증된 뒤에만 새 receipt를 게시합니다. 누락·전송·형식·파일 게시 오류는
고정 invalid receipt로 남겨 이전 허가가 재사용되지 않게 합니다. producer나 전송이 멈추면
180초 원본 만료가 그대로 작동합니다. 가져온 시각으로 issued_at/expires_at을 갱신하지 않습니다.

valid true는 origin/model/hash와 서버 정책의 알려진 필드만 투영하고, producer의 추가 진단값이나
raw body·secret 필드는 전달하지 않습니다. valid false는 kind/valid/reason/issued_at/expires_at만
허용합니다. 전체 health+policy bundle은 계속 64KiB 이하입니다. 이 전달은 보호된 원본과
고정 SSH 경로의 검증 결과를 옮기며 서버 정책을 새로 만들거나 변경하지 않습니다.

## 고정 SSH reader와 importer

`health-result-reader.py`는 `SSH_ORIGINAL_COMMAND`가 정확히 `read-health-v1`일 때만
고정 결과를 출력합니다. 요청 인자로 파일 경로나 셸 명령을 받지 않습니다. 설정에서 실제
hostname과 다음 역할 하나를 선택합니다.

| 역할 | 허용된 결과 |
|---|---|
| `model-host` | 고정 host-query/local-generation 파일 |
| `gateway-container` | 고정 upstream-generation 파일 |
| `gateway-host` | literal CT203에 고정 Python reader와 고정 설정 경로로 `pct exec` |
| `public-observer` | 고정 public-console/api/development 파일. 별도 검증 후에만 public-generation 포함 |
| `collector-host` | 고정 `/var/lib/pickle-service-health-monitor/summary.json`의 원본 epoch heartbeat |

reader는 상태·epoch·지연·토큰·부팅 ID·알려진 숫자 지표만 whitelist로 내보냅니다. 본문과
credential, 임의 문자열 필드는 제거합니다. 파일 조회/형식 오류는 OBSERVER_ERROR이며
서비스 장애로 바꾸지 않습니다. 실제 결과의 원본 sample epoch를 유지하므로 오래된 결과를
새로 가져와도 신선해지지 않습니다.

서버 reader의 고정 경로와 Python 모듈, 설정의 모든 부모는 root 소유이며 group/other 쓰기
권한이 없어야 합니다. 결과가 root 0600이므로 reader도 보호 경계 안에서 실행합니다.
운영자가 별도 scoped key와 out-of-band host fingerprint를 검증한 뒤에만 root authorized_keys에
다음과 같은 forced-command 제한을 설치합니다. 기존 root 관리 키를 importer에 복사하거나
비밀값을 공개 예시에 넣지 않습니다. 새 사용자·키 발급과 authorized_keys 변경은 이 도구가
수행하지 않습니다.

```text
restrict,command="/usr/bin/python3 -I -B /usr/local/libexec/pickle/health-result-reader.py --config /etc/pickle-llm-health/reader.json" ssh-ed25519 <separate-reader-public-key>
```

`health-result-importer.py`는 명시적 내부 IP/user/port와 root 0600 key/known_hosts 파일을
선택합니다. `ssh -F /dev/null`로 ambient 설정을 배제하고 strict host checking,
IdentitiesOnly/BatchMode, agent/forward/ControlPath/ProxyCommand 비활성화를 강제합니다.
고정 protocol 외 원격 명령이 없고 전체 SSH는 15초·stdout 64KiB로 제한합니다.
NetBird의 RFC6598 `100.64.0.0/10`은 Python의 private 판정과 별도로 명시 허용합니다.
공개·link-local·multicast·unspecified·reserved 주소는 허용하지 않습니다. gateway-host의
PVE 명령은 제한된 PATH와 무관하게 `/usr/sbin/pct`를 명시합니다. timeout/크기 초과/오류에서는
leader가 먼저 종료돼도 stdout을 상속한 자식까지 해당 실행의 새 process group으로 종료합니다.
leader를 reap하기 전까지 PID를 보존해 process-group ID 재사용을 피합니다.

import JSON을 `/etc/pickle-llm-health/import-<instance>.json`에 설치하고 대응하는
`pickle-health-import@<instance>.timer`를 선택합니다. source 결과는
`/var/lib/pickle-health-import-<instance>/reports/<source>.json`에 원본 epoch로 저장합니다.
전체 bundle을 검사한 뒤 저장하며 전송/형식 오류 시 기존 결과를 보존하고 importer 자신의
`result.json`에 OBSERVER_ERROR를 남깁니다. collector에는 **importer 결과와 각 원격 결과를
모두** 연결해, 전송 실패 중 과거 원격 결과가 잠시 신선하게 남아도 수집 장애가 드러나게 합니다.
collector가 오래된 원격 결과를 재사용해 서비스 정상이나 복구를 확정하지 않도록 각 주기의
기존 신선도 상한을 유지합니다.

같은 import bundle의 child에는 `collector-import.example.json`처럼 `observer_parent`를 지정합니다.
부모 importer source의 `expected_kind=observer-report`를 함께 지정해야 합니다. 부모 결과의
현재 수집 오류·누락·만료·형식 오류는 공통 observer에 귀속하며, 그때 누락·만료된 child의
별도 observer incident와 미전송 observer 통지는 보류합니다. 각 child의 UNKNOWN과 원본 epoch,
reason은 summary에 남습니다. 신선한 child의 서비스 실패·TLS 보안 실패·개별 probe 오류는 계속
독립 판정합니다. 부모가 현재 HEALTHY이면 기존 incident가 복구 관찰 중이어도 child의 개별
신선도와 3회 observer 실패 기준을 적용합니다. 관계는 한 단계만 허용하며 상세 상태·전송 보존은
[최초 경보와 안정 복구](llm-health-monitor.md#최초-경보와-안정-복구)를 따릅니다.

양방향 heartbeat는 collector 호스트의 `collector-host` 역할과 외부 호스트의
`public-observer` 역할에 `include_collector_heartbeat=true`를 사용해 준비할 수 있습니다.
고정 summary의 version/timestamp/source 구조를 검증하고 원본 관측 epoch만 heartbeat로
내보냅니다. 각 서비스 장애가 있어도 collector가 관측 중이면 heartbeat는 정상이며,
summary가 오래됐으면 importer가 시간을 갱신하지 않아 상대 collector에서 UNKNOWN이 됩니다.
조회 불가/형식 오류는 즉시 별도 OBSERVER_ERROR입니다.

NetBird 전송 권한은 policy 하나에 rule 하나씩 등록합니다. 여러 rule을 성공 응답에
포함해도 모두 저장되지 않는 제약은 [upstream issue #3583](https://github.com/netbirdio/netbird/issues/3583)에
기록돼 있습니다. 출발지와 목적지의 전용 peer group, protocol, 목적지 port와
`bidirectional` 값을 명시하고 비활성 상태로 먼저 생성합니다. POST 응답과 별개로
`GET /api/policies/<id>`를 다시 조회해 rule 수와 모든 권한 필드를 대조합니다.
활성화 전후 기존 group/policy 전체를 비교하고, 허용한 각 방향에서 고정 reader의 실제
연결과 허용하지 않은 명령/port의 거부를 확인한 뒤 전송 경로를 완료로 기록합니다.
서버가 반환한 본문이나 활성 상태만으로 실제 연결을 대신하지 않습니다.

실제 양쪽의 scoped key·host pin·접속 경로와 collector source를 설치하고 양방향 신선도를
검증하기 전 상호 dead-man 완료로 기록하지 않습니다. 공유 VPN이나 SMTP가 함께 고장나는
경우에는 여전히 통지 공백이 남으며, 이 패키지는 별도 외부 dead-man 서비스를 만들지 않습니다.

## 검증과 배포·원복

새 테스트는 운영 네트워크나 SMTP를 호출하지 않습니다.

```bash
python3 -B scripts/tests/test_public_health.py
python3 -B scripts/tests/test_health_result_transport.py
```

배포는 기존 감시 package에 새 public probe/reader/importer와
`lib/health_result_transport.py`를 root 소유 0644로 더한 뒤 설정·key·host pin을 검토합니다.
서버 reader를 먼저 설치하고 forced-command 제한과 허용 protocol 외 거부를 확인한 다음,
observer의 GET과 importer timer를 통지 off로 켭니다. 24~48시간 동안 TLS/JSON 형태와
source epoch, 수집 실패와 서비스 실패 구분을 비교합니다. 공개 canary는 전용 key 검증이
끝난 뒤 별도 단계로 켭니다. 운영 단절이나 인증서 오류를 실제로 만들지 않습니다.

원복은 새 public/import timer를 먼저 disable/stop하고 oneshot 종료를 확인합니다. 새
forced-command key 한 줄과 파생 diagnostic key만 회수하며 기존 관리 키와 upstream/학생
키는 보존합니다. 이전 검증 파일을 복원하되 budget·incident·delivery를 과거로 되감지
않습니다. collector source를 조정할 때 사라진 observer를 서비스 정상으로 표시하지 않습니다.
