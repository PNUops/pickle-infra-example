# PostgreSQL dump의 PBS 백업과 독립 복구점 감시

이 문서는 별도 DB LXC에서 5분마다 PostgreSQL dump를 PBS로 보내고, 다른 호스트에서
현재 복구 가능한 시점을 확인하는 절차다. 도구 작성과 실제 설치를 구분한다. 아래
설정과 timer는 아직 배치됐다는 뜻이 아니며 실제 PBS 연결과 복원 인수시험이 필요하다.

정기 대상은 플랫폼 DB와 비밀번호를 제외한 PostgreSQL role 정의다. 사용자 VM 디스크는
정기 대상으로 추가하지 않는다. API env, 인증서, gateway 설정과 업로드 파일은 별도의
플랫폼 설정 백업 대상이며 이 DB dump만으로 복구되지 않는다.

## 성공과 실패 기준

`scripts/db-pbs-backup.sh`는 기본 dry-run이다. `--run`은 다음을 모두 수행한다.

1. 기대 hostname, primary 주소, PostgreSQL 18 system identifier와 애플리케이션 schema를
   확인한다. 초기화만 된 빈 DB나 standby, 잘못된 DB는 백업 성공으로 표시하지 않는다.
   `PGHOST`/`PGSERVICE` 등 상속된 libpq 환경은 제거하고 로컬 Unix socket의 5432 포트와
   postgres peer 인증을 명시하여 다른 DB로 접속하는 것을 막는다.
2. root 전용 임시 디렉터리에 `pg_dump -Fc`, `pg_dumpall --globals-only --no-role-passwords`를
   만든다. 시간과 DB 신원, dump/role SHA-256을 별도 manifest에 기록한다.
3. client-side encryption을 명시하여 PBS `host/<backup_id>/<time>`에 업로드한다.
4. PBS에서 그 archive를 새 경로로 실제 restore하고 세 파일의 SHA-256을 원본과 비교한다.
   반환된 dump의 `pg_restore --list`도 통과해야 한다.
5. 성공 receipt를 별도의 암호화된 `verification.pxar`로
   `host/<backup_id>-verified/<같은 time>`에 게시한다. 이 snapshot의 manifest와 receipt도
   내려받아 대조한 뒤에만 source의 verified checkpoint를 전진시킨다.

데이터 시점은 업로드 완료 시간이 아니라 **pg_dump 시작 직전 시각**이다. 실제 snapshot
획득보다 이른 보수적 기준이며 WAL 시점이나 마지막 commit LSN을 실측한 값은 아니다.
파일 왕복은 매 백업마다 검증하지만 DB 전체 SQL 복원과 서비스 재개는 뒤의 별도 시험이다.

감시는 DB LXC 밖의 독립 호스트에 둔다. 초기 배치 후보는 pve-node-3이며, DB나 pve-node-2가
내려가도 감시할 수 있어야 한다. pve-node-3의 CPU, 메모리와 디스크 예약에는 monitor의
PBS client, 복호화와 임시 파일 및 상태 디렉터리도 포함한다. 실제 사용량을 측정하고
core 복구 예약과 함께 사용자 배치 용량에서 제외한다. 이후 DB를 다른 호스트로
수동 복구할 때에는 monitor와 DB의 장애 범위를 다시 확인한다.

Monitor는 source의 로컬 checkpoint를 읽지 않는다. PBS의 data/proof 그룹을 현재
조회하고 최신 완성된 proof, 복호화한 receipt, 참조하는 data snapshot의 현재 manifest를
대조한다. PBS 삭제, 권한 변경, 접속 실패, 서명/파일 식별 불일치, 서버 verify 실패를
최근 로컬 기록만으로 통과시키지 않는다.

| 조건 | 판정 |
|---|---|
| 원격 증거가 일치하고 데이터 시점이 10분 미만 | HEALTHY |
| 10분 이상, 15분 미만 | WARNING |
| 15분 이상 또는 현재 원격 증거를 확인할 수 없음 | FAILED |

Monitor는 1분 주기다. 판정 함수의 경계는 600/900초이며 알림 시각에는 최대 한 주기의
탐지 지연과 네트워크 지연이 추가된다. 원격 조회 전체 예산은 45초다. source가 꺼져도
판정이 가능하도록 source DB 서비스나 backup enable marker에 의존하지 않는다.
데이터 시점의 나이는 원격 조회가 모두 끝난 시각으로 계산하므로 조회 중 경계를 넘으면
그 결과에 WARNING 또는 FAILED를 반영한다.

## 도구와 키 준비

2026-09-16 공식 문서는 PBS client 4.2.5-1 기준이다. 두 실행 환경에 지원되는
`proxmox-backup-client`를 별도 설치하고, source에는 PostgreSQL 18의 `pg_dump`,
`pg_dumpall`, `pg_restore`, `psql`이 있어야 한다. 설치와 패키지 출처 검증은 실제 배치
단계에서 수행한다. 감시 호스트에는 PostgreSQL server나 source DB 접근이 필요하지 않다.

- source token은 이 작업 전용 namespace와 두 host group의 backup/read/prune에만 사용한다.
- monitor token은 같은 namespace를 읽는 별도 token이며 backup/prune 권한을 주지 않는다.
- API token 값, client encryption key와 필요 시 key password는 root 소유 0600 파일이다.
  JSON 설정에는 값 대신 절대 경로만 넣는다.
- encryption key의 복구 사본을 datastore와 source/monitor 밖의 보호된 위치에도 보관한다.
  새 클라이언트에서 이 사본으로 복호화하는 시험을 마친 뒤 custody receipt를 작성한다.
- receipt의 선언은 보관 위치에 대한 운영자 확인 기록이다. 도구가 원격 vault를 직접
  검증한 것으로 해석하지 않는다. 키 내용과 password는 dump, verification receipt와 로그에 넣지 않는다.
- default `~/.config/proxmox-backup/master-public.pem`이 있으면 source는 멈춘다.
  자동 RSA key export가 섞이지 않는 전용 client 환경을 사용한다. 기존 파일을 자동 삭제하지 않는다.

Custody receipt 형식:

```json
{
  "key_file_sha256": "실제 암호키 파일의 SHA-256",
  "outside_datastore": true,
  "custody_reference": "별도 암호화 복구 보관 위치의 식별자",
  "recovery_copy_verified_at": "2026-09-16T00:00:00+00:00"
}
```

## 설정과 로컬 설치

다음은 값의 형태를 보여 주는 예시다. 실제 DB system identifier는 source에서
`SELECT system_identifier FROM pg_control_system()`으로 확인하며, 모든 자리표시자를
실제 배치 정보로 바꾼다. `instance_id`는 논리적인 DB 서비스의 고정 UUID다. 복원으로
PostgreSQL system identifier가 바뀌면 source 설정은 갱신하되 논리 UUID와 옛 복구점은 보존한다.

```json
{
  "expected_hostname": "pickle-db",
  "monitor_hostname": "pve-node-3",
  "instance_id": "629a771b-012b-4fea-8c0d-5f171573a4fa",
  "database": "pickle_verify",
  "expected_system_identifier": "1234567890123456789",
  "minimum_schema_version": 121,
  "source_address": "100.65.1.21",
  "repository": "db-backup@pbs!writer@pbs.example.test:platform",
  "namespace": "platform-verification",
  "backup_id": "application-db",
  "server_fingerprint": "ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab:ab",
  "password_file": "/etc/pickle-db-backup/pbs-token",
  "encryption_key_file": "/etc/pickle-db-backup/encryption-key.json",
  "encryption_password_file": "/etc/pickle-db-backup/encryption-password",
  "escrow_receipt_file": "/etc/pickle-db-backup/key-custody.json",
  "state_dir": "/var/lib/pickle-db-backup",
  "monitor_state_dir": "/var/lib/pickle-db-monitor",
  "mail_config_file": null
}
```

Monitor 쪽 설정은 같은 논리 UUID, DB명, namespace와 backup ID를 사용하되 repository의
auth ID와 `password_file`은 reader token으로 바꾼다. key 파일은 monitor의 보호된 사본을
지정한다. `mail_config_file`도 monitor에서만 설정한다. source 로컬 상태를 복사하지 않는다.

두 실행 환경에 아래 파일을 같은 상대 구조로 설치한다. 기존 파일을 덮어쓰지 말고
처음 설치인지 확인한다.

```text
/opt/pickle/db-backup/bin/db-pbs-backup.sh
/opt/pickle/db-backup/bin/lib/db_pbs_backup.py
/opt/pickle/db-backup/bin/lib/isolated_core.py
/etc/pickle-db-backup/config.json
```

```bash
# 기본 계획 출력: 자격증명이나 PBS를 읽지 않는다.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json

# source에서 1회. 존재하는 상태 디렉터리를 초기화하지 않는다.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json --initialize

# monitor에서 별도로 1회.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json --initialize-monitor

# source의 첫 실제 backup/readback. 결과의 retention도 확인한다.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json --run

# monitor에서 현재 PBS 데이터로 독립 확인한다.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json --status

# 검토할 네 systemd unit의 내용만 JSON으로 출력한다. 설치하거나 enable하지 않는다.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json --render-units
```

source에는 출력된 `pickle-db-pbs-backup.service/.timer`, monitor에는
`pickle-db-pbs-monitor.service/.timer`만 설치한다. 첫 backup과 독립 조회, 복원 시험을
통과한 뒤 source의 `/etc/pickle-db-backup/enable-backup` marker와 해당 timer를 활성화한다.
Monitor timer에는 source marker를 조건으로 넣지 않는다. timer의 명령은 설치 경로의
스크립트를 `/bin/bash`로 실행한다.

## 보존과 실패 처리

정책은 최근 288개, 일별 7개, 주별 4개의 **검증된 복구점 쌍**을 유지한다.
PBS의 proof 그룹에 공식 prune dry-run을 수행하고 현재 검증점이 keep인지 먼저 확인한다.
제거 대상마다 receipt의 data 참조와 실제 data manifest를 대조하여 data snapshot을
제거하고, 현재 목록에서 제거를 확인한 다음 proof를 제거한다. 부분 실패는 다음 실행에서
현재 목록을 다시 읽어 처리하며, data와 proof를 각각 독립 prune하지 않는다.

검증되지 않은 원격 data snapshot은 이 보존 집계에 넣거나 자동 삭제하지 않는다.
실패 원인과 task, 소유권을 확인한 뒤 개별 처리한다. 반복 실패 시 이 자료의 공간 증가도
확인한다. source의 retention 실패는 nonzero 종료와 `maintenance.json`에 남는다.
이는 데이터 시점의 freshness와 별개이며, 독립 monitor의 HEALTHY가 보존 정책 성공을 뜻하지 않는다.

local spool은 비밀을 담은 임시 파일이다. root 전용이고 실패본은 가장 최근 한 벌을
남기며, 다음 시도와 함께 최대 두 벌을 사용한다. cleanup은 같은 instance/run UUID의
확인된 디렉터리만 처리한다. 모르는 파일이나 소유권이 있으면 삭제하지 않고 멈춘다.
DB 크기의 3배와 2 GiB의 여유가 없으면 새 dump를 시작하지 않아 DB 파일시스템을 보호한다.
외부 입력 키, 원본 DB, 원본 archive와 다른 backup group은 cleanup 대상이 아니다.

## 알림 준비

이 단계에서는 메일 설정 파일을 준비하고 `enabled`는 false로 둔다. 실제 연동 후 승인된
테스트 한 통은 운영자가 실행한다. 최초 HEALTHY는 `BASELINE` receipt만 기록하고 메일을 보내지
않는다. 이후 WARNING 또는 FAILED, 장애 뒤 HEALTHY 복구처럼 상태가 바뀔 때만 발송하고 같은
상태는 반복하지 않는다.
실제 연동 단계에서 수신자와 TLS 설정을 확인하고 `enabled`를 true로 바꾼 뒤 테스트한다.

```json
{
  "enabled": false,
  "host": "smtp.example.test",
  "port": 587,
  "tls_mode": "starttls",
  "username": "backup-notifier@example.test",
  "password_file": "/etc/pickle-db-backup/smtp-password",
  "sender": "backup-notifier@example.test",
  "recipient": "operator@example.test"
}
```

```bash
# monitor에서 실제 연동 뒤 명시적으로 1회만 실행한다.
bash scripts/db-pbs-backup.sh --config /etc/pickle-db-backup/config.json --test-email
```

`mail-test.json`은 SMTP 전에 ATTEMPTED를 기록한다. 송신 결과가 불확실해도 자동 재발송하지
않는다. SENT 또는 전달 불확실 상태를 확인하며 receipt 삭제로 무작정 재시도하지 않는다.
인증값은 출력하지 않고 TLS 검증을 끄는 옵션도 없다.

운영 상태 알림은 SENT만 중복 제거한다. 연결·인증 실패나 SMTP의 명시적 거부처럼
미전송이 확인된 경우에는 60초, 300초 후 재시도하며 총 3회로 제한한다. 전송 도중 연결이
끊겨 수신 여부가 불명확하면 UNCERTAIN으로 남기고 자동 재발송하지 않는다. 프로세스 중단으로
ATTEMPTED만 남았거나 재시도 한도를 넘은 경우도 nonzero 종료를 유지한다. 운영자는 SMTP
기록과 수신 여부, 원인을 확인한 뒤 해당 receipt를 보존하고 새 시도 여부를 결정한다.
테스트 한 통에는 이 자동 재시도 정책을 적용하지 않는다.

## 전체 DB 및 서비스 수동 복구 인수시험

1. 이번 검증이 소유한 서비스만 대상으로 하고 source DB writer와 API/jobs를 정지한다.
   source 네트워크와 자동 시작을 차단한 뒤 대상의 새 DB LXC 및 private 연결을 준비한다.
   원본 DB, 디스크와 backup snapshot은 보존한다.
2. 독립 monitor가 확인한 proof와 data snapshot을 지정한다. datastore 밖의 복구용 key
   사본과 reader token으로 새 private 경로에 archive를 restore하고 receipt의 hash와 비교한다.
3. target DB가 비어 있고 기대한 DB/role인지 확인한 뒤 `pg_restore --exit-on-error
   --single-transaction --no-owner --role=<대상 role>`로 복원한다. 기존 DB에 `--clean`이나
   dump 덮어쓰기를 기본 실행으로 두지 않는다. role 정의는 검토하고 필요한 역할만 맞춘다.
4. role password와 API의 암호화 키 등은 별도 보관 자료로 맞춘다. dump에 role password를
   넣지 않았으므로 SQL만 복원한 상태를 로그인 및 암호화 값 복구 성공으로 판단하지 않는다.
5. 실제 표/row의 보존, 로그인, 같은 서비스 주소의 API 조회와 소유한 test VM 제어,
   proxy/SSH/웹터미널 및 정책 동작을 확인한다. 유일한 DB writer와 job owner를 확인한 뒤에만
   대상의 API/jobs와 backup timer를 재개한다.
6. source 재가동에 따른 중복 writer가 없음을 확인한다. 장애 선언부터 서비스 재개까지의
   RTO와 마지막 보존된 업무 데이터 시점의 RPO를 실제 기록한다. 파일 readback 시간을
   서비스 RTO로 대신하지 않는다.

빈 target DB에서 실행할 명령의 형태는 다음과 같다. 대상 hostname과 역할, 경로는
그 복구 작업에서 확인한 값으로 지정한다. 마지막 명령은 DB에 쓰므로 원본에서 실행하지 않는다.

```bash
EXPECTED_TARGET=pickle-db-restore
TARGET_DB=pickle_verify
TARGET_ROLE=pickle_verify
RESTORED_DUMP=/root/restore-check/database.dump
RESTORE_LOG=/root/restore-check/pg-restore.log
test "$(hostname -s)" = "$EXPECTED_TARGET" || exit 1
table_count=$(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
  runuser -u postgres -- /usr/lib/postgresql/18/bin/psql -X -qAt -w \
  -h /var/run/postgresql -p 5432 -U postgres -v ON_ERROR_STOP=1 -d "$TARGET_DB" \
  -c "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace \
      where n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_toast%'")
test "$table_count" = 0 || exit 1
test ! -e "$RESTORE_LOG" || exit 1
umask 077
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
  runuser -u postgres -- /usr/lib/postgresql/18/bin/pg_restore \
  -h /var/run/postgresql -p 5432 -U postgres -w \
  --exit-on-error --single-transaction --no-owner --role="$TARGET_ROLE" \
  --dbname="$TARGET_DB" < "$RESTORED_DUMP" > "$RESTORE_LOG" 2>&1
```

## 검증과 근거

```bash
python3 scripts/tests/test_db_pbs_backup.py
shellcheck scripts/db-pbs-backup.sh
bash scripts/verify.sh
```

자체 검사는 remote snapshot 부재, source 상태 없이 수행하는 독립 판정, 600/900초 경계,
변조·누락·암호화 미설정, receipt 게시 실패, 보존 범위와 mail 중복 방지를 검증한다.
실제 PBS/SMTP 연결과 SQL 복원은 수행하지 않는 offline 시험이다.

공식 근거는 [client 사용법](https://pbs.proxmox.com/docs/backup-client.html),
[CLI 명세](https://pbs.proxmox.com/docs/proxmox-backup-client/man1.html),
[manifest 형식](https://github.com/proxmox/proxmox-backup/blob/master/pbs-datastore/src/manifest.rs)이다.
사후 `client.log.blob`는 upload-log가 암호화를 지원하지만 원래 manifest의 파일이 아니므로
일반 restore의 파일 조회 조건에 맞지 않는다. 따라서 이 도구는 두 번째 정식 snapshot의
verification archive를 사용한다. source/reader token 권한과 두 그룹의 paired retention도
실제 배치 때 함께 확인한다. 서버의 verify/GC 일정은 별도이며 client readback을 대체하지 않는다.

최종 갱신: 2026-09-16
