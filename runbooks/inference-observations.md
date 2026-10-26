# 전용 추론 관측 표본과 전달

이 패키지는 모델 호스트의 최신 관측 표본을 고정 SSH 명령으로 읽고, 별도 중계
호스트의 로컬 outbox에서 API로 전달하는 코드와 유닛 예시입니다. Linux, Python 3,
systemd와 읽기 가능한 `/proc`을 전제로 합니다. 모델 호스트의 GPU 표본은 설정한
`gpu_index` 한 개를 나타냅니다. 서비스 설치와 활성화, 키 발급, API source 허용,
네트워크나 신뢰 설정은 별도의 운영 승인 뒤에 수행합니다.

생성 요청이나 서빙 설정·재시작, 기존 health/query/generation 감시 실행은 하지 않습니다.
`sample`은 네트워크로 고정 loopback `/metrics`만 읽고 호스트 값은 로컬 읽기로 관측합니다.
기존 `read-health-v1`, health 결과와 budget, 알림 및 incident 파일도 사용하지 않습니다.

## 표본과 상태

`inference-observation.py sample`은 loopback의 고정 `/metrics`에 GET만 수행합니다.
관측 시각과 원래 모델/엔진 label을 유지한 누적 counter, gauge와 histogram을 순수
파서로 정규화합니다. Prometheus 원문이나 임의 label 값은 외부로 전달하지 않습니다.
선택하지 않은 family는 이름만 기록합니다. histogram 평균은 percentile이 아니며
관측 표본의 최댓값도 기간 전체의 순간 최댓값을 보장하지 않습니다.
허용된 secondary label의 빈 값과 줄바꿈은 그대로 보존합니다. DB에서 표현할 수 없는
U+0000은 명시적으로 거절하며 해당 수집 결과는 `SOURCE_FAILED`로 남깁니다.

호스트 값은 아래 키로 구분합니다. 미보고와 미지원, 수집 실패의 값은 `null`이고
실제 관측한 0과 다릅니다. 시스템 메모리와 GPU 메모리를 더하지 않습니다.

| 키 | 의미와 단위 |
|---|---|
| `cpu_load1` | `/proc/loadavg`의 1분 load average. CPU 사용률이 아닙니다 |
| `memory_total_bytes`, `memory_available_bytes` | `/proc/meminfo`의 시스템 메모리 bytes |
| `swap_used_bytes` | 관측된 SwapTotal − SwapFree bytes |
| `gpu_utilization_percent` | 설정한 GPU의 관측 사용률, 0~100 percent |
| `gpu_temperature_celsius` | GPU 온도, Celsius |
| `gpu_memory_total_bytes` | GPU가 보고한 메모리 bytes. 통합 메모리 장비의 미지원 값은 별도입니다 |
| `gpu_power_watts`, `gpu_power_limit_watts` | 보고된 전력과 전력 상한, watts |

producer epoch와 sequence는 전용 `state.json`에 보존합니다. 정상 재시작이나 부팅이
sequence를 되감지 않습니다. boot ID, 활성 서비스 invocation과 PID 시작 시각,
명시한 `configuration_id`는 별도 identity이며 알아내지 못한 값은 미보고입니다.
서빙 구성이 바뀌면 자격증명이나 환경 전체를 해시하지 않고 configuration ID를
갱신합니다. identity 변경과 counter 감소, 표본 공백은 새 baseline으로 처리하며
누락 구간을 0으로 채우지 않습니다.

timer는 1분 주기이고 producer는 UTC minute당 새 표본을 최대 한 개만 만듭니다.
최초 상태 생성은 현재 minute를 예약해 다음 minute부터 수집합니다. 같은 minute의
재호출은 GET이나 새 sequence를 만들지 않습니다. 최신 `latest.json`은 원자적으로
교체합니다. SSH polling 사이에 놓친 중간 표본은 복구할 수 없으며 공백으로 남습니다.
모델 호스트에 무한 backlog나 원격 삭제 권한을 만들지 않습니다.

## 고정 SSH 읽기

`read` 모드는 `SSH_ORIGINAL_COMMAND=read-inference-observation-v1`만 허용합니다.
고정 설정과 StateDirectory의 검증된 최신 표본만 읽고 파일이나 checkpoint를 쓰지
않습니다. source UUID와 호스트명이 다르거나 상태가 없으면 고정 reason으로 거절합니다.

전용 reader key를 준비한 뒤 승인된 모델 호스트에 아래 형태의 제한을 설치합니다.
실제 공개키는 이 예시에 넣지 않습니다. 기존 관리 키나 health reader key를 복사하거나
교체하지 않습니다.

```text
restrict,command="/usr/bin/python3 -I -B /usr/local/libexec/pickle/inference-observation.py read --config /etc/pickle-inference-observation/reader.json --state-dir /var/lib/pickle-inference-observation" ssh-ed25519 <separate-observation-reader-public-key>
```

중계 호스트는 명시한 내부 IP와 전용 key/known_hosts만 사용합니다. ambient SSH
설정과 agent, forwarding, proxy, connection multiplexing은 사용하지 않습니다.
연결과 전체 실행 시간, 응답 bytes에 각각 상한이 있습니다.

## 불변 outbox와 API ACK

`report`는 producer 원문에 최초 `reporterReceivedAt`을 붙인 HTTP body bytes를 로컬
outbox에 한 번 저장합니다. 이후 retry는 그 bytes를 그대로 보냅니다. producer
`observedAt`, 중계 수신 시각과 API `receivedAt`은 서로 다른 시각입니다.

API 경로는 `POST /internal/inference-observations`이고 HTTPS와 기본 인증서 검증이
필수입니다. redirect는 따르지 않습니다. API source/peer/bearer 설정은 별도로 등록하고
활성화하며 LLM 사용자 키나 서빙 key를 API reporter 인증에 사용하지 않습니다.
한 요청은 한 표본이고 전체 body는 512 KiB 이하입니다. 모델/엔진 32그룹, 선택 series
2,048개, histogram 경계 128개, label 256 UTF-8 bytes와 안전한 JSON 정수 상한을 지킵니다.

같은 source/epoch/sequence와 같은 body bytes의 retry만 duplicate입니다. ACK는
`ACCEPTED` 또는 `DUPLICATE`, 같은 source/epoch/sequence 및 보낸 bytes의 SHA256이
모두 맞아야 합니다. ACK를 받은 뒤 checkpoint를 atomic write와 fsync로 먼저 보존하고,
그다음 같은 불변 파일의 hash를 재확인해 unlink와 directory fsync를 수행합니다.
checkpoint 직전 또는 직후 중단돼도 미확인 표본을 새 시각으로 재작성하지 않습니다.

409 충돌과 422 만료/거절은 파일을 지우지 않는 bounded 격리 metadata로 남깁니다.
같은 표본을 계속 POST하지 않으며 다른 정상 pending은 처리할 수 있습니다. 격리 파일도
로컬 행수/bytes 상한을 사용합니다. 상한에 닿으면 새 관측 큐 쓰기만 실패하며 원문을
자동으로 버리지 않습니다. 전달 실패와 수집 공백은 기존 serving/incident 상태를
바꾸지 않고 이 컴포넌트의 `status.json`과 journal에만 나타납니다. 로그에는 token이나
표본 본문, HTTP 오류 본문을 기록하지 않습니다.

DB의 90일 표본 보존은 로컬 큐의 무손실·무기한 보관 보장이 아닙니다. 장기 실패의
old replay 거절, 충돌, 디스크 상한 또는 state 유실은 운영자 조치가 필요합니다.
먼저 전용 timer를 멈추고 대상 key/hash와 원문을 보호해 원인을 확인합니다. 명시적인
처분 승인 전에는 pending/state를 삭제하거나 reporterReceivedAt을 고쳐 재전송하지
않습니다. 이 도구는 격리 해제, 대량 삭제나 과거 checkpoint 복원을 자동 수행하지 않습니다.

## 설정과 설치 경계

예시 설정은 `hosts/llm-monitor/inference-observation-*.example.json`이며 source UUID,
호스트 binding과 각 경로를 실제 승인 값으로 바꿉니다. 설정과 key/pin 파일은 소유자의
private regular file이고 디렉터리는 0700, 상태와 표본은 0600입니다. symlink와 hardlink,
다른 소유자나 노출된 권한은 거절합니다. 기존 latest/pending이 있는데 state가 사라지면
자동 초기화하지 않습니다.

승인된 설치는 `inference-observation.py`와 `scripts/lib/`의
`inference_observation.py`, `inference_metrics.py`, `observation_io.py`,
`observation_runtime.py`, `observation_spool.py`를 함께 배치합니다. 예시 systemd unit은
sampler와 reporter를 별도 StateDirectory와 timeout으로 실행합니다. 기존 유닛에
의존성이나 restart를 추가하지 않습니다. 모델 endpoint에 인증이 필요하면 선택한
credential drop-in과 `metrics_credential_file`을 함께 연결하며 값을 source에 넣지 않습니다.
API의 실제 HTTPS 경로와 peer binding, 신뢰 루트의 수용은 설치 승인 때 따로 확인합니다.

실제 설치 전에는 아래 offline fixture만 실행합니다. HTTP와 SSH 결과는 mock이며
실제 GPU나 서비스에 접속하지 않습니다. native process-group fixture는 Linux에서만
실행하고 다른 개발 환경에서는 그 한계를 별도로 기록합니다.

```bash
python3 -B scripts/tests/test_observation_spool.py
python3 -B scripts/tests/test_inference_observations.py
python3 -B scripts/tests/test_observation_io.py
bash scripts/verify.sh
```

원복은 새 timer/service만 멈추고 원문과 checkpoint를 보존합니다. 서빙 서비스나 기존
health/import/알림 상태를 재시작하거나 과거 상태로 되감지 않습니다.
