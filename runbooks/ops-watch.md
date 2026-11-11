# ops-watch 운영 감시

교내 호스트가 점검 결과를 AWS로 보내고, AWS 한 곳에서 판정해 Discord와 상태 페이지에 알리는 감시 체계예요.
기존 `discord-ops-alerts`, collector, 백업 감시기의 메일 알림을 대체해요. 코드는 이 레포의 `ops-watch/`에 있어요.

## 구조

| 구성 요소 | 위치 | 하는 일 |
|---|---|---|
| 에이전트 | pve-node, pve-node-2, pve-node-3, gpu-node, AWS | 카탈로그에서 자기 호스트 항목만 골라 점검하고, 항목별 최신 결과 하나씩을 묶어 보내요. 판정하지 않아요. |
| 수신 | AWS, SSH 강제 명령 | 묶음을 검사해 `/var/lib/pickle-ops-watch/inbox/<호스트>.json`에 AWS 시각과 함께 저장해요. |
| 판정 | AWS, `opswatch` 계정 | 1분마다 모든 묶음을 읽고 상태 변화만 Discord로 보내요. 상태 페이지 데이터도 써요. |
| 보조 경로 | pve-node-3 에이전트 | AWS에 결과를 세 번 연속 못 보내거나 판정이 멈추면 pve-node-3가 직접 Discord로 알려요. |

- 교내 호스트는 결과를 밀어 넣기만 해요. AWS에는 교내로 들어가는 키가 없어요.
- 호스트의 키 하나는 강제 명령으로 자기 호스트 이름이 고정돼 있어요. 카탈로그가 그 호스트에 배정한 항목만 쓸 수 있어요.
- 수신은 점검 종류마다 선언한 detail 필드만 받아요. IP 형태의 값, 마크다운과 멘션 문법, 512 KiB 초과 묶음은 거부해요.

## 판정 규칙

- 새 표본(`sample_at`이 바뀐 결과)만 세요. 1분 판정이 15분 주기 점검을 여러 번 세지 않아요.
- 장애는 연속 실패가 `fail_after`(기본 1, 공개 HTTP와 생성 점검은 2)에 닿는 순간 알려요.
- 복구는 정상 표본 한 번이면 바로 알려요. 다시 실패하면 또 알려요. 안정화 대기나 재알림 주기는 없어요.
- `ERROR`는 점검 자체가 실패한 경우예요. 장애를 열지도 닫지도 않고 ⚙️ 감시 문제로만 알려요.
- 호스트 묶음이 190초 넘게 안 오면 그 호스트 감시가 끊긴 거예요. 그 호스트 항목은 상태를 바꾸지 않아요.
- 첫 실행은 현재 상태만 기록하고 보내지 않아요.
- 매일 09:00 KST 이후 첫 실행에서 요약 한 건을 보내요. 설치한 날은 보내지 않아요.

## 알림 분류

| 분류 | 색 | 제목 |
|---|---|---|
| 장애 | 빨강 | `🔴 장애 · 대상` |
| 주의 | 노랑 | `🟡 주의 · 대상` |
| 감시 문제 | 회색 | `⚙️ 감시 문제 · 대상` |
| 복구, 감시 재개 | 초록 | `🟢 복구 · 대상` |

- 같은 판정 회차에서 같은 그룹의 장애가 둘 이상이면 embed 하나로 묶어요.
- 운영 외 항목(스테이징, 개발 콘솔, gpu-node)은 설명 앞에 「사용자 영향 없음」을 붙이고 영향 칸을 빼요.
- 원인 문장은 각 점검 모듈의 `REASONS` 표가 정본이에요. 표에 없는 코드는 「원인 정보를 받지 못했어요」로 나와요.

## 교내 연결이 끊겼을 때

| 교내 묶음 | AWS 바깥 점검 | 판단 | 알림 |
|---|---|---|---|
| 전부 끊김 | 사용자 서비스 장애 | 교내망, 정전, 교내 NAT 장애 | 🔴 「교내 연결 끊김」 한 건 |
| 전부 끊김 | 정상 | 교내에서 AWS로 나가는 경로만 막힘 | ⚙️ 「교내 감시 결과 미도착」 한 건 |
| 일부만 끊김 | 상관없음 | 그 호스트 장애 | ⚙️ 호스트별 「감시 결과 미도착」 |

- 연결이 돌아오면 복구를 한 번 보내고, 끊기기 전과 상태가 달라진 항목만 따로 알려요.
- 에이전트는 실패한 묶음을 쌓아 두지 않아요. 연결이 돌아와도 옛 결과가 한꺼번에 오지 않아요.
- pve-node-3 보조 경로는 Discord에도 닿지 않으면 보내지 않아요. 교내 밖 연결 전체가 끊긴 상황이기 때문이에요.
- AWS와 교내가 함께 죽으면 알림이 나가지 않아요. 이때는 09:00 요약이 오지 않는 것으로 알아차려요.

## 항목 추가와 변경

1. `ops-watch/catalog/items/<호스트>.json`에 항목을 추가해요. 필드는 `opswatch/catalog.py`가 검사해요.
2. 새 점검 종류가 필요하면 `opswatch/checks/`에 모듈을 추가해요. 계약은 `checks/__init__.py` 맨 위에 있어요.
3. `bash scripts/verify.sh`를 통과시키고, `ops-watch/bin/ops-watch render-samples --catalog ops-watch/catalog`로 문구를 확인해요.
4. 공개 사본 `infra-example`에는 같은 작업 단위로 반영해요. 카탈로그의 실제 주소와 경로는 예시 값으로 바꿔요.
5. 각 호스트에서 `install.sh agent`(AWS는 `install.sh judge`)를 다시 실행해 카탈로그를 교체해요.

`target`과 `impact`는 Discord에 그대로 나가요. `page_label`은 공개 상태 페이지에 나가므로 CT 번호와 호스트 이름, IP를 넣지 않아요.

## 설치

### AWS (판정, 수신, 상태 페이지)

1. 레포 체크아웃에서 `sudo ops-watch/install.sh judge`를 실행해요. `opswatch` 계정과 디렉터리, 유닛이 생겨요.
2. `/etc/pickle-ops-watch/judge.json`을 `ops-watch/examples/judge.json`대로 만들어요. 소유자 `opswatch`, 모드 `0600`이에요. 처음에는 `"send": false`로 둬요.
3. Discord 웹훅 URL 한 줄을 `/etc/pickle-ops-watch/secrets/webhook`에 넣어요. 소유자 `opswatch`, 모드 `0600`이에요.
4. AWS 자신의 에이전트 설정을 `examples/agent-judge-host.json`대로 `/etc/pickle-ops-watch/agent.json`에 둬요. root 소유 `0600`이에요.
5. 공개 생성 점검 키를 `/etc/pickle-ops-watch/secrets/public-generation.key`에 둬요. root 소유 `0600`이에요.
6. 교내 호스트 공개키를 `examples/authorized_keys` 형식으로 `/home/opswatch/.ssh/authorized_keys`에 넣어요.
7. `install.sh enable-agent`, `install.sh enable-judge`로 타이머를 켜요.
8. 상태 페이지는 `nginx/pickle-status.conf`를 쓰고, 인증서는 이름 하나짜리 Let's Encrypt HTTP-01로 받아요. Lightsail 방화벽에서 80과 443만 열어요.

### 교내 호스트 (pve-node, pve-node-2, pve-node-3, gpu-node)

1. `sudo ops-watch/install.sh agent`를 실행해요.
2. 전송 키를 만들어요: `ssh-keygen -t ed25519 -N '' -f /etc/pickle-ops-watch/secrets/push.key`. 공개키는 AWS `authorized_keys`에 그 호스트 이름의 강제 명령으로 넣어요.
3. AWS 호스트 키를 `/etc/pickle-ops-watch/judge_known_hosts`에 고정해요.
4. `examples/agent-campus.json`을 복사해 `host`와 `destination`을 고쳐 `/etc/pickle-ops-watch/agent.json`에 둬요. pve-node-3는 `examples/agent-pve-node-3.json`을 쓰고 보조 경로용 웹훅도 둬요.
5. gpu-node은 로컬 생성 점검 키를 `/etc/pickle-ops-watch/secrets/dgx-local.key`에 둬요.
6. `sudo /usr/bin/python3 -I -B /usr/local/lib/pickle-ops-watch/bin/ops-watch agent --config /etc/pickle-ops-watch/agent.json`을 한 번 실행해 `pushed: true`를 확인한 뒤 `install.sh enable-agent`로 켜요.

## 전환과 되돌리기

1. 판정을 `"send": false`로 이틀 돌리며 `ops-watch inspect`와 상태 페이지를 기존 알림과 비교해요.
2. `"send": true`로 바꾸고, 같은 시각에 기존 `pickle-discord-ops-alerts.timer`를 세 호스트에서 꺼요.
3. 백업 감시기와 스테이징 collector의 메일 발송을 꺼요.
4. 다음 날 09:00 요약이 오는지 확인해요.

되돌릴 때는 `install.sh disable`로 새 타이머를 끄고 기존 타이머를 다시 켜요. 기존 파일은 철거 단계 전까지 그대로 둬요.

## 확인 명령

| 목적 | 명령 |
|---|---|
| 호스트별 최신 결과 | `sudo -u opswatch /usr/bin/python3 -I -B /usr/local/lib/pickle-ops-watch/bin/ops-watch inspect --config /etc/pickle-ops-watch/judge.json` |
| 판정 상태와 전송 기록 | `sudo -u opswatch sqlite3 /var/lib/pickle-ops-watch/judge.sqlite 'select * from items; select id, delivery, code from outbox order by id desc limit 10;'` |
| 시험 메시지 | `sudo -u opswatch /usr/bin/python3 -I -B /usr/local/lib/pickle-ops-watch/bin/ops-watch test-message --config /etc/pickle-ops-watch/judge.json` |
| 에이전트 실행 기록 | `journalctl -u pickle-ops-watch-agent.service -n 20` |

## 설치 전에 실측할 것

점검 모듈을 만들 때 문서만으로 확인하지 못한 값이에요. 설치 단계에서 각 호스트에서 확인해요.

- AWS 릴레이 유닛 이름(`haproxy.service`, `relay-agent.service`, `wg-quick@wg0.service`)과 판정 서버가 릴레이와 같은 장비인지
- CT201 콘솔 nginx 유닛 이름, pve-node과 pve-node-2의 thin pool 이름 `pve/data`
- DB 백업 감시 설정 경로와 PBS 용량 설정 경로
- gpu-node `nvidia-smi`가 온도를 보고하는지
- 교내 호스트에서 AWS 관리 SSH 포트로 나가는 경로
- LLM 실패율 기준(실패 비율 0.2, 최소 요청 10건), 메일 적체 15분과 60분, 잔액 경고 20달러는 잠정값이에요. 이틀 비교 기간에 조정해요.

최종 갱신: 2026-11-12
