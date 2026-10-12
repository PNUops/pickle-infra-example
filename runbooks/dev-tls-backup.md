# 개발 TLS의 별도 암호화 백업과 복구

2026-10-12 사전 준비를 적용했다. 개발 전용 namespace/user/token과 두 호스트의
새 복구자료, frozen exporter/worker, 별도 SSH pin과 forced reader, unit 파일이 설치됐다.
사전 준비 시 service는 loaded/static/inactive이고 timer는 loaded/disabled/inactive였다.
아래 현재 commissioning 결과와 초기 disabled 상태를 구분한다.
2026-10-13 HTTP-01 신규 lineage 발급과 실제 PBS snapshot backup과 node-restore의 독립 exact snapshot 복구를 검증했다.
같은 lineage의 hook 없는 renewal dry-run 성공 보고와 종료, 이후 source 전체 digest와
복구점 일치도 확인했다. 2026-10-13 03:54–04:00 KST에 개발 전용 TLS/SNI 선택과 두 번째 graceful reload,
공개 strict TLS/console/API/Host 검증을 마쳤다. 새 timer만 enabled/active/waiting이며
15분 calendar/Persistent=yes다. 첫 자연 trigger는 04:15:00 KST에 실행되고
04:15:02에 ExecMainStatus=0/Result=success로 종료한 것을 04:17 KST에 확인했다.
이때 timer의 다음 실행은 04:30 KST였다. 기존 route의 status/leaf/expiry가
전후 같고 선택 시 실제 source digest와 기존 복구점도 같았다. 첫 scheduled run의
unit 상태만 관측했으므로 그 실행이 같은 복구점을 재사용했다고 주장하지 않는다.
수동 worker의 source/복구점 대조와 scheduled unit 성공은 별도 증거다.
아래 인수시험과 복구 절차는 설치 재현 및 장애 복구 시의 재검증에 사용한다.

## 경로와 권한

개발 TLS source는 node-dev CT100의 `dev.example.com` Certbot lineage다. node-dev에서
PBS TCP 8007은 현재 도달하지 않으므로 overlay 정책을 바꾸지 않고 node-restore에서 pull한다.
node-restore에는 기존 PBS client와 OpenSSL을 사용한다. 운영 백업 namespace, token,
암호키, group과 job은 사용하거나 수정하지 않는다.

새 PBS namespace `dev-tls`, 새 writer user `pickle-dev-tls@pbs`, 별도 reader user `pickle-dev-tls-read@pbs`와
각각의 privsep token `writer`와 `reader`를 만든다. user와 writer에는 해당 namespace의 DatastoreBackup만,
reader user/token에는 해당 namespace의 DatastoreReader만 준다. 초기에는 자동 prune을 만들지
않고 용량을 확인한다. 대상 datastore는 기존 `example-store`이며 quota/여유 공간을
먼저 확인한다. group은 `host/node-dev-dev-tls`다.

node-restore root가 새 SSH Ed25519 read key와 CMS RSA recipient key, 새 PBS encryption key를
생성한다. root 전용 `/etc/pickle-dev-tls/` 0700, 개인키/token/config 0600을 사용한다.
node-dev에는 CMS public certificate와 고정 export config만 설치하며, 새 read key의 public
half를 아래 forced command로 등록한다. sshd가 이미 지원하는 `/root/.ssh/authorized_keys2`를 신규 생성해 등록했고,
기존 `/root/.ssh/authorized_keys` bytes는 보존했다. sshd 설정 변경이나 reload는 없다.

```text
from="198.18.0.12",restrict,command="/usr/bin/python3 -I /usr/local/libexec/pickle-dev-tls-export.py" ssh-ed25519 <NEW_PUBLIC_KEY>
```

node-restore의 pull은 node-dev private `198.18.0.11:22`를 사용한다. node-dev의 기존 검증된
host-key pin을 public 자료로 복사하고 StrictHostKeyChecking=yes, UpdateHostKeys=no,
IdentitiesOnly=yes를 고정한다. node-restore→node-dev private TCP 22 연결은 설치 전에 확인한다.

export config의 정확한 후보는 다음과 같다.

```json
{"hostname":"node-dev","ctid":100,"lineage":"dev.example.com","recipient_certificate":"/etc/pickle-dev-tls/recipient.pem"}
```

exporter는 renewal config, 현재 archive 4개와 그 live symlink 4개만 읽는다.
다른 lineage, Certbot account private key, DNS vendor key, nginx 설정은 포함하지 않는다.
원문 tar는 CT100/node-dev memory에만 두고 CMS ciphertext만 SSH stdout으로 보낸다.
stderr는 고정 실패 메시지만 낸다. Mac/Git/log에는 인증서 개인키를 보내지 않는다.

## 설치와 첫 인수시험 후보

1. CT100의 Python3 존재와 source hostname/lineage를 확인한다. protected path의 root
   ownership와 symlink/권한을 검사한 뒤 exporter를 고정된 신규
   `/usr/local/libexec/pickle-dev-tls-export.py`에 root:root 0600으로 설치한다.
   forced command는 이 파일만 호출하며 배포 checkout에서 직접 실행하지 않는다.
   node-restore worker도 `/usr/local/libexec/pickle-dev-tls-backup.py`에 고정 설치한다.
   `/var/lib/pickle-dev-tls/`를 root:root 0700으로 새로 만든다. 작업 중 재시작은 없다.
2. 새 PBS namespace와 ACL/token을 만들고 native encryption key를 node-restore에서 생성한다.
   비밀 값은 host-root 0600 파일로만 저장한다. PBS TLS fingerprint는 기존 검증된 pin을
   사용한다. 새 writer/reader가 다른 namespace를 읽거나 쓰지 못하는지 확인한다.
3. CMS private key와 PBS encryption key의 복구 사본을 node-dev의 새 root 전용
   `/etc/pickle-dev-tls/recovery/`에 보관한다. token custody도 동일 방식으로 별도 보관한다.
   기존 양방향 host-to-host SSH는 private 주소 host-key pin이 없어 strict 검증에 실패했다.
   따라서 node-dev에서 별도 custody transport RSA recipient를 만들고 public certificate만
   전달한다. node-restore가 recovery bundle을 CMS 암호화하고 운영자 Mac은 ciphertext만 relay한다.
   node-dev은 root tmpfs에서 복호화해 새 recovery 경로에 0600으로 보관한다. 새 read key는
   쓰기/키 전송에 사용하지 않는다. 원문은 Mac을 경유하지 않는다. 두 호스트의 SHA-256과
   mode/owner를 비교하고 node-dev 복구 사본을 node-restore의 새 임시 경로로 가져와 실제 복호화한다.
4. node-restore가 새 read key로 exporter를 호출해 CMS를 `/run/pickle-dev-tls/<new-id>/`에 받는다.
   복호화한 tar의 정확한 9개 member와 symlink 목적지를 검사하고 certificate/private key
   public key 일치, SAN 이름, 유효기간을 확인한다. 임시 평문은 tmpfs에만 두고 제거한다.
5. native client로 `proxmox-backup-client backup tls.pxar:<ciphertext-directory>`를 실행한다.
   `--repository`, `--ns dev-tls`, `--backup-id node-dev-dev-tls`, `--keyfile`을 모두 명시하고
   password는 root token file을 가리키는 PBS_PASSWORD_FILE로 전달한다.
6. node-restore의 새 경로에서 reader token과 복구 사본 key로 exact snapshot `tls.pxar`를 restore한다.
   원본 CMS SHA와 비교한 뒤 실제 CMS 복호화 및 4번의 인증서/키 검증을 다시 한다.
   PBS snapshot, 시간, cert fingerprint/notAfter, archive member 이름, SHA와 성공/실패만
   기록한다. 키/토큰 내용은 출력하지 않는다. 실패 시 timer를 설치하지 않는다.

example backup host에서 검토할 native 신규 명령은 다음과 같다. 실행 전에 같은 이름이 없음을 확인하고
현재 ACL/user 목록을 root 전용 경로에 보관한다. token 명령의 stdout은 Mac이나 로그로
보내지 않고 example backup host root 파일로만 받는다. key/token 전달도 recipient-encrypted CMS로 한다.

```bash
proxmox-backup-debug api create /admin/datastore/example-store/namespace --name dev-tls
proxmox-backup-manager user create pickle-dev-tls@pbs
proxmox-backup-manager user generate-token pickle-dev-tls@pbs writer > /etc/pickle-dev-tls/writer-result.json
proxmox-backup-manager user create pickle-dev-tls-read@pbs
proxmox-backup-manager user generate-token pickle-dev-tls-read@pbs reader > /etc/pickle-dev-tls/reader-result.json
proxmox-backup-manager acl update /datastore/example-store/dev-tls DatastoreBackup --auth-id pickle-dev-tls@pbs --propagate false
proxmox-backup-manager acl update /datastore/example-store/dev-tls DatastoreBackup --auth-id 'pickle-dev-tls@pbs!writer' --propagate false
proxmox-backup-manager acl update /datastore/example-store/dev-tls DatastoreReader --auth-id pickle-dev-tls-read@pbs --propagate false
proxmox-backup-manager acl update /datastore/example-store/dev-tls DatastoreReader --auth-id 'pickle-dev-tls-read@pbs!reader' --propagate false
```

writer와 reader의 user/token effective permission을 해당 namespace와 다른 namespace에서
각각 readback한다. reader user를 분리해 token/user 교집합에 Read가 없는 문제를 피한다.
기존 user/token ACL은 수정하지 않는다.

node-restore에서 새 key 생성 후보는 다음과 같다. root 경로를 0700으로 신규 생성하고 umask 077을
설정한 뒤 실행한다. CMS self-signed certificate는 암호화 recipient 식별용이며 TLS가 아니다.

```bash
ssh-keygen -q -t ed25519 -N '' -f /etc/pickle-dev-tls/read_ed25519
openssl req -x509 -newkey rsa:3072 -nodes -subj /CN=dev-tls-backup-recipient -days 3650 -keyout /etc/pickle-dev-tls/recipient.key -out /etc/pickle-dev-tls/recipient.pem
proxmox-backup-client key create /etc/pickle-dev-tls/pbs-key.json --kdf none
```

transport는 `openssl cms -encrypt -binary -outform DER -aes-256-gcm <public-recipient>`와
`openssl cms -decrypt -binary -inform DER -recip <certificate> -inkey <host-private-key>`를
host memory/stdin 파이프로 사용한다. Mac relay는 ciphertext size/SHA만 확인한다.

## 갱신과 timer 후보

CT100 기존 certbot.timer와 generic nginx deploy hook을 그대로 사용한다. 새 reload hook은
추가하지 않는다. 새 lineage의 `certbot renew --cert-name dev.example.com --dry-run`과
nginx 설정 검증을 첫 commissioning 전에 수행한다.

node-restore 새 `pickle-dev-tls-backup.service`는 flock으로 겹침을 막고 pull/검증/업로드/restore
한 번을 수행한다. `TimeoutStartSec=300`, root, UMask=0077, interpreter를 명시한다.
새 `pickle-dev-tls-backup.timer`는 15분 주기/Persistent=true다. protected state는
`/var/lib/pickle-dev-tls/state.json` root:root 0600으로 원자 갱신한다. directory는 0700,
ancestor는 root 소유/쓰기 제한이며 symlink, 다른 소유자, 잘못된 mode/JSON은 실패로 처리한다.
digest는 정확한 9개 member 이름과 각 regular file 내용 SHA, symlink target을 모두 포함한다.
변경이 없고 reader의 현재 snapshot 목록에 이전 검증된 시점의 tls.pxar가 존재하면
새 업로드와 restore를 생략한다. 변경/신규/이전 복구점 소실은 native backup과 exact restore를
모두 통과한 뒤에만 상태를 전진시킨다. 목록 조회/backup/restore 실패는 상태를 바꾸지 않는다.
CMS는 AES-256-GCM이며 node-dev OpenSSL 3.5.6과 node-restore OpenSSL 3.5.7 native dummy 왕복/변조 거부가 2026-10-12
통과했다. dummy 파일은 /run에서 생성·즉시 제거했고 실제 key에는 접근하지 않았다.
초기 발급 직후에는
service를 수동 실행해 첫 복구점이 검증된 다음 공개 TLS를 활성화한다.
실패는 systemd failed 상태와 고정 journald 실패 메시지로 남는다. 성공에는 snapshot과
certificate/CMS SHA만 journald에 기록한다. 설치 인수시험과 운영 점검에서는 최근 성공이
30분을 넘지 않는지 확인한다. worker는 매 실행 source와 이전 복구 검증으로 저장한 fingerprint를 비교한다.
변경·신규·이전 복구점 소실 때만 새 backup과 exact restore를 수행하며,
변경이 없으면 해당 snapshot 존재를 확인한 뒤 새 restore를 생략한다. freshness 자동 감시/외부 알림은 현재 worker에 구현됐다고 주장하지 않는다.
정기 worker `scripts/dev-tls-backup.py`와 timer/service는 위의 frozen 경로와 systemd 경로에
설치됐고 위의 공개 commissioning 뒤 개발 전용 timer만 활성화했다.
worker는 native client의 exact snapshot restore와 CMS 복호화, cert/key 일치를 확인한다.
timer 설치 전 실패/restore 테스트와 별도 복구 사본의 인수시험을 마친다.

## 장애 복구

node-dev/CT100 손실 때 node-restore는 PBS exact snapshot을 위의 reader+key로 복원한다. 안전한
root tmpfs에서 member 검사 후 새 CT의 `/etc/letsencrypt/{archive,live,renewal}`에 해당
lineage만 복원한다. nginx-t와 cert/key 일치를 확인한 뒤 해당 개발 vhost를 활성화한다.

백업에 ACME account private key는 없다. 기존 renewal account가 사라지면 새 ACME account를
등록하고 HTTP-01로 같은 cert-name과 webroot를 사용해 certonly --force-renewal을 실행한다.
기존 renewal config의 account id를 무효인 채 두고 자동 갱신 성공이라고 판단하지 않는다.
새 account로 lineage config가 갱신됐는지 확인한 뒤 dry-run/백업/독립 복원 시험을 다시 한다.
DNS vendor credential은 이 과정에 필요하지 않다.

node-restore 손실 때 node-dev recovery copy에서 새 node-restore의 root 경로로 키/token을 가져오고 PBS client로
동일한 snapshot을 restore한다. 양쪽 손실은 이 두-host custody의 범위를 벗어난다.
기존 기존 core 백업과 운영 PBS 복구 키·job은 어느 복구 단계에서도 수정하지 않는다.

## 적용된 사전 준비의 보존과 rollback

example backup host의 기존 user/ACL section은 동일하고 기존 token.shadow 값 5개도 그대로다.
새 writer token은 최초 native `Result:` 출력 prefix 처리에서 준비가 중단돼 값이
사용되지 않은 상태로 한 번 revoke/reissue했다. 재발급 이후 writer/reader 원문은 example backup host
root 0600 파일에 먼저 보관하고 CMS-GCM으로 node-restore에만 전달했다. 생성된 새로운 자료는
모두 보존한다. node-dev recovery의 7개 파일과 node-restore source의 owner/mode/SHA 및
실제 반환 bytes를 CMS 왕복으로 대조했다. 운영 키/계정/namespace/job은 재사용하지 않았다.

중단 시 새 timer/service만 inactive/disabled 상태로 유지한다. 새 forced reader를 해제해야
하면 신규 authorized_keys2 파일을 root 보호 경로에 보존해 옮기고 기존 authorized_keys는
건드리지 않는다. PBS 접근 해제는 새 user 둘을 disable하고 해당 dev-tls ACL 네 줄만
제거한다. namespace/snapshot/key/token 원본과 두 custody 자료는 삭제하지 않는다.
기존 운영 resource나 서비스 재시작은 rollback에 포함하지 않는다.

## HTTP-01 commissioning의 파일 모드

root 작업 runner의 umask 0077을 HTTP-01 webroot 생성과 Certbot 호출에 그대로 상속하면
새 공개 challenge 디렉터리가 0700이 되어 nginx worker가 읽지 못한다. 최초 발급에서
CA 404가 났고, 새 소유 webroot/challenge 경로를 0755로 정정한 다음 실제 공인 주소의
dummy challenge 일치와 제거를 확인해 재시도했다. Certbot은 명시적으로 umask 0022에서
호출한다. private key는 Certbot이 정한 root 0600인지 별도 검증한다. 신규 live/archive
lineage 디렉터리는 0700, renewal config는 0600으로 보호하며 기존 lineage는 수정하지 않는다.

첫 복구점의 예시 표기는 `host/node-dev-dev-tls/<EXAMPLE_SNAPSHOT_TIME>`다. 실제 9-member
content/symlink 및 cert/key/SAN 비교가 통과했고, dry-run 뒤 worker가 현재 source digest와
동일한 원격 복구점 존재를 확인해 새 snapshot을 만들지 않았다. dry-run의 초기 supervisor는
실패했지만 native 출력의 simulated renewals 성공, 종료된 실제 process, 최종 nginx-t와
기존 파일 보존은 독립 확인했다. supervisor 실패 원인을 확정했다고 주장하지 않는다.

## 실제 공개 commissioning 검증 범위

2026-10-13 공개 console은 검토한 index SHA와 일치했고 `/api/v1/me`는 인증 없이 401,
`/llm/v1/models`와 `/terminal/ws`는 404였다. 기존 default reject를 보존하므로 잘못된
HTTP Host는 연결 종료로 거부되며 HTTP 421이라고 기록하지 않는다. `/v1/models`는
SPA fallback HTML이고 공개 LLM endpoint로 광고하지 않으며 404라고 주장하지 않는다.
새 public TLS leaf의 DER SHA와 PEM SHA는 서로 다른 표현의 hash이므로 구분한다.

선택 이후 CT901의 actual PID/open JAR FD SHA/readiness를 확인했다.
쿠키 인증 시험은 수행하지 않았다. 소스에 정의된 Secure/__Host/HttpOnly 속성과 실제 HTTPS 쿠키
응답의 실측은 다른 증거이며 실행하지 않은 실측을 성공으로 기록하지 않는다.
