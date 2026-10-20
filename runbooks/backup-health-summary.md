# 백업 근거와 신선도 요약

`backup-health-summary.py`는 독립 감시 호스트에서 기존 core 상태와 DB 정상 조회 캐시,
DB monitor의 native systemd 실행 결과를 읽습니다. 백업·PBS 조회·HTTP·SMTP를 실행하지
않고 기존 감시 파일과 알림 상태를 변경하지 않습니다. Python 표준 라이브러리와 기존
`lib/llm_health.py`의 보호 파일 읽기·lock·원자 저장을 사용합니다.

결과는 `/var/lib/pickle-backup-health-summary/result.json`의 root 0600 파일입니다.
상위 디렉터리는 root 0700이며 요약 자신의 lock만 함께 둡니다. top-level은
`kind=backup-freshness-summary`, `schema_version=1`, `node`, `observed_at_epoch`,
`clock_status`, `notifications_enabled=false`와 core/database 두 행입니다. 전체 백업을
HEALTHY나 FAILED라고 선언하는 top-level status는 없습니다.

## Core의 현재 판정과 복구점 나이

고정 입력 `/var/lib/pickle-core-pbs-monitor/state.json`에서 관측 schema 1의
`last_observation`만 현재 근거로 사용합니다. 기존 top-level status/report와 최초 사건
보고는 최신 inventory가 아닙니다. 이전 source처럼 새 관측 schema가 없으면 UNKNOWN입니다.

관측이 12분 이내이고 목록 조회가 OK일 때만 native의 HEALTHY/FAILED/PENDING을
현재 행에 표시합니다. 오래되거나 query가 UNAVAILABLE이면 current_verdict는 UNKNOWN이고
원본 관측 시각·목록 조회 시각·native 판정은 별도로 남깁니다. CT1200~1204의 진단 행이
하나라도 빠지거나 시각이 잘못되면 현재 정상을 추론하지 않습니다.

각 last_valid_points의 `ctime`은 원래 값이고 `current_age_seconds`는 요약의 현재 시각에서
계산합니다. 조회 실패의 CACHED evidence는 원래 inventory_observed_at_epoch를 유지합니다.
UNKNOWN 나이를 0으로 채우지 않으며, 오래된 복구점과 오래된 관측을 구분합니다.

## DB 캐시와 native 실행의 결합

고정 입력은 `/var/lib/pickle-db-monitor-production/last-remote-check.json`입니다.
이 파일은 native monitor가 정상일 때만 갱신하는 과거 증거입니다. query as-of는
`recovery_point_epoch + age_seconds`로 복원하고, 마지막 정상 복구점/파일 복원 시각,
snapshot과 현재 나이를 `last_healthy_evidence`에 별도로 표시합니다. cache의 mtime과
`notification.json`을 heartbeat로 사용하지 않습니다. cache가 사라지면 앞서 검증한 요약의
정상 증거를 PREVIOUS_SUMMARY_HEALTHY_CACHE로 보존할 수 있지만 현재 판정은 UNKNOWN입니다.

`systemctl show pickle-db-pbs-production-monitor.service`의 고정 일곱 속성을 최대 3초간
읽습니다. 현재 HEALTHY는 다음 조건이 모두 성립한 경우뿐입니다.

- inactive/dead, Result=success, ExecMainStatus=0인 종료된 invocation입니다.
- monotonic 시작·종료 시각이 현재 boot의 시간 범위 안에 있고 종료와 캐시 as-of가 3분 이내입니다.
- 캐시 as-of가 그 invocation의 시작~종료 범위에 들어갑니다. native 캐시의 정수 초와
  monotonic→wall 변환 오차 때문에 양쪽 경계에 최대 2초만 허용합니다.

뒤의 WARNING/오류, 새 검사 실행 중, 과거 invocation의 캐시, 종료 결과만 정상인 경우는
UNKNOWN입니다. native InvocationID와 원래 monotonic timestamp, 변환한 wall timestamp를
별도로 보존합니다. 캐시가 존재한다는 이유만으로 현재 PBS 확인 성공을 표시하지 않습니다.
시계가 수집 도중 뒤로 움직이거나 wall/monotonic 진행이 어긋나면 두 현재 판정을 UNKNOWN으로
둡니다. 이는 외부 NTP 동기화를 새로 검증했다는 뜻은 아닙니다.

## 설치와 검증

설치 파일이 레포지토리에 있다는 사실은 운영 배치를 뜻하지 않습니다. 실제 호스트의
hostname과 두 고정 입력 경로, native unit을 먼저 확인합니다. 새 config는 root 0600,
부모 `/etc/pickle-backup-health-summary`는 root 0700으로 두고 example의 호스트 이름을
실제 선택한 호스트로 바꿉니다. 다른 파일이나 명령을 config에 넣으면 거부합니다.

실행 파일은 `/usr/local/libexec/pickle/backup-health-summary.py`에 root 0644로 둡니다.
공통 모듈은 같은 부모의 `lib/llm_health.py`이며 root 소유와 group/other 쓰기 금지를
확인합니다. unit은 Python interpreter를 명시하므로 파일의 실행 비트에 의존하지 않습니다.
기존 core/DB monitor source와 unit은 이 요약을 설치하면서 교체하지 않습니다.

새 unit은 PrivateNetwork와 AF_UNIX만 허용하고 native 디렉터리를 읽기 전용으로 둡니다.
자신의 StateDirectory만 쓰며 MemoryMax 48M, CPUQuota 10%, TasksMax 8, 전체 10초로
제한합니다. 입력이 아직 없으면 요약의 UNKNOWN으로 표시하도록 선택적 ReadOnlyPaths를 씁니다.
timer는 1분 주기, AccuracySec 1초, Persistent=false입니다.

```bash
python3 -B scripts/tests/test_backup_health_summary.py
bash scripts/verify.sh
systemd-analyze verify hosts/production/systemd/pickle-backup-health-summary.service \
  hosts/production/systemd/pickle-backup-health-summary.timer
```

대상 Linux에서 unit 검증과 보호 파일 hash/권한을 확인한 뒤 새 timer만 배치합니다.
다음 자연 실행의 원본 시각과 현재 나이를 native 입력·종료 invocation과 대조합니다.
요약 자체의 종료 코드 0은 계산/저장이 끝났다는 뜻이며 백업 정상 신호가 아닙니다.
이 결과를 generic collector의 FAILED source나 별도 Discord/메일 producer로 등록하지 않습니다.
기존 native 감시의 SMTP 담당과 delivery state는 그대로 유지합니다.

되돌릴 때에는 새 summary timer만 stop/disable하고 oneshot 종료를 확인합니다. 자신의
코드/unit/config와 result를 소유 hash로 대조해 회수하며 native 감시·캐시·state·백업점은
원복 대상으로 삼지 않습니다. 요약을 지운 뒤 백업이 정상이라고 표시하지 않습니다.
