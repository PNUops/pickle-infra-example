# 자체 서빙 LLM 정기 감시

이 패키지는 자체 서빙 호스트의 상태와 모델 조회, 정답이 정해진 실제 생성을 별도로
관측합니다. Python 표준 라이브러리와 systemd만 사용하며 서빙 설정과 방화벽, 모델,
학생 키를 변경하지 않습니다. 파일이 레포지토리에 있다는 사실은 운영 설치나 감시 개시를
뜻하지 않습니다. 다음 절차에서 설치 호스트와 코드 hash, 활성 timer와 첫 자연 실행을
확인한 뒤 해당 환경의 배치로 기록합니다.

## 점검 범위와 제한

| 점검 | 위치와 주기 | 판정 |
|---|---|---|
| `host-query`의 host | 서빙 호스트, 1분 | boot ID, 서비스 기동 경과, MemAvailable, GPU 온도/사용률. 부팅 또는 서비스 시작 뒤 10분은 LOADING |
| `host-query`의 query | 서빙 호스트, 1분 | 인증된 `/health`와 `/v1/models`, 예상 모델 일치. 실제 생성 정상과 별도 |
| `local-generation` | 서빙 호스트, 15분 | 고정 산술 질문, 정답 `4`, 정상 종료와 양수 입력/출력 토큰, 최대 출력 8토큰 |
| `upstream-generation` | 실제 gateway가 있는 컨테이너, 5분 | 같은 자체 서빙 엔드포인트를 직접 호출. gateway의 학생 인증·인가·사용량 적재는 통과하지 않음 |
| collector | 결과가 있는 호스트, 1분 | 허용한 로컬 결과의 신선도와 상태 전이. 원격 SSH 수집과 외부 감시는 별도 배치 |

생성은 thinking off, temperature 0, 비스트리밍이며 DNS를 쓰지 않는 명시적 내부 IP의
HTTP 자체 서빙 주소만 받습니다. 상용 호스트명과 공개 IP, URL 내 인증값, redirect는
허용하지 않습니다. 연결은 3초, 전체 HTTP 작업은 별도 자식 프로세스의 20초 상한으로
제한합니다. 자식 timeout은 클라이언트 연결을 닫습니다. 서버가 취소 요청을 즉시 처리한다는
보장은 없으므로 5분/15분 간격과 동시 실행 lock을 함께 유지합니다.

생성 실행기는 HTTP 시도 전에 원자적으로 사용 횟수를 저장합니다. UTC 일자별 로컬
120회/업스트림 312회, 최소 간격 각각 900초/300초이며 실패와 프로세스 중단도 횟수에
포함됩니다. 상태와 counter를 지우거나 과거 파일로 되돌려 한도를 우회하지 않습니다.
중복 실행과 한도 생략은 BUDGET_LIMIT으로 stdout에 남기고 마지막 실제 결과를 덮어쓰지
않습니다. 학생 사용량과 유료 모델 호출은 이 직접 경로에 없습니다.

GPU 사용률이 높거나 생성 counter가 조용하다는 사실만으로 장애를 선언하지 않습니다.
지원되는 vLLM `/metrics`의 알려진 숫자만 보존하고, boot ID 변경이나 counter 감소는
reset으로 구분합니다. 이 delta는 동시 사용자 요청을 포함하므로 probe 토큰 수가 아닙니다.
optional metrics는 health/models와 별도 자식 프로세스에서 최대 3초간 조회합니다. metrics가
멈추거나 조회되지 않아도 이미 성공한 health/models는 유지하고 metrics만 OBSERVER_ERROR로
구분합니다. host/query service의 전체 30초 예산 안에서 이 추가 상한을 포함합니다.
unified memory 장비에서 GPU memory가 N/A면 UNSUPPORTED로 남깁니다. host memory는
`/proc/meminfo`에서 별도로 읽습니다. 지원하지 않는 지표를 0으로 바꾸지 않습니다.

공개 gateway의 인증·인가·통계 경로는 이 패키지가 검증하지 않습니다. 그 경로는
[외부 HTTPS 감시](llm-health-external.md)의 공개 생성 canary 가 전용 비학생 진단 key 로
봅니다. 그 canary 는 route 를 가리지 않으므로(유료 upstream 포함) 자체 서빙 생성 정상의
증거로 읽지 않습니다. 학생 키로 대신하지 않습니다. 외부 모델 fallback과
서비스 자동 재시작·호스트 재부팅도 이 패키지의 동작이 아닙니다.

## 설치 전 검증

1. 실제 배치의 서빙 이름/IP와 gateway 컨테이너를 읽기 확인합니다. 서빙 호스트에서
   generation과 query를, gateway 컨테이너에서 upstream generation을 선택합니다.
   한 호스트에 서로 다른 환경의 설정을 혼합하지 않습니다.
2. `python3 -B scripts/tests/test_llm_health.py`와 `bash scripts/verify.sh`를 실행합니다.
   테스트는 loopback fixture만 열며 운영 모델과 SMTP를 호출하지 않습니다.
3. 대상 Linux에서 `systemd-analyze verify hosts/llm-monitor/systemd/*`로 unit을 검사합니다.
   필요한 Python3, systemd credential과 `nvidia-smi`는 기존 설치를 확인하며 버전 변경은
   이 절차에 포함하지 않습니다.
4. 설치 대상 파일이 이미 있으면 새 이름의 보호 backup에 복사하고 기존 state는 보존합니다.
   파일 hash와 uid/mode, 기존 timer 상태를 변경 전에 기록합니다.

## 통지 없는 초기 배치

1. `scripts/llm-health-probe.py`, `scripts/service-health-monitor.py`를 root 소유 0644로
   `/usr/local/libexec/pickle/`에 설치합니다. 공통 모듈은 같은 디렉터리 아래
   `lib/llm_health.py`에 root 소유 0644로 설치합니다. 설치 경로의 부모는 root 소유이며
   group/other 쓰기 권한이 없어야 합니다.
2. `/etc/pickle-llm-health/`를 root 0700으로 만들고 선택한 example JSON을 실제 환경에
   맞춰 root 0600으로 둡니다. 예시 주소/모델을 그대로 활성화하지 않습니다.
3. 기존 upstream credential에서 필요한 **토큰 한 값만** root 경계 안에서
   `/etc/pickle-llm-health/upstream-token`의 root 0600 파일로 분리합니다. 전체 gateway
   env를 복사하거나 shell trace, argv, journal, 결과 파일로 토큰을 출력하지 않습니다.
   유닛은 LoadCredential로 서비스별 읽기 전용 사본을 전달합니다. 토큰 회전은 원본과
   파생 파일을 함께 갱신하고 원본 credential을 이 감시 작업에서 교체하지 않습니다.
4. 선택한 unit만 `/etc/systemd/system/`에 root 소유 0644로 설치합니다. DGX에서는
   host-query/local-generation과 collector, gateway 컨테이너에서는 upstream-generation을
   선택합니다. collector example은 DGX 로컬 결과만 포함합니다. CT 결과를 합치려면
   별도로 검토한 고정 명령의 읽기 전용 전송과 신선도 검증을 먼저 설치합니다.
5. collector 설정의 `notifications_enabled`를 **false**로 유지합니다. SMTP 설정을
   설치하지 않아도 관측이 가능하며, 이 모드에서는 SMTP 설정을 읽지 않습니다.
   `--dry-run`은 기존 결과를 읽어 stdout만 만들며 state/lock/incident/SMTP를 쓰지 않습니다.
   24~48시간 관찰은 통지 off의 일반 collector로 진행해야 지속된 시간 창을 검증할 수 있습니다.
6. `systemctl daemon-reload` 후 선택한 timer만 enable/start합니다. 이는 서빙 서비스의
   restart가 아닙니다. 기존 백업/네트워크 unit과 timer는 변경하지 않습니다. 첫 자연 실행의
   result/summary에서 정답, 지연, 토큰, 관측 시각과 24~48시간 부하를 확인합니다.

결과와 budget 디렉터리는 systemd StateDirectory가 root 0700으로 만듭니다. 각 결과와
state는 0600이며 고정 크기의 최신 JSON만 보존합니다. 본문과 응답 원문, credential은
결과에 없고 journal에도 정형 상태·지연·토큰만 남습니다. journal 보존은 호스트의 기존
용량/기간 정책을 따르며 운영자 조사는 해당 기간 안에서 수행합니다.

## 최초 경보와 안정 복구

collector는 실제 새 생성 결과 두 번, 조회/host 결과 세 번 실패할 때 해당 층의 incident를
엽니다. 같은 timestamp를 반복 읽어 실패 횟수를 늘리지 않습니다. 실제 서비스 실패와
결과 수집/권한/파싱/지연의 OBSERVER_ERROR는 별도 incident입니다. 1분 결과는 3분,
5분 결과는 12분, 15분 결과는 35분의 신선도 상한을 사용합니다.

장애 메일은 사건 최초 1회입니다. 복구는 신선한 정상 결과가 2개 이상이고 첫 정상부터
60분간 유지된 뒤 1회입니다. 중간 실패·LOADING·UNKNOWN·신선도 공백은 안정 창을
초기화하고 같은 incident를 유지합니다. 닫힌 incident 이후 새 확정 실패는 새 사건입니다.
통지 off에서 이미 열린 incident는 통지 활성화 시 현재 상태를 한 번 보냅니다.
이때 과거 사건의 전송 기록과 현재 incident 번호를 대조합니다. 과거 장애 메일이 SENT여도
통지 off 중 새로 열린 사건의 최초 통지를 생략하지 않습니다. 복구가 확정되면 미전송
NEW/NOT_SENT 장애 통지를 취소해 뒤늦은 과거 장애와 복구를 함께 보내지 않습니다.

현재 `config.sources`에 등록된 대상만 발송 범위입니다. 제거된 대상의 과거 incident와 delivery는
보존하되 발송하지 않습니다. 여러 collector를 쓰면 서비스/TLS/생성 통지와 감시자 통지의
담당을 분리합니다. 원격 source에는 `observer_parent`로 같은 설정의 importer source 한 개를
명시할 수 있습니다. [외부 수집 예시](../hosts/llm-monitor/collector-import.example.json)의 부모는
`expected_kind=observer-report`이며 `kind`, 상태와 importer의 고정 reason을 확인합니다.
부모 누락·self 참조·순환·다단계 관계는 설정 오류입니다. 관계가 없는 기존 설정은 그대로 동작합니다.

collector는 모든 결과 경로를 한 번 읽은 뒤 고정한 snapshot으로 판단합니다. 공통 importer가
현재 OBSERVER_ERROR이거나 결과가 누락·만료·유효하지 않을 때, 그 부모에 연결된 **누락되거나
만료된 child**의 observer 실패를 UNKNOWN으로 묶습니다. 공통 parent의 observer incident만
원래 3회 기준으로 열고, child의 실패 횟수와 안정 복구 창은 초기화합니다. 신선한 서비스 FAILED,
TLS 보안 FAILED, 개별 probe의 명시적 OBSERVER_ERROR와 잘못된 child 문서는 억제하지 않습니다.
parent가 현재 HEALTHY이면 과거 incident의 안정 복구 중이어도 child를 억제하지 않습니다.

summary는 UNKNOWN의 원본 epoch와 reason, `suppressed_by`, `parent_status`를 보존합니다.
억제 중 기존 child incident·sequence를 지우거나 새 번호를 만들지 않으며, child observer의
NEW/NOT_SENT 장애 통지 예약·재전송은 보류합니다. 부모가 돌아온 뒤 child 자체가 계속 없으면
기존 observer 판정을 재개합니다. 불확실한 구간에는 NEW/NOT_SENT 복구 통지를 취소하고
ATTEMPTED/SENT/UNCERTAIN 기록은 보존합니다. 억제로 새 child incident가 열리지 않았으면
그 child의 복구 통지도 생기지 않습니다. 실제 배치 설정을 바꾼 뒤 bundle 전체 단절·개별 child
단절·신선한 TLS 실패 fixture와 자연 실행을 확인하고 초기 관찰이 끝난 뒤 통지를 켭니다.

수신자와 제목의 대상 이름을 검토하고 초기 관찰이 끝난 뒤 `notifications_enabled: true`와
root 0600 `mail_config` 경로를 설정합니다. SMTP JSON은 기존 백업 메일의 TLS 설정 형식
(`host`, `port`, `tls_mode`, `username`, `sender`, `recipient`, `password_file`)을 재사용할
수 있습니다. 비밀번호는 JSON에 넣지 않고 별도 root 0600 파일로 둡니다. 시험 메일은
명시적으로 승인된 경우에만 보냅니다. SMTP 수락은 inbox 도착의 증거가 아닙니다.

메일 전에 ATTEMPTED를 원자 저장합니다. 새 실패·LOADING·UNKNOWN이 관측되면 이전 사건의 NEW/NOT_SENT
복구 통지는 SUPERSEDED로 막아 현재 장애 중 과거 복구 메일이 나가지 않게 합니다.
이미 수락되거나 불확실한 ATTEMPTED/SENT/UNCERTAIN 기록은 그대로 보존합니다.
수락 여부 불확실/중단 ATTEMPTED는 자동
재전송하지 않습니다. 명확히 미수락한 경우만 1분/5분 뒤, 총 3회까지 재시도합니다.
감시자나 collector 자체가 완전히 중단되면 자기 메일을 보낼 수 없습니다. 독립 외부
heartbeat가 배치되기 전 이를 교차 감시 완료로 기록하지 않습니다.

## 원복

1. 새 timer만 disable/stop하고 대응하는 oneshot이 종료됐는지 확인합니다. 실행 중 강제
   kill로 서빙을 변경하지 않습니다. 서빙 unit은 원복 대상이 아닙니다.
2. 검증한 이전 probe/collector/unit/config를 복원한 뒤 daemon-reload합니다. 이전 판이
   새 state를 읽을 수 없으면 timer를 꺼 둔 채 호환 검토를 받습니다.
3. budget과 incident/sequence/delivery state를 과거 값으로 되감지 않습니다. UNCERTAIN과
   ATTEMPTED는 수신 기록 대조 전 재전송하지 않습니다. 폐기할 파생 credential만 회수하고
   기존 upstream 원본과 학생 키, 백업 복구점은 보존합니다.
