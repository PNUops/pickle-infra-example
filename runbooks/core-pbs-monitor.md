# pve-node-3 코어 PBS 백업 독립 감시

이 감시는 pve-node-3에서 PBS 읽기 전용 스토리지 `pbs-example-core-read`의 실제 목록을
조회한다. 백업 작업의 종료 코드나 로컬 상태 파일을 정상 근거로 사용하지 않는다.
대상은 CT 1200, 1201, 1202, 1204이다. 기존 수동 복구점 네 개가 `protected=1`로
그대로 남아 있는지도 별도로 확인한다. 일일 복구점은 `protected` 값과 관계없이
형식 `pbs-ct`/`lxc`, CTID 경로, volid의 UTC 시각과 `ctime` 일치,
암호화 키 fingerprint, 양수 크기를 검사한다.

## 판정과 알림

일일 작업은 2025-01-02부터 03:17 KST에 시작한다. 첫 시행 전인
2025-01-01에는 기존 수동 복구점 네 개가 정상이면 `BASELINE`을 기록한다.
첫 시행일 이후 04:30 KST 전에는 오늘의 새 복구점이 없거나 일부만 있어도
`PENDING`으로 기다리고, 그 시각부터는 네 CT 모두에 오늘 03:17 이후
생성된 복구점이 있어야 정상이다. 첫 실행이 2025-01-02 04:30 이후더라도
이 기준을 소급 적용한다. 목록 조회 실패, 수동 복구점 소실, 잘못된 메타데이터와
기한을 넘긴 일일 복구점은 `FAILED`로 판정한다. 최초 정상 판정은 `BASELINE`으로
기록하고 메일을 보내지 않는다. 이후 정상→실패에 `FAILED`, 실패→정상에
`RECOVERED` 메일을 한 번씩 보낸다. 같은 상태의 반복 실행은 다시 보내지 않는다.
용량 증가에 맞춰 마감 시각을 바꿔야 하면 `--deadline HH:MM`으로 KST 시각을
명시한다. 기본값은 `04:30`이다. 첫 시행일은 유닛에
`--first-due-date 2025-01-02`로 고정한다. 배치 시각에 맞춰 이 날짜를
뒤로 미루면 실제 누락을 가리므로 변경 전 운영 판단이 필요하다.

SMTP는 이미 pve-node-3에 설치된 `/etc/pickle-example/mail.json`의 `host`, `port`,
`tls_mode`, `username`, `sender`, `recipient`, `password_file`을 사용한다.
수신자는 배치 전에 운영자가 지정한 `<operator-email>`과 일치해야 한다. 별도의 자격증명 사본을 만들지
않는다. 발송 전에 별도 상태 디렉터리에 `ATTEMPTED`와 시도 횟수를 기록한다.
설정 파일 부재, SMTP 연결·인증 실패와 명시적 메시지 거부처럼 수락되지 않은
것이 확실하면 `NOT_SENT`로 기록한다. 첫 실패 뒤 1분, 두 번째 실패 뒤 5분이
지난 후 같은 상태를 다시 검사할 때 재시도한다. 총 3회 실패하면
`RETRIES_EXHAUSTED`로 남기고 자동 재시도를 멈춘다. 타이머가 5분 간격이라
실제 재시도는 다음 타이머 실행 시점에 이뤄진다.

DATA 제출 이후 연결이 끊겨 수락 여부를 모르면 `UNCERTAIN`으로 기록한다.
프로세스가 발송 도중 중단돼 `ATTEMPTED`만 남아도 자동 재발송하지 않는다.
이 두 상태와 `RETRIES_EXHAUSTED`에서는 운영자가 SMTP 수락과 수신함을
확인한 뒤 조치한다. `SENT`는 SMTP 수락이며 최종 수신함 도착의 증거는 아니다.
상태 파일은 `/var/lib/pickle-core-pbs-monitor/state.json`이다.

## 설치 전 확인과 실행

아래 경로와 명령은 **pve-node-3**용이다. 적용 전에 PBS reader 목록에서 네 수동
복구점과 fingerprint를 다시 확인한다. 암호나 `mail.json` 본문을 출력하지 않는다.
다음 명령은 검토한 이 예시 레포지토리 checkout에서 pve-node-3 운영자가 실행한다. 설치 전에
`timedatectl show -p Timezone -p NTPSynchronized`가 각각 `Asia/Seoul`, `yes`인지
확인한다. 스크립트도 매 실행에서 이를 확인하고, 이전 검사보다 시계가 뒤로 간
경우 중단한다.

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

서비스는 5분마다 실행되고 한 번에 최대 120초 기다린다. 1일 첫 수동 실행은
수동 복구점 네 개만 정상이라면 메일 없이 `BASELINE`을 남긴다. 이후 상태는
`systemctl status pickle-core-pbs-monitor.service`와
`journalctl -u pickle-core-pbs-monitor.service`로 확인한다. 설치한 파일을
수정한 뒤에는 다시 설치하고 `systemctl daemon-reload`를 실행한다.

감시를 되돌릴 때에는
`systemctl disable --now pickle-core-pbs-monitor.timer`로 다음 실행을 막고
진행 중인 service가 없는지 확인한다. 설치한 유닛과 스크립트는 경로·해시로
이 작업의 소유임을 대조한 뒤에만 회수한다. 상태 파일은 장애·메일 수락
판정의 증거이므로 먼저 보호 사본을 남기고 바로 초기화하지 않는다. pve-node-2
백업 job, PBS storage와 네 보호 수동 복구점은 감시 회수와 별개로 유지한다.

실행 계정은 root다. 상태와 lock은 `/var/lib/pickle-core-pbs-monitor/`에
root 소유 0700으로 둔다. DB 백업의 상태 및 lock과 공유하지 않는다. 정상은
종료 코드 0, 백업 검증 실패는 1, 스크립트·메일 전달 오류는 2다. `FAILED`인데
메일이 미발송 또는 불확실하면 종료 코드 2이며 상태 파일에는 해당 실패 판정과
전달 상태가 보존된다.

오프라인 회귀 검사는 다음 명령으로 수행한다. 이 검사는 PBS나 SMTP에 접속하지
않으며 실제 메일을 보내지 않는다.

```bash
python3 scripts/tests/test_core_pbs_monitor.py
```

예시 기준일: 2025-01-01
