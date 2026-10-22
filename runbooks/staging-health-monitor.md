# 스테이징 외부 가용성과 접근통제 감시

스테이징의 외부 가용성은 허용된 외부 관측 지점에서 HTTP 200과 정확한
`candidate-ready` 본문으로 확인합니다. 비허용 관측 지점의 HTTP 403 검사는 별도
보안 source로 유지합니다. 이 문서는 도구와 설정 절차이며 실제 설치·활성화의 증거는
별도 운영 기록입니다. API나 콘솔의 실제 업무 동작을 이 health-only 응답으로
검증했다고 기록하지 않습니다.

## 관측의 분리

네 가지 설정 예제가 `hosts/llm-monitor/`에 있습니다.

| source | 종류 | 정상 조건 | 오류 구분 |
|---|---|---|---|
| `public-staging` | `public-http` | HTTP 200, `text/plain`, 정확한 `candidate-ready` bytes | 403, redirect, HTML, 추가 개행과 다른 본문은 실패 |
| `public-tls-staging` | `tls` | CA·hostname·정확한 DNS SAN 확인, 인증서 잔여 7일 이상 | chain/hostname/SAN과 7일 미만 만료는 보안 장애 |
| `public-staging-expiry-warning` | `tls-expiry-warning` | 인증서 잔여 30일 이상 | 30일 미만은 독립된 주의 사건이며 7일 미만에도 주의를 임의 해소하지 않음 |
| `staging-access-policy` | `access-policy` | 내부 ingress 경유, 동일 SNI·Host·정확한 SAN, HTTP 403 | 다른 HTTP 상태와 TLS 보안 실패는 장애, DNS/TCP/응답 관측 불능은 observer 오류 |

모든 관측은 HTTPS를 사용하고 redirect를 따라가지 않습니다. `required_dns_san`은
URL의 hostname과 같아야 하며 wildcard SAN으로 대신하지 않습니다. 외부 가용성과
7일 TLS 장애, 30일 만료 예고는 서로 다른 사건 이력을 가집니다. 이 구분으로 기존
가용성 장애가 인증서 만료 예고나 새 TLS 실패를 가리지 않게 합니다.

`access-policy`의 `connect_address`만 연결 주소를 바꿀 수 있습니다. 실제 선택한
RFC1918 IPv4 주소를 명시하며 URL의 hostname은 TLS 검증과 SNI, Host에 유지합니다.
공개 HTTP나 generation에는 연결 주소 override를 허용하지 않습니다. 이 검사는
**내부 ingress를 통한 비허용 원본의 접근통제 검사**입니다. 공개 엣지 전체의 deny나
독립 외부 비허용 원본을 검증한 것으로 기록하지 않습니다. 서버 allowlist를 넓히거나
403을 정상 서비스 응답으로 간주하는 설정 변경은 이 절차에 포함하지 않습니다.

30일 주의 source도 wire status는 기존 `FAILED`/`HEALTHY`/`OBSERVER_ERROR`를 유지합니다.
`FAILED`는 이 source의 30일 기준 미달을 뜻하며 명시적인 `severity: warning`으로
보안 장애와 구분합니다. collector와 Discord 설정 모두 이 등급을 선택해야 합니다.
등급·kind·고정 reason이 맞지 않으면 경고를 일반 장애로 수락하지 않습니다.
주의 mail/Discord 제목은 「주의」와 「주의 해소」이고, 감시 자체의 observer 오류는
일반 감시 장애로 유지합니다. 관측 불능이나 오래된 데이터는 주의 해소가 아닙니다.

## 설정과 실행

설정과 결과는 root 전용 `0600`, 상위 디렉터리는 `0700`으로 보관합니다.
예제의 hostname, allowed host, URL과 내부 ingress 주소를 실제 값으로 바꿉니다.
각 source에 새 전용 state 디렉터리를 사용합니다.

```bash
python3 -I -B /usr/local/libexec/pickle/public-service-health-probe.py \
  --config /etc/pickle-llm-health/public-staging.json \
  --state-dir /var/lib/pickle-public-staging
```

이 명령은 실제 HTTPS 관측과 전용 결과 쓰기를 수행합니다. 자체 mail이나 Discord
발송은 하지 않습니다. 외부 관측 세 개를 한 oneshot의 여러 `ExecStart`로 순차
실행할 수 있습니다. 현재 호스트의 resource slice와 service 제한을 확인하고 기존
관측과 함께 자연 실행의 메모리 peak·실패·지연을 대조합니다.

`pickle-public-observation.service/timer`는 기존 외부 console/API/development와
세 TLS 관측, 새 스테이징 세 관측을 하나의 oneshot에서 순차 실행합니다. 기존 여섯
config와 state 경로는 그대로 사용합니다. 매분 calendar를 사용하며 실행 중인
service에 재진입하지 않습니다. 고정 `public-observation-batch.sh`가 아홉 관측을
끝까지 시도하고 CLI 오류를 집계합니다. 각 request는 10초 예산이며, GNU coreutils의
`timeout`이 전체 batch를 100초에 TERM, 필요하면 추가 5초 후 KILL로 종료합니다.
별도 session을 만들지 않아 자식도 같은 process group의 종료 대상입니다.
systemd의 110초 start timeout은 추가 경계입니다. 실제 sample 간격이 180초
freshness 안에 있는지 확인합니다. 호스트 부하와
실제 I/O 지연까지 고정 주기로 보장하는 설정은 아닙니다.

한 config·권한 오류의 CLI 실패는 뒤의 나머지 관측을 생략하지 않습니다.
오류가 하나라도 있으면 batch는 nonzero로 끝납니다. 해당 결과의 freshness와
observer 상태로 실패를 검출합니다.
통합 unit의 `success`는 모든 대상이 정상이라는 증거가 아닙니다. 원본 결과와
collector의 source별 분류를 확인합니다.

통합 전에 기존 `pickle-public-health@console`, `@api`, `@development`,
`@tls-production`, `@tls-development`, `@tls-gateway` timer의 활성 상태와 unit,
설정·결과 경로를 기록합니다. 이 여섯 timer만 disable/stop하고 실행 중 service가
끝난 뒤 통합 timer를 선택합니다. 개별 service를 강제 종료하거나 config/state를
삭제하지 않습니다. 실제 자연 실행과 resource slice peak를 확인합니다. 원복은
통합 timer를 stop/disable하고 해당 service가 끝난 뒤 기존 기록에서 활성 상태였던
timer만 복구합니다. 원본 결과·사건·메일 전달 기록은 어느 방향에서도 되돌리지 않습니다.

`pickle-staging-health-collector` 예제는
별도 config/state로 수집합니다. 내부 403 probe는 기존
`pickle-public-health@.service`에 별도 config와 전용 결과 경로를 선택해 실행할 수
있으며 collector와 같은 원본 state를 쓰지 않습니다. 새 collector의 SMTP를 선택하면
네트워크와 service 예산도 실제 발신 경로에 맞춰 확인합니다.

AWS의 고정 reader에 `include_staging: true`를 지정하면 아래 두 파일만 추가합니다.

- `/var/lib/pickle-public-staging/result.json`
- `/var/lib/pickle-public-staging-expiry-warning/result.json`

`tls_sources`에는 `public-tls-staging`을 추가합니다. 이 파일은
`/var/lib/pickle-public-tls-staging/result.json`입니다. Reader는 임의 경로를 받지
않습니다. importer의 `expected_sources`도 실제 bundle과 같게 지정합니다.
최대 12개 report와 기존 64KiB 출력 제한, 15초 전송 예산을 유지합니다.
구 reader는 새 opt-in을 내보내지 않으므로 reader와 library를 먼저 확인하고
새 기대 목록을 선택합니다. 기존 DGX·gateway probe에는 새 상태 enum이 필요 없습니다.

새 스테이징 collector의 30일 source는 다음 필드를 함께 사용합니다.

```json
{
  "name": "staging-expiry-warning",
  "path": "/var/lib/pickle-health-import-aws/reports/public-staging-expiry-warning.json",
  "report_source": "public-staging-expiry-warning",
  "expected_kind": "tls-expiry-warning",
  "severity": "warning",
  "freshness_seconds": 180,
  "failure_count": 1
}
```

7일 TLS source는 `component: critical-tls`, `expected_kind: tls`,
`failure_count: 1`을 사용합니다. 403 source는 `component: access-policy`,
`expected_kind: access-policy`, `failure_count: 1`입니다. HTTP 가용성은 별도
확인 횟수를 적용합니다. importer parent 상관관계를 선택하면 공통 import 누락을
자식별 보안 장애로 중복 통보하지 않으며 실제 fresh 개별 실패는 보존합니다.

새 collector의 `notifications_enabled`와 `mail_config`만 실제 발신 정책에 맞춰
선택합니다. 기존 shared collector의 mail 설정은 유지합니다. Discord는 새
`state.json`/`summary.json` 쌍과 source 이름을 명시합니다. 주의 source에는
`severity: warning`을 추가합니다. Observer에는 주의 등급을 적용하지 않습니다.

## 전환과 기록 보존

기존 pve-node-3 공개 헤어핀 감시의 incident, SMTP `SENT`/`ATTEMPTED`/`UNCERTAIN`,
원본 state와 복구 원본은 보존합니다. 새 source의 정상 baseline으로 이전 사건을
가짜 RECOVERED 처리하거나 예전 state를 복원하지 않습니다. 새 관측의 독립 확인과
발신 소유권 대조가 끝난 뒤에만 기존 timer·Discord source·SMTP의 향후 발신을
각각 명시적으로 퇴역시킵니다. 기존 파일의 삭제는 전환 요건이 아닙니다.

하나의 source를 새 collector SMTP와 Discord에 연결하는 결정과 서로 다른 source를
같은 장애 이름으로 중복 연결하는 문제를 구분합니다. 공개 서비스의 발신 소유자는
하나로 정하고 다른 위치의 동일 source를 추가 notifier에 중복 등록하지 않습니다.
재시도·불확실한 수락·복구 확인은 기존 collector와 Discord 정책을 따릅니다.

오프라인 검사는 실제 HTTPS·SMTP·Discord 요청을 보내지 않습니다.

```bash
python3 -B scripts/tests/test_staging_health.py
bash scripts/verify.sh
```
