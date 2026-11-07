# 후보 코어 CT의 PBS 일일 백업

아래는 **pve-node-2**의 CT 1200, 1201, 1202, 1204를 PVE 기본 `vzdump` 작업으로 매일
03:17 KST에 PBS의 `example-core` namespace로 보내는 후보 백업 절차예요. 쓰기 storage는
`pbs-example-core-write`예요. pve-node-3의 읽기 전용 storage는 이 작업에 쓰지 않아요.
DB 백업이나 복원 검증을 대신하지 않아요.

## 등록 전 확인 (pve-node-2 root)

1. `pvecm status`가 `Quorate: Yes`이고 네 CT가 pve-node-2에서 `running`인지 확인해요.
2. 각 `/etc/pve/lxc/{1200,1201,1202,1204}.conf`의 SHA-256을 훅의 고정값과 비교해요.
   설정을 바꿨다면 백업을 멈춘 상태에서 포함 범위를 검토하고 훅의 해시를
   갱신해요. `mp*`의 `backup=0`, bind mount, device mount는 아카이브에 포함되지
   않을 수 있으므로 실제 데이터 경계를 확인해요.
3. `pvesm status --storage pbs-example-core-write`와
   `pvesm config pbs-example-core-write`로 PBS 연결, namespace, pve-node-2에서의 가용성을
   확인해요. PBS에서 token의 쓰기 범위와 encryption key의 별도 복구 사본을 확인해요.
4. `pvesh get /cluster/backup`에서 겹치는 VMID의 기존 작업이 없는지 확인해요.
   `pvesh usage /cluster/backup -v`에서 이 PVE 버전의 옵션을 확인해요.

## 설치와 등록 (pve-node-2 root)

`vzdump`의 임시 디렉터리는 PBS 저장소가 아니에요. CT의 임시 파일 작업에 쓰이는
전용 경로를 먼저 만들고 소유와 ACL을 확인해요.

```bash
install -d -o root -g root -m 0700 /var/lib/vz/dump/candidate-core-tmp
setfacl -m u:100000:--x /var/lib/vz/dump/candidate-core-tmp
stat -c '%U:%G %a %n' /var/lib/vz/dump/candidate-core-tmp
getfacl -cp /var/lib/vz/dump/candidate-core-tmp
install -o root -g root -m 0700 scripts/candidate-core-vzdump-hook.sh \
  /usr/local/sbin/candidate-core-vzdump-hook
```

ACL 적용 뒤 `stat`의 group class는 mask 때문에 `710`으로 보일 수 있어요. `getfacl`에서
owner `rwx`, group/other `---`, `user:100000:--x`를 확인해요. 해당 UID에는 통과
권한만 주며 파일 목록을 읽을 권한은 주지 않아요.

아래 등록 명령은 PVE 9.2의 `pvesh usage` 출력과 대조한 뒤 실행해요. `remove=0`과
`keep-all=1`은 초기 보존 정책이며, PBS 쪽 prune/GC 정책도 별도로 확인해요.

```bash
pvesh create /cluster/backup \
  --id example-core-daily --enabled 0 --node pve-node-2 \
  --schedule '03:17' --repeat-missed 0 \
  --vmid '1200,1201,1202,1204' --storage pbs-example-core-write \
  --mode snapshot --bwlimit 32768 --lockwait 0 \
  --tmpdir /var/lib/vz/dump/candidate-core-tmp \
  --remove 0 --prune-backups 'keep-all=1' \
  --script /usr/local/sbin/candidate-core-vzdump-hook \
  --notification-mode legacy-sendmail
```

`mailto`를 넣지 않아요. 성공 메일은 발생시키지 않으며, 이 호스트의 외부 sendmail
전달은 검증되지 않았어요. 실패와 마지막 성공 시각은 별도 감시에서 판정해요.

## 첫 실행과 판정

`pvesh get /cluster/backup/example-core-daily`로 옵션을 다시 읽고 네 VMID, pve-node-2,
storage, mode, bandwidth, tmpdir, 보존, `repeat-missed`, hook, 알림 값을 대조해요.
작업을 비활성 상태로 둔 채 운영자가 지켜보는 첫 수동 실행을 아래 명령으로 시작해요.
`/nodes/pve-node-2/vzdump`는 등록 작업의 옵션을 상속하지 않으므로 VMID, storage,
snapshot, bandwidth, lockwait, tmpdir, 보존과 hook을 등록값과 대조해 모두 다시 지정해요.

```bash
pvesh create /nodes/pve-node-2/vzdump \
  --job-id example-core-daily \
  --vmid '1200,1201,1202,1204' --storage pbs-example-core-write \
  --mode snapshot --bwlimit 32768 --lockwait 0 \
  --tmpdir /var/lib/vz/dump/candidate-core-tmp \
  --remove 0 --prune-backups 'keep-all=1' \
  --script /usr/local/sbin/candidate-core-vzdump-hook \
  --notification-mode legacy-sendmail
```

task 로그와 네 CT별 결과를 모두 확인해요. PBS에서 네 백업 snapshot의
namespace, 생성 시각, 크기, 소유자를 확인해요. 실제 복원 가능성은 격리 복원 시험으로
따로 입증해요. 성공을 확인한 뒤에만
`pvesh set /cluster/backup/example-core-daily --enabled 1`로 일일 작업을 켜요.
실패한 작업을 성공으로 간주하거나 retention을 줄이지 않아요.

## 초기 보존과 후속 전환

`remove=0`/`keep-all=1`은 첫 정기 실행과 복원 검증을 위한 임시 정책이에요.
PBS가 자동으로 오래된 일일 archive를 지우지 않으므로, 첫 일일 실행부터
7일 안에 CT별 실제 증가량, PBS 예시 서버 datastore의 mount와 여유 공간, 기존 보호
수동본과 새 일일본의 목록을 측정해요. PVE의 namespace-scoped capacity
표시가 0인 상태에서는 그 값을 공간 판단에 쓰지 않아요.

보존 작업을 켜기 전에는 CT별 독립 복원과
[PBS prune dry-run](https://pbs.proxmox.com/docs/maintenance.html)을 먼저 확인해요.
기본 검토안은 일별 7개와 주별 4개이며, `example-core` namespace의
네 CT group에만 적용해요. 보호된 수동 복구점 네 개가 keep 결과와
실제 목록에서 유지되는지 확인하고, DB의 별도 namespace와 5분
data/proof 보존은 건드리지 않아요. GC와 verify는 DB dump가 밀리지 않는
별도 부하 창에서 시험한 뒤 예약해요. 보존 결정을 못 한 채 백업을
무기한 누적하거나 공간 부족을 PVE 표시 0만으로 판단하지 않아요.

## 실패와 되돌리기

새 백업이 실패하면 task의 UPID와 CTID별 결과를 보존하고 기존 보호 복구점과
원본 CT 상태를 먼저 읽어요. 훅의 설정 해시 불일치는 새 설정을 무조건
승인하거나 CT를 다시 시작할 이유가 아니에요. 현재 소유 CT의 변경인지
확인하고 별도 변경 전 수동 백업과 복구 경로를 정한 뒤 훅을 갱신해요.
`lock: backup`이나 임시 LVM snapshot이 남았으면 재실행하지 않고
해당 task와 잠금, 볼륨의 소유를 조사해요. 동일 작업을 자동으로 재시도하지 않아요.

정기 작업을 멈출 때에는
`pvesh set /cluster/backup/example-core-daily --enabled 0`을 먼저 실행하고
`pvesh get /cluster/backup`으로 반영을 확인해요.
진행 중인 task가 없고 소유가 이 작업 하나임을 확인한 경우에만
`pvesh delete /cluster/backup/example-core-daily`로 등록을 회수해요.
전용 hook과 tmpdir는 참조 중인 작업이 없고 경로와 소유, 내용을 다시 확인한
뒤 회수해요. 기존 PBS storage, token, encryption key, 네 보호 수동 archive와
DB 백업 설정은 이 되돌리기에서 지우지 않아요. pmxcfs 설정 DB를 외부
SQLite로 열거나 백업 전 파일로 통째 교체하지 않아요.

훅은 `job-init`에서 네 CT의 원본 설정 해시를 확인해요. `backup-start`에서는 PVE가
선택된 CT 설정에 추가한 `lock: backup` 한 줄만 제외한 해시를 확인하고, 다른 세 CT는
원본 해시를 요구해요. 두 단계 모두 node와 PBS storage, quorum, 실행 상태를 검사해요.
다른 CT, qemu 또는 snapshot 이외 모드로
시작하면 실패해요. 훅 오류가 있으면 task 로그를 보고 설정 변경과 이동, 정지를 먼저
조사해요. pmxcfs의 live `config.db`를 SQLite로 열지 않아요.

최종 갱신: 2026-11-08
