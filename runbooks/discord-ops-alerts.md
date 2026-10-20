# 호스트 운영 알림의 Discord 전송

각 호스트의 root 계정에서 실행합니다. Python 3 표준 라이브러리를 사용합니다.
기존 감시의 판정 파일과 systemd 실행 결과를 읽고 Discord 전용 대기열을 기록합니다.
SMTP 설정이나 메일 전달 상태, 감시 프로그램의 파일은 변경하지 않습니다.

## 설정과 감시 범위

`hosts/production/discord-alerts.example.json`이 설정 형식입니다. 실제 설정은
`/etc/pickle-discord-alerts/config.json`, webhook은 같은 디렉터리의 `webhook` 파일에
보관합니다. 두 파일은 root 소유 `0600`, 상위 디렉터리는 root 소유 `0700`입니다.
webhook 파일에는 URL 한 줄만 기록합니다. URL은
`https://discord.com/api/webhooks/<숫자 ID>/<토큰>` 형태만 허용합니다.
다른 호스트, 포트, query, fragment와 HTTP 주소는 거부합니다.

| 설정 | 의미 |
|---|---|
| `expected_hostname` | `hostname`의 첫 구간과 일치하는 실행 호스트 |
| `host_label` | Discord에 표시할 한글 이름. 숫자, 공백, 괄호와 하이픈 사용 가능 |
| `webhook_file` | root 전용 webhook 파일의 절대 경로 |
| `state_dir` | 전용 상태 디렉터리. 기본 유닛은 `/var/lib/pickle-discord-alerts` 사용 |
| `sources[].name` | 호스트 안에서 고유한 소문자 영문 식별자 |
| `sources[].label` | Discord에 표시할 한글 감시 이름 |
| `sources[].type` | 아래 표의 수집 방식 |
| `sources[].path` | 입력 파일 절대 경로 또는 systemd service 이름 |
| `sources[].max_age` | 갱신 지연을 알릴 시간(초). 30초 이상, 7일 이하 |

| 유형 | 읽는 정보와 판정 |
|---|---|
| `cron_markers` | `path.ok`와 `path.fail`을 읽습니다. 실패 마커가 있으면 장애이며 마지막 마커 수정 시각으로 갱신을 판단합니다. 로그 본문은 전송하지 않습니다. |
| `monitor_state` | JSON의 `status` 또는 `level`과 `last_check_epoch`를 읽습니다. `OK`, `HEALTHY`는 정상이고 경고, 실패와 오류는 장애로 알립니다. 기존 sequence는 복제하지 않습니다. |
| `db_cache` | 마지막 정상 PBS 조회 캐시의 `recovery_point_epoch`, `last_readback_epoch`와 파일 수정 시각을 확인합니다. 복구점이 제한보다 오래되면 정상 확인 지연, 캐시 갱신이 늦으면 결과 갱신 지연으로 알립니다. |
| `systemd` | service의 `Result`, `ExecMainStatus`와 monotonic 시작·종료 시각을 조회합니다. 실패 종료와 실행 중단을 별도로 판정합니다. 한 설정에 네 개까지 사용합니다. |

운영 서비스가 있는 호스트는 `production-health` 마커를 선택합니다.
독립 감시 호스트는 코어 PBS, 용량과 공개 경로 감시 상태 파일을 선택합니다.
2026-10-20 첫 적용에서 선택하는 범위는 다음과 같습니다.

| 호스트 | 유형 | 입력 | 갱신 제한(초) |
|---|---|---|---|
| pve-node-2 | `cron_markers` | `/var/lib/pickle-ops/production-health` | 1500 |
| pve-node-3 | `monitor_state` | `/var/lib/pickle-core-pbs-monitor/state.json` | 720 |
| pve-node-3 | `monitor_state` | `/var/lib/pickle-pbs-capacity-monitor/state.json` | 720 |
| pve-node-3 | `monitor_state` | `/var/lib/pickle-staging-cert-monitor/state.json` | 2100 |
| pve-node-3 | `db_cache` | `/var/lib/pickle-db-monitor-production/last-remote-check.json` | 600 |

`systemd` 유형은 선택 사항입니다. 기존 감시의 비정상 종료 코드가 실제 장애 판정과
겹치면 같은 사건을 두 번 알릴 수 있으므로 첫 적용에서는 선택하지 않습니다.
기존 감시가 중단되면 입력의 갱신 시각 지연으로 알립니다.
DB의 `last-remote-check.json`은 **정상 판정 때만**
갱신됩니다. 오래된 캐시는 현재 백업 장애의 원인이나 감시 프로세스 중단을 증명하지
않습니다. 지연 알림을 받은 뒤 DB monitor service의 실제 실행 결과를 확인합니다.

첫 정상 상태는 기준으로만 기록합니다. 첫 비정상 상태와 장애/정상 전이,
결과 갱신 지연/복구를 알립니다. 입력을 읽을 수 없으면 갱신 지연을 알리고,
이를 정상 복구로 처리하지 않습니다. 반복 상태는 전송하지 않습니다.
파일 폴링 사이에 발생하고 끝난 짧은 전이는 놓칠 수 있습니다.

## 설치와 무발송 확인

서버와 Discord 채널, 접근 권한을 확인한 뒤 webhook을 생성합니다. URL을 argv,
일반 로그, 터미널 출력 또는 Git에 넣지 않습니다. 보호된 전달 경로로 webhook
파일을 배치하고 별도 보호 설정 파일을 작성합니다. 예제의 호스트 이름과 감시 대상을
그대로 운영 설정으로 사용하지 않습니다.

검토한 체크아웃에서 실행 파일과 유닛을 설치합니다.

```bash
install -D -o root -g root -m 0755 scripts/discord-ops-alerts.py \
  /usr/local/libexec/pickle/discord-ops-alerts.py
install -D -o root -g root -m 0644 hosts/production/systemd/pickle-discord-ops-alerts.service \
  /etc/systemd/system/pickle-discord-ops-alerts.service
install -D -o root -g root -m 0644 hosts/production/systemd/pickle-discord-ops-alerts.timer \
  /etc/systemd/system/pickle-discord-ops-alerts.timer
python3 -I -B /usr/local/libexec/pickle/discord-ops-alerts.py \
  --config /etc/pickle-discord-alerts/config.json --inspect
systemd-analyze verify /etc/systemd/system/pickle-discord-ops-alerts.service \
  /etc/systemd/system/pickle-discord-ops-alerts.timer
```

`--inspect`와 `--dry-run`은 입력 분류만 출력합니다. webhook을 읽거나 Discord에
연결하지 않고 전용 상태 파일도 작성하지 않습니다. 실행 모드를 생략해도 같은
검사를 수행합니다. 비공개 입력의 원문, URL과 report는 출력하지 않습니다.

## 연결 시험과 활성화

`--test-message`는 「운영 알림 시험」으로 표시한 한 건을 대기열에 기록하고
Discord에 제출합니다. 같은 전용 상태 디렉터리에서는 다시 실행해도 새 시험 메시지를
만들지 않습니다. 다른 장애 대기 건은 시험 명령에서 발송하지 않습니다.

```bash
python3 -I -B /usr/local/libexec/pickle/discord-ops-alerts.py \
  --config /etc/pickle-discord-alerts/config.json --test-message
```

채널의 실제 메시지와 `state.json`의 `message_id`를 대조합니다. 대기열의 비정상
결과는 원인을 확인하고 다음 단계로 진행합니다. 첫 운영 실행은 이미 진행 중인
장애도 새 알림으로 전달하므로 해당 채널의 수신 범위를 먼저 확인합니다.

```bash
systemctl daemon-reload
systemctl start pickle-discord-ops-alerts.service
systemctl enable --now pickle-discord-ops-alerts.timer
systemctl list-timers pickle-discord-ops-alerts.timer
```

timer는 1분마다 실행하며 최대 10초의 지연을 둡니다. 한 실행에서 HTTP 요청을
세 건까지 처리하고 다음 timer가 나머지를 이어갑니다. 기존 API나 DB 재기동은
필요하지 않습니다. 새 webhook 설정과 복구 사본의 보호 위치를 운영 기록에 남깁니다.

## 전송 상태와 복구

대기열은 root 전용 `state.json`에 원자 교체와 디렉터리 fsync로 저장하고
`dispatcher.lock`으로 직렬화합니다. 요청 전 `ATTEMPTED`를 저장하며 프로세스가
그 상태에서 중단되면 다음 실행은 `UNCERTAIN`으로 기록합니다. HTTP 요청에는
`wait=true`와 빈 `allowed_mentions.parse`를 넣습니다. 환경변수 proxy를 사용하지
않고 redirect를 따라가지 않습니다. URL이나 HTTP 오류 원문을 로그에 쓰지 않습니다.

| 상태 | 처리 |
|---|---|
| `PENDING` | 다음 시도 시각에 전송합니다. 명확한 4xx 거절은 1분, 5분 간격으로 최대 세 번 시도합니다. |
| `SENT` | HTTP 200과 Discord message ID를 확인했습니다. 실제 채널 표시와 접근 권한은 별도로 확인합니다. |
| `UNCERTAIN` | 5xx, 응답 유실, 성공 응답의 ID 부재 또는 중단된 요청입니다. 자동 재전송하지 않습니다. |
| `FAILED` | 명확한 거절의 재시도 소진 또는 전송 규약 오류입니다. 보호 설정과 채널 권한을 확인합니다. |

429는 전달 실패 횟수와 분리해 집계하며 응답의 `retry_after` 이후에 재시도합니다.
같은 호스트의 대기열 전체에 이 시각을 적용합니다. 여러 호스트가 같은 webhook을
사용하면 Discord가 반환하는 제한에 각각 따릅니다. 알림은 일반화한 한글 상태와
표시 이름, 확인 시각만 포함합니다. 내부 주소, 원문 로그, SMTP 설정과 개인정보는
복사하지 않습니다.

`UNCERTAIN`은 채널과 message ID를 확인하고 별도 판단합니다. 상태 파일을 지우거나
`ATTEMPTED`를 무조건 `PENDING`으로 바꾸면 중복 발송할 수 있습니다. 이 도구에는
일괄 재전송 명령이 없습니다. 미해결 기록은 보존하며 확인된 전송 기록은 최근
일부만 유지합니다. 미해결 대기열이 제한을 넘으면 기록을 버리지 않고 중단합니다.

되돌릴 때에는 이 timer만 stop/disable한 뒤 진행 중인 service가 끝났는지
확인합니다. 자신의 설치 파일과 설정은 경로·해시로 대조하고 회수합니다. 전송 상태는
중복 방지와 사건 이력의 증거이므로 보존합니다. 기존 감시 timer와 SMTP 설정은
별개로 유지합니다. 이 도구 자체가 실행되지 않거나 호스트 전체가 꺼지면 같은
호스트에서 알릴 수 없습니다.

오프라인 검증은 실제 Discord나 systemd에 접속하지 않습니다.

```bash
python3 -B scripts/tests/test_discord_ops_alerts.py
```
