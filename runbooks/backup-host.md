# Ubuntu 호스트의 PBS VM과 qnetd

Ubuntu 22.04와 libvirt를 유지하는 dept-node를 대상으로 하는 절차예요. PBS와 qnetd의 신규 설치와 신원 보관, 복구 검증을 다뤄요.
PBS는 Debian 13 전용 VM에, qnetd는 Ubuntu 호스트에 설치해요. 기존 Docker와 관리 IP, 기본 경로,
SSH 정책은 유지해요. 초기 디스크는 boot 64 GiB와 datastore 1 TiB이며 기존 파티션은 바꾸지 않아요.

## 실행 파일과 준비물

| 파일 | 실행 위치와 역할 |
|---|---|
| `scripts/bootstrap-backup-host.sh` | dept-node root. host NetBird와 masked qnetd, PBS 디렉터리 준비 |
| `scripts/check-backup-host.py` | dept-node root. 기존 도구만 사용한 disk/RAID 상태 수집, 변경과 설치 없음 |
| `scripts/check-backup-storage.py` | dept-node root. 공식 패키지에서 추출한 SMART/PERC 바이너리의 hash 검증과 읽기 수집 |
| `scripts/enroll-backup-peer.sh` | dept-node 또는 PBS guest root. 일회용 key 파일로 host-only peer 등록 |
| `scripts/configure-qnetd.sh` | dept-node root. mesh 주소 바인딩 또는 검증한 CSR 서명 |
| `scripts/create-pbs-vm.sh` | dept-node root. 새 파일과 domain만 생성하고 guest bootstrap 실행 |
| `scripts/install-pbs-guest.sh` | 새 PBS VM 내부. PBS 설치, 신규 data disk 초기화와 관리 방화벽 |

스크립트는 기본적으로 사전 검사만 수행해요. 적용하려면 같은 인자에 `--apply`를 더해요.
호스트 이름 검사는 필수이며 기존 domain과 디스크, NSS를 새 대상으로 덮어쓰지 않아요.
파일과 SHA256을 먼저 검토하고 운영자 자신의 SSH 터미널에서 sudo를 실행해요.
sudo 비밀번호는 설정 파일이나 실행 로그에 남기지 않아요. root SSH도 활성화하지 않아요.

필요한 입력은 dept-node의 실제 hostname, root 소유 0700의 빈 backup 디렉터리,
공식 checksum으로 검증한 Debian 13 amd64 generic cloud qcow2와 SHA256, 운영자 SSH 공개키예요.
Cloud image는 backing file이 없는 완전한 이미지여야 해요. VM 안의 SSH는 이 공개키만 허용해요.
이미지 선택 시 [공식 cloud 이미지 목록](https://cloud.debian.org/images/cloud/trixie/)에서
현재 안정판과 checksum을 확인하고 URL과 hash를 실행 기록에 보존해요.

이 절차의 스크립트는 PBS 4.2.5-1, NetBird CLI 0.78.2, Ubuntu qnetd 3.0.1-1을 고정해요.
APT 서명 키를 검증하고, 필요한 정확한 버전이 없으면 다른 버전으로 대신하지 않아요.
[PBS 설치](https://pbs.proxmox.com/docs/installation.html),
[NetBird Linux 설치](https://docs.netbird.io/get-started/install/linux)를 기준으로 다시 확인해요.

## 호스트 준비

1. 첫 root 실행은 상태 수집이에요.

   ```bash
   sudo bash scripts/bootstrap-backup-host.sh --expected-host dept-node --check
   ```

   기존 workload와 `/home` mount, 여유 공간, RAID 상태, cache 보호를 읽어 확인해요.
   `/home`은 ext4이며 PBS data 파일을 둘 위치예요. 기존 `/vm` pool은 data 대상으로 쓰지 않아요.
   종료 코드 0은 수집 완료이며 disk 정상 판정은 아니에요. 기존 PERC/StorCLI가 없으면
   `raid_health=UNKNOWN`으로 표시해요. `smartmontools`가 없어도 설치하지 않고 누락으로 기록해요.
   도구가 없으면 공식 패키지를 일반 사용자 경로에 내려받고 `dpkg-deb -x`로 추출해요.
   패키지를 설치하거나 maintainer script를 실행하지 않아요. Ubuntu APT의 검증된 패키지
   metadata와 Dell 공식 다운로드의 SHA256을 각각 대조하고 실행 파일의 SHA256도 기록해요.
   H755와 Ubuntu 22.04를 명시하는 [Dell PERCCLI 7.2616 A15](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=pdg3h)를 사용해요.
   새 버전이어도 해당 controller와 OS 지원 목록이 다르면 바꾸지 않아요.

   ```bash
   sudo python3 -I scripts/check-backup-storage.py --expected-host dept-node \
     --smartctl /absolute/path/to/smartctl --smartctl-sha256 <sha256> \
     --raid-cli /absolute/path/to/perccli64 --raid-cli-sha256 <sha256> \
     > storage-health.json
   ```

   `umask 077`인 운영자 터미널에서 실행해요. 바이너리는 root 소유의 비공개 `/root` 아래 임시 디렉터리에
   복사한 뒤 hash를 확인하고 실행해요. 실행 전에 해당 filesystem의 `noexec` 여부를
   확인하며 mount 보안 옵션은 바꾸지 않아요. `/run`은 `noexec`일 수 있어 실행 파일의
   사본 위치로 사용하지 않아요. 실행 실패나 시간 초과, 장치 누락이면 JSON을 보존하고
   종료 코드 1과 `collection_complete=false`로 표시해요. `collection_complete=true`도
   수집 완료이며 하드웨어 정상 판정은 아니에요. 읽는 항목은 controller와 virtual disk, physical disk,
   BBU/CacheVault, NVMe SMART예요. Controller 조회는 `noforeign`으로 foreign scan을 피해요.
   SMART 자동 탐색은 ioctl device node를 생성할 수 있어 실행하지 않으며, `/dev/nvme0n1`과
   `/dev/nvme1n1`만 읽어요. 경로가 없으면 누락으로 남겨요. RAID 물리 disk는 PERC의
   상태와 media error, SMART alert로 판정하고 raw SAS SMART attributes는 수집하지 않아요.
   Self-test나 firmware, RAID 구성과 cache 정책 변경은 하지 않아요.
   BBU와 CacheVault 중 미장착 항목의 오류를 전체 disk 실패로 취급하지 않아요. 출력은
   수동 판정 자료이며 명령 실패와 SMART bitmask, 매체 오류, cache 보호를 직접 확인해요.
   실행 종료 때 임시 root 사본을 제거하고, 검토 후 일반 사용자 경로의 진단 묶음도 회수해요.
   RAID와 disk 상태를 판단한 뒤에만 PBS 파일 할당을 진행해요.
2. 새 실행의 backup 디렉터리를 root 소유 0700으로 만들고 다음 사전 검사를 먼저 실행해요.

   ```bash
   sudo bash scripts/bootstrap-backup-host.sh \
     --expected-host dept-node --backup-dir /pickle/backup/dept-node-pbs-preparation
   ```

   `/pickle/backup/...`은 운영자가 정한 이 실행의 보호 위치로 바꿔요. 스크립트는 그곳에
   기존 네트워크와 패키지 목록, libvirt NAT XML, APT, firewall 사본을 남겨요.
3. 검토 뒤 `--apply`로 실행해요. qnetd는 설치 직후 전체 주소에서 시작되지 않도록 먼저 mask해요.
   NSS CA는 패키지가 생성해요. 기존 NSS에 초기화 명령을 다시 실행하지 않아요.
4. dept-node용 일회용 NetBird key를 `/run/`의 root 소유 0600 파일로 전달하고 peer를 등록해요.

   ```bash
   sudo bash scripts/enroll-backup-peer.sh --expected-host dept-node \
     --peer-name dept-node --setup-key-file /run/pickle-dept-node-setup.key --apply
   ```

   성공하면 파일은 삭제돼요. 관리 API에서 setup key도 revoke해요. Admin PAT는 호스트에 두지 않아요.
   Host-only peer는 DNS와 client/server route를 바꾸지 않으며 NetBird 내장 SSH도 켜지 않아요.
   NetBird 설치 전에 `NB_DISABLE_SSH_CONFIG=true` service drop-in을 두어 SSH client 설정도
   자동 편집하지 않게 해요. Debian signing key는 primary fingerprint
   `EFE37DF047DF7CCDF1FC54FA83F79AD029778355`로 검증해요.
5. PVE 두 peer에서 dept-node peer TCP 5403만 허용하고 운영자 관리 접근을 따로 제한해요.
   실제 `wt0` IPv4를 확인한 뒤 `configure-qnetd.sh --mesh-ip <IPv4> --apply`를 실행해요.
   listener가 그 주소에만 있고 TLS/client certificate가 필수인지 확인해요.

Ubuntu qnetd 3.0.1의 daemon CLI는 TLS 필수 값으로 `-s req`를 받아요. 동봉 man page의
`required` 표기와 다르므로 변경 전에 실제 binary의 인자 해석을 검사해요. PVE qdevice의
corosync 설정값 `tls=required`와 구분해요. stderr는 journal에 남기며 최초 시작이
실패하면 반복 재시도를 멈추고 원인을 확인해요.

qnetd는 NetBird 서비스 이후 시작하고 bind 실패를 재시도해요. NetBird peer 준비를 대신하지는
않아요. `netbird status`와 실제 연결, qnetd listener를 함께 확인해요.

## PBS VM

```bash
sudo bash scripts/create-pbs-vm.sh --expected-host dept-node \
  --boot-image /path/to/debian-13-genericcloud-amd64.qcow2 \
  --sha256 <verified-sha256> --ssh-public-key /path/to/operator.pub \
  --uefi-loader /path/from/virsh-domcapabilities/CODE.fd \
  --uefi-vars-template /path/from/qemu-firmware-descriptor/VARS.fd
```

사전 검사 뒤 `--apply`를 더하면 아래 항목만 생성해요.

- `backup-vm`: 4 vCPU, 8 GiB RAM, 자동 시작, MAC `52:54:00:9e:01:10`.
- `/var/lib/libvirt/images/backup-vm/boot.qcow2`: 새 64 GiB UEFI boot disk와 cloud-init seed.
- `/home/libvirt/backup-vm/datastore.raw`: 사전 할당한 새 1 TiB raw disk.
- Libvirt 관리 경로의 guest별 UEFI NVRAM. 정확한 경로는 생성 뒤 `domain.xml`에 기록해요.
- 기존 libvirt default NAT의 DHCP reservation `198.19.122.10`, gateway/DNS `198.19.122.1`.

아래 연결 조합은 원래 검증한 구성의 기록이에요.

Libvirt capabilities의 실제 KVM DAC uid/gid로 디스크와 seed 소유권을 지정하고,
동일 uid/gid에서 read/write 접근을 검사한 뒤 VM을 시작한다. 자동 ownership 변경에 의존하지 않는다.
Firmware 경로는 호출자가 명시한다. 생성기는 loader를 `virsh domcapabilities`의 현재
machine/architecture 지원 목록과 대조하고, loader와 vars template이 같은 QEMU firmware
descriptor의 pair인지 확인한다. 이어 explicit UEFI dry-run XML이 두 경로를 그대로 쓰고
TPM을 추가하지 않는지와 QEMU 계정의 read 권한을 검사한다. cloud-init seed는 IDE CD-ROM이
아니라 read-only virtio block으로 연결한다. Debian 13 genericcloud source, UEFI와 이 연결의
조합만 실측한 것이며 특정 커널 모듈 유무를 원인으로 단정하지 않는다.
Libvirt가 만드는 guest별 NVRAM 경로는 생성 뒤 `domain.xml`에 기록된다. VM 정의와 함께
보존하고 복구 대상으로 취급하며, 실패 정리 때 경로를 확인하지 않고 지우지 않는다.

Guest는 `PBS_DATA` serial과 정확한 1 TiB 크기, 기존 filesystem 부재를 검사한 뒤에만
ext4를 생성해요. Datastore는 UUID로 mount하며 PBS service의 기동 조건에는 mount와 guest firewall이
포함돼요. 초기화가 중단되면 cloud-init 로그와 생성한 두 파일을 확인하고 재개해요.
생성 파일을 자동으로 지우는 rollback은 없어요.

- Domain이 이미 있으면 `create-pbs-vm.sh`를 다시 실행하지 않아요. domain 상태와 disk 경로,
  DHCP reservation을 조회하고 기존 guest에서 실패 원인을 확인해요.
- Domain 생성 전 실패했으면 만들어진 boot/data/seed와 DHCP reservation을 각각 확인해요.
  새 파일에 보존할 데이터가 없음을 증명한 뒤 이 실행의 파일과 reservation만 회수하거나,
  확인한 기존 파일로 domain 생성을 이어가요. 포괄 삭제나 disk 재생성은 하지 않아요.
- Guest 설치의 package 다운로드 등이 실패했으면 기존 `/root/install-pbs-guest.sh`를
  같은 인자로 다시 실행할 수 있어요. 완료 표식이 있어도 저장한 UUID와 실제 disk/mount,
  datastore 이름과 경로가 모두 일치해야 성공으로 판정해요.
- `.chunks`가 생겼는데 datastore 설정에 없으면 설치는 중단돼요. 부분 생성된 빈 디렉터리인지
  기존 backup 데이터인지 확인하고 PBS의 datastore 재사용 절차로 복구해요. 이 상태를
  새 disk로 간주해 mkfs하거나 자동으로 `.chunks`를 삭제하지 않아요.

운영자 공개키로 dept-node SSH jump를 거쳐 `root@198.19.122.10`에 접속해
`cloud-init status --wait`, `/var/lib/pickle-pbs-bootstrap/complete`, `findmnt`, PBS 상태를 확인해요.
Guest의 자체 NetBird peer는 **dept-node key와 다른 일회용 key**로 등록해요.
PVE 두 peer→PBS TCP 8007과 운영자 관리 접근만 허용해요. 공인 8007 port forward는 만들지 않아요.
등록된 PBS mesh 주소와 인증서 fingerprint로 PVE backup storage를 설정하고 전용 최소 권한
token을 사용해요. Encryption key는 PBS VM 밖에도 복구 가능한 형태로 보관해요.

## Root SSH 없는 qdevice 인증서 등록

1. dept-node `/var/lib/pickle/qnetd-public/qnetd-cacert.crt`를 일반 SSH로 가져와 두 PVE에 전달해요.
2. 각 PVE의 `corosync-qdevice-net-certutil -i -c <CA>`로 빈 NSS를 준비해요.
3. pve-node-2에서 `corosync-qdevice-net-certutil -r -n example-prod`를 실행해요.
4. 생성된 `/etc/corosync/qdevice/net/nssdb/qdevice-net-node.crq` 공개 CSR만 dept-node로 전달해요.
   송수신 SHA256을 비교하고 운영자가 아래 명령을 실행해요.

   ```bash
   sudo bash scripts/configure-qnetd.sh --expected-host dept-node \
     --sign-request /path/to/qdevice-net-node.crq --sha256 <verified-sha256> --apply
   ```

5. 출력된 공개 `cluster-example-prod.crt`를 pve-node-2로 옮겨 `-M -c <signed-cert>`로 import해요.
   생성 PKCS12는 private key를 포함해요. PVE root끼리 직접 전달하고 0600으로 보호해요.
   pve-node-3에서는 `-m -c <pkcs12>`로 import하며 임시 PKCS12는 이후 삭제해요.
6. 두 PVE가 online/quorate일 때 `quorum.device`를 `model=net`, `votes=1`,
   `algorithm=ffsplit`, `tls=required`, `host=<dept-node mesh IPv4>`로 등록해요.
   지원되는 PVE lock/atomic-write 경로에서 최신 config_version을 증가시켜요.
7. 두 qdevice service와 `corosync-cfgtool -R` 후 expected/total votes 3, quorum 2와
   TLS client 상태를 확인해요. raw pmxcfs SQLite를 열지 않아요.

6–7단계의 사전 검사, 잠금과 적용 후 검증은 [qdevice 활성화 도구](qdevice-activation.md)를
사용해요. 이 도구는 앞선 인증서 교환을 대신하지 않아요.

## 복구와 완료 확인

### 복구 신원과 설정 보관

복구 사본은 root 소유 0700 디렉터리에 두고 archive 파일은 0600으로 만들어요. 원본 파일의
소유자와 그룹, mode는 manifest에 별도로 기록해요. 보호 archive와 live 서비스 원본의 mode는
다를 수 있어요. Mac으로 가져온 archive도 0600, 추출 디렉터리는 0700으로 두고,
서비스 위치에 다시 놓을 때는 manifest의 원본 권한을 복원해요.

| 대상 | 보존할 내용과 제외 경계 |
|---|---|
| dept-node/backup-vm NetBird 0.78.2 | 실측된 `/var/lib/netbird/default.json`, `/var/lib/netbird/service.json`, `/var/lib/netbird/root/`, `netbird.service`와 drop-in. 다른 profile 경로는 `netbird profile list`와 service ExecStart/Environment에서 실제 사용을 확인한 것만 추가. `/var/lib/netbird/state.json` 같은 동적 runtime state는 identity 사본에서 제외 |
| backup-vm 영구 네트워크 | `/etc/netplan/`과 적용 전후 renderer 결과. DHCP lease나 일시적인 interface state는 대체 자료가 아님 |
| PBS | `/etc/proxmox-backup/datastore.cfg`, `user.cfg`, `token.shadow`, `acl.cfg`, `authkey.key`, `csrf.key`, `proxy.key`/`proxy.pem`과 별도 보관한 backup encryption key. secret 원문은 manifest나 작업 로그에 넣지 않음 |
| dept-node qnetd와 PVE qdevice | host의 `/etc/corosync/qnetd/nssdb/`와 config/unit, 각 PVE의 `/etc/corosync/qdevice/net/nssdb/`와 Corosync 설정. 실행 중인 qnetd NSS의 0640·service group 권한은 정상 동작 조건일 수 있으므로 일괄 0600으로 바꾸지 않음 |
| PBS VM | `virsh dumpxml backup-vm` 결과와 XML이 가리키는 guest NVRAM, boot/data/seed의 경로·format·virtual/logical size, data filesystem UUID·mount와 `/etc/fstab`. XML, NVRAM, immutable cloud source와 seed는 SHA-256을 보존하고, 1 TiB data disk 전체 hash를 요구하지 않음 |

NetBird profile 이름은 추정하지 않아요. `netbird profile list`, service의 ExecStart/Environment와
실제 identity 파일을 함께 대조해 service active profile과 root CLI profile을 구분해요. 복구 후에는
peer ID, 관리 그룹, policy와 route/DNS 비활성 상태를 확인하며 새 peer를 조용히 만드는 것을
복구로 간주하지 않아요. 같은 `default.json` identity를 복원할 때는 기존 peer instance가
정지 또는 폐기 상태이거나 다시 기동되지 않을 상태인지 먼저 확인해요. 한 identity를 두 VM에서 동시에 실행하지 않아요.

### Datastore 설정 분리와 재연결

Datastore 설정을 분리하기 전에 같은 filesystem을 쓰는 다른 PBS writer가 없고 backup과
restore, verify, prune, GC job이 실행 중이지 않은지 확인해요. `/etc/proxmox-backup/datastore.cfg`,
datastore 경로, 정확한 filesystem UUID, namespace와 snapshot owner, index hash를 보호 사본에
남겨요. 경로 일치나 mount 성공만으로 같은 datastore라고 판단하지 않아요. 보존할
backup/verify/prune/GC/sync job의 schedule과 enabled 상태를 기록하고 분리 중에는 실행되지 않게
멈춰요. 재연결 검증 뒤에만 원래 상태로 복구해요.

Data를 보존한 채 설정만 분리할 때는 아래 두 옵션을 모두 명시해요.

```bash
proxmox-backup-manager datastore remove <store> \
  --destroy-data false --keep-job-configs true
```

`proxmox-backup-manager datastore remove`는 내부 worker가 끝날 때까지 기다리는 CLI이므로
background나 pipeline으로 분리하지 않고 exit 0을 확인한 뒤 다음 단계로 가요. 같은 delete API를
직접 호출했다면 반환 UPID를 아래 task status 기준으로 별도 대기해야 해요.

Filesystem UUID와 mount를 다시 확인한 뒤 기존 datastore를 재연결해요. `create` 결과는 설정
완료가 아닌 비동기 worker의 UPID예요.

```bash
umask 077
proxmox-backup-manager datastore create <store> <exact-path> \
  --reuse-datastore true --output-format json > <protected-result.json>
```

Result JSON에서 UPID를 읽고 URL path로 안전하게 encode한 뒤, root의 local API wrapper로
`/nodes/localhost/tasks/<UPID>/status`를 JSON으로 조회해요. `status=stopped`와
`exitstatus=OK`가 함께 나올 때까지 기다려요. 완료 전에 예전 `datastore.cfg`를 덮어써서
되돌리지 않아요. task가 실패하면 현재 config와 filesystem을 그대로 보존하고 task log를 확인해요.
완료 뒤 config와 filesystem UUID, index, snapshot owner를 대조하고 server-side verify를 수행해요.
실제 restore와 SHA-256 비교는 설정 판정과 별도의 마지막 단계예요.

### 최소 권한 파일 백업·복원 검증

검증은 전용 namespace와 임시 user, 그 user의 token 하나만 사용해요. Namespace 생성 CLI의
text renderer가 응답을 그리지 못하고 panic할 수 있어 root에서 API debug CLI의 JSON 출력을 사용해요.

```bash
proxmox-backup-debug api create /admin/datastore/<store>/namespace \
  --name <validation-namespace> --output-format json
```

하위 namespace라면 `--parent <existing-parent>`를 함께 지정해요. `name`에는 마지막 component만
넣고 전체 경로는 넣지 않아요.

명령이 오류를 냈더라도 실제 namespace 목록을 JSON으로 먼저 읽어요. 이미 생성됐다면 재시도해
중복 상태를 만들지 않아요. `proxmox-backup-manager user generate-token`은
`--output-format`을 지원하지 않아요. `Result` JSON에 secret을 한 번만 반환하므로 stdout 전체를
root 소유 0600 파일로 직접 받아야 해요. 기존 경로나 symlink에 redirect하지 않아요. token 값은
터미널과 worklog, Git, 명령 이력에 출력하지 않아요.

User와 token은 같은 `/datastore/<store>/<validation-namespace>` 경로에 각각
`DatastoreBackup` ACL을 받아요. Token 권한은 user 권한과 교집합이므로 둘 중 하나만 주면
검증이 성립하지 않아요. 아래 순서로 범위를 확인해요.

```bash
proxmox-backup-manager user create <validation-user>@pbs --expire <unix-expiry>
umask 077
protected_token_dir=$(mktemp -d /root/pbs-validation.XXXXXXXX)
test "$(stat -c '%u:%a' "$protected_token_dir")" = '0:700'
token_result=$(mktemp "$protected_token_dir/token-result.XXXXXXXX")
proxmox-backup-manager user generate-token <validation-user>@pbs <validation-token> \
  --expire <unix-expiry> > "$token_result"
test -f "$token_result" && test ! -L "$token_result" && test -s "$token_result"
test "$(stat -c '%u:%a' "$token_result")" = '0:600'
proxmox-backup-manager acl update \
  /datastore/<store>/<validation-namespace> DatastoreBackup \
  --auth-id <validation-user>@pbs --propagate false
proxmox-backup-manager acl update \
  /datastore/<store>/<validation-namespace> DatastoreBackup \
  --auth-id '<validation-user>@pbs!<validation-token>' --propagate false
```

1. Result JSON의 secret을 필요한 client의 별도 0600 payload로 한 번만 옮기고 server의 Result JSON을 즉시 삭제해요. Client payload의 owner/mode와 nonempty를 확인한 뒤에만 다음 단계로 가요.
2. 새 8 MiB synthetic 파일과 note를 client-side encryption key로 암호화해 backup해요.
3. pve-node-2와 pve-node-3에서 각각 restore하고 원본 SHA-256과 비교해요.
4. Namespace 밖 backup이 `Datastore.Backup` 부족으로 거부되는지 확인해요.
5. PBS server-side verify가 성공하는지 확인해요.
6. 위 절차로 datastore 설정을 분리하고 재연결한 뒤 다시 restore하고 SHA-256과 snapshot owner를 확인해요.
7. Snapshot과 namespace, 두 ACL(`--delete true`), token(`user delete-token`), user(`user remove`), client payload, 임시 key와 복원 파일 및 `protected_token_dir`을 소유 관계대로 정리하고 목록에서 사라졌는지 확인해요.

Snapshot을 forget해도 참조가 사라진 chunk는 PBS의 기본 GC grace 뒤 정상 GC가 회수해요.
`.chunks` 아래 파일을 수동 삭제하거나 검증을 빨리 끝내려고 grace를 우회하지 않아요.
이 시험은 암호화된 파일 백업과 복원, 권한 경계만 증명해요. VM backup/restore,
database RPO/RTO와 dept-node 물리 host 장애, offsite 사본은 별도 완료 조건이에요.

PBS VM 재부팅 중 host qnetd를 확인하고, 두 PVE 정상 상태에서 qnetd 정지와 복귀를 확인해요.
한 PVE씩 재부팅할 때는 생존 노드+qdevice quorum을 검증해요. qnetd가 없는 동안 추가 PVE를
정지하지 않아요. 이 검증은 dept-node 물리 host 장애나 자동 HA를 입증하지 않아요.

PBS boot를 잃으면 새 boot VM을 만들고 기존 data disk를 재연결해요. UUID mount를 확인해
위 비동기 완료 절차와 `reuse-datastore`로 기존 datastore를 등록해요. 기존 data disk에는
mkfs를 실행하지 않아요.
VM XML과 filesystem UUID, PBS 설정, 암호화 키가 같은 dept-node에만 남지 않도록 보관해요.
같은 datastore에 두 PBS writer를 동시에 붙이지 않아요.

정기 백업 대상은 플랫폼 DB와 core LXC, 설정, 복구 키예요. 사용자 VM 정기 백업은 이 절차의
대상이 아니에요. 소유한 시험 VM만 별도 namespace에서 백업과 복원을 검증해요. DB는 5분
dump 뒤 PBS 저장 완료를 기준으로 freshness를 계산해요. 최근 하루와 일7/주4 보존을 적용하며
실제 restore에서 데이터 시점과 서비스 복귀 시간을 기록해요. Task OK만으로 복원이 완료됐다고
판정하지 않고 복원 데이터의 서비스 검증을 별도로 수행해요. Prune/GC는 승인한 namespace와
retention에만 적용해요.

qdevice rollback은 두 PVE가 online/quorate일 때 `pvecm qdevice remove`를 먼저 실행해요.
이 명령은 PVE의 qdevice NSS도 제거하므로 필요한 복구 사본을 먼저 확인해요. 이후 dept-node qnetd를
멈추고 이 작업의 서비스 설정과 peer 정책만 회수해요. 기존 SSH, libvirt NAT, Docker 또는
공인 방화벽을 복구 명목으로 초기화하지 않아요.

최종 갱신: 2026-11-08
