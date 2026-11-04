# pve-node-3 코어 PBS 백업 독립 감시

이 감시는 pve-node-3에서 PBS 읽기 전용 스토리지 `pbs-example-core-read`의 실제 목록을
조회해요. 백업 작업의 종료 코드나 로컬 상태 파일을 정상 근거로 사용하지 않아요.
일일 대상은 CT 1200, 1201, 1202, 1203, 1204예요. 기존 수동 복구점 네 개가 `protected=1`로
그대로 남아 있는지도 별도로 확인해요. 일일 복구점은 `protected` 값과 관계없이
형식 `pbs-ct`/`lxc`, CTID 경로, volid의 UTC 시각과 `ctime` 일치,
암호화 키 fingerprint, 양수 크기를 검사해요.
보호 baseline은 기존 CT1200/1201/1202/1204의 네 개예요. CT1203의 새 수동 pin을 요구하지 않아요.
이 감시는 backup job의 멤버나 설정 pin, 복구점을 변경하지 않아요.

## 판정과 알림

일일 작업은 2025-01-02부터 03:17 KST에 시작해요. 첫 시행 전인
2025-01-01에는 기존 수동 복구점 네 개가 정상이면 `BASELINE`을 기록해요.
첫 시행일 이후 04:30 KST 전에는 오늘의 새 복구점이 없거나 일부만 있어도
`PENDING`으로 기다리고, 그 시각부터는 다섯 CT 모두에 오늘 03:17 이후
생성된 복구점이 있어야 정상이에요. 첫 실행이 2025-01-02 04:30 이후더라도
이 기준을 소급 적용해요. 목록 조회 실패와 수동 복구점 소실, 잘못된 메타데이터,
기한을 넘긴 일일 복구점은 `FAILED`로 판정해요. 상태 기록이 없는 최초 정상 판정은 `BASELINE`으로
기록하고 메일을 보내지 않아요. 이후 정상→실패에 `FAILED`, 실패→정상에
`RECOVERED` 메일을 한 번씩 보내요. 같은 상태의 반복 실행은 다시 보내지 않아요.
용량 증가에 맞춰 마감 시각을 바꿔야 하면 `--deadline HH:MM`으로 KST 시각을
명시해요. 기본값은 `04:30`이에요. 첫 시행일은 유닛에
`--first-due-date 2025-01-02`로 고정해요. 배치 시각에 맞춰 이 날짜를
뒤로 미루면 실제 누락을 가리므로 변경 전에 운영 판단이 필요해요.

SMTP는 이미 pve-node-3에 설치된 `/etc/pickle-example/mail.json`의 `host`, `port`,
`tls_mode`, `username`, `sender`, `recipient`, `password_file`을 사용해요.
수신자는 배치 전에 운영자가 지정한 `<operator-email>`과 일치해야 해요. 별도의 자격증명 사본은 만들지 않아요.
발송 전에 별도 상태 디렉터리에 `ATTEMPTED`와 시도 횟수를 기록해요.
설정 파일 부재, SMTP 연결 실패, 인증 실패, 명시적 메시지 거부 중 하나로 미수락이 확실하면 `NOT_SENT`로 기록해요.
첫 실패 뒤 1분, 두 번째 실패 뒤 5분이 지난 후 같은 상태를 다시 검사할 때 재시도해요.
총 3회 실패하면 `RETRIES_EXHAUSTED`로 남기고 자동 재시도를 멈춰요.
타이머가 5분 간격이므로 실제 재시도는 다음 타이머 실행 시점에 이뤄져요.

DATA 제출 이후 연결이 끊겨 수락 여부를 모르면 `UNCERTAIN`으로 기록해요.
프로세스가 발송 도중 중단돼 `ATTEMPTED`만 남아도 자동 재발송하지 않아요.
이 두 상태와 `RETRIES_EXHAUSTED`에서는 운영자가 SMTP 수락과 수신함을 확인한 뒤 조치해요.
`SENT`는 SMTP 수락이며 최종 수신함 도착의 증거는 아니에요.
상태 파일은 `/var/lib/pickle-core-pbs-monitor/state.json`이에요.

## 최신 관측과 사건 기록

`last_check_epoch`는 기존 소비자와의 호환을 위해 유지해요. `last_observation`은
PENDING 판정과 동일 상태 반복, PBS 조회 실패를 포함해 매 검사에서 갱신해요.

| 필드 | 의미 |
|---|---|
| `observed_at_epoch` | 이번 검사 시각 |
| `query_status` | PBS 목록 조회의 OK 또는 UNAVAILABLE. 백업 정상 판정과 별개 |
| `inventory_observed_at_epoch` | 현재 또는 마지막 성공 목록 조회 시각. 조회 실패로 갱신하지 않음 |
| `last_valid_points` | CT별 마지막 유효 archive의 ctime/volid와 이번 검사 시각 기준 age_seconds |
| `point_evidence` | 이번 목록의 OBSERVED, 이전 목록의 CACHED, 증거 없는 UNAVAILABLE |

STALE인 archive도 진단용 마지막 유효 시각과 나이를 남겨요. 보호된 수동 archive도 유효 archive에
포함되지만 오늘의 일일 복구점 요건을 대신하지 않아요. 기존 `latest`와 HEALTHY/FAILED 판정은
오늘의 일일 복구점 기준을 그대로 써요. 새 archive의 메타데이터가 잘못돼도 더 오래된 유효 archive의
진단 정보를 남기며, 잘못된 최신 archive 판정을 가리지 않아요.

PBS 조회가 실패하면 마지막 성공 목록을 CACHED로 명시하고 실제 시간 경과만큼 나이를 늘려요.
이전 목록 증거가 없으면 시각과 나이는 null이에요. 조회 실패를 나이 0이나 새 목록의 정상 확인으로 바꾸지 않아요.
`report`는 기존 알림과 재시도 기록이므로 최신 inventory로 해석하지 마세요.
새 소비자는 `last_observation`의 query와 시각을 함께 확인해요.

새 FAILED episode에서만 `incident.first_report`를 만들고 같은 사건에서는 바꾸지 않아요.
기존 상태의 과거 `report`는 `legacy_available_report`와 `LEGACY_AVAILABLE_REPORT` 출처로만 보존해요.
이를 첫 장애 보고라고 추정하지 마세요. 기존 FAILED 사건은 `LEGACY_ACTIVE_INCIDENT`로 표시하며
first_report를 만들지 않아요. 자연 상태 전이에서는 이전 알림의 전달 기록을 `previous_notification`에 보존해요.

추가 관측 schema는 `observation_schema_version=1`이에요. 이관만으로 sequence/delivery/attempts/retry_after를
초기화하거나 메일을 재전송하지 않아요. PENDING 관측도 기존 FAILED 사건을 복구로 닫지 않아요.
SENT와 UNCERTAIN, 중단된 ATTEMPTED, 재시도 소진 기록을 보존하고 SMTP 담당은 이 감시 하나로 유지해요.
다른 수집자가 같은 backup 사건의 메일을 다시 만들지 않아요.

## 설치 전 확인과 실행

아래 경로와 명령은 **pve-node-3**용이에요. 적용 전에 PBS reader 목록에서 네 수동 복구점과 fingerprint를 다시 확인해요.
암호나 `mail.json` 본문은 출력하지 마세요.
다음 명령은 검토한 이 예시 레포지토리 checkout에서 pve-node-3 운영자가 실행해요. 설치 전에
`timedatectl show -p Timezone -p NTPSynchronized`가 각각 `Asia/Seoul`, `yes`인지 확인해 주세요.
스크립트도 매 실행에서 이를 확인하고 이전 검사보다 시계가 뒤로 가면 중단해요.

```bash
install -D -o root -g root -m 0755 scripts/core-pbs-monitor.py \
  /usr/local/libexec/pickle/core-pbs-monitor.py
install -D -o root -g root -m 0644 hosts/pve-node-3/systemd/pickle-core-pbs-monitor.service \
  /etc/systemd/system/pickle-core-pbs-monitor.service
install -D -o root -g root -m 0644 hosts/pve-node-3/systemd/pickle-core-pbs-monitor.timer \
  /etc/systemd/system/pickle-core-pbs-monitor.timer
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/pickle-core-pbs-monitor.service \
  /etc/systemd/system/pickle-core-pbs-monitor.timer
systemctl start pickle-core-pbs-monitor.service
systemctl enable --now pickle-core-pbs-monitor.timer
systemctl list-timers pickle-core-pbs-monitor.timer
```

서비스는 5분마다 실행되고 한 번에 최대 120초 기다려요. 1일 첫 수동 실행은
상태 기록이 없는 경우 수동 복구점 네 개만 정상이면 메일 없이 `BASELINE`을 남겨요. 이후 상태는
`systemctl status pickle-core-pbs-monitor.service`와
`journalctl -u pickle-core-pbs-monitor.service`로 확인해요.
설치한 파일을 수정한 뒤에는 다시 설치하고 `systemctl daemon-reload`를 실행해요.

감시를 되돌릴 때는 `systemctl disable --now pickle-core-pbs-monitor.timer`로 다음 실행을 막고
진행 중인 service가 없는지 확인해요. 설치한 유닛과 스크립트는 경로와 해시로 이 작업의 소유인지 대조한 뒤에만 회수해요.
상태 파일은 장애와 메일 수락 판정의 증거예요. 먼저 보호 사본을 남기고 바로 초기화하지 마세요. pve-node-2
백업 job과 PBS storage, 네 보호 수동 복구점은 감시 회수와 별개로 유지해요.
관측 schema를 모르는 구 코드로 되돌리면 새 사건 기록이 유실될 수 있어요. state를 과거 값으로
덮어쓰지 말고 현재 schema와 호환되는 코드로 되돌리거나 timer를 멈춘 채 검토해 주세요.

실행 계정은 root예요. 상태와 lock은 `/var/lib/pickle-core-pbs-monitor/`에 root 소유 0700으로 둬요.
DB 백업의 상태나 lock과 공유하지 않아요. 정상은 종료 코드 0, 백업 검증 실패는 1,
스크립트나 메일 전달 오류는 2예요. `FAILED`인데 메일이 미발송이거나 불확실하면 종료 코드 2이며
상태 파일에 실패 판정과 전달 상태를 보존해요.

오프라인 회귀 검사는 다음 명령으로 진행해요. PBS나 SMTP에 접속하거나 실제 메일을 보내지 않아요.

```bash
python3 scripts/tests/test_core_pbs_monitor.py
```

예시 기준일: 2025-01-01

최종 갱신: 2026-11-04
