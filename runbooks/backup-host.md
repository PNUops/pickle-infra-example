# Ubuntu 호스트의 PBS VM과 qnetd

대상은 Ubuntu 22.04와 libvirt를 유지하는 dept-node이다. PBS와 qnetd의 신규 설치, 신원 보관과
복구 검증을 다룬다. PBS는 Debian 13 전용 VM, qnetd는 Ubuntu 호스트에 설치한다. 기존 Docker, 관리 IP, 기본 경로와 SSH
정책은 유지한다. 초기 디스크는 boot 64 GiB와 datastore 1 TiB이며 기존 파티션은 변경하지 않는다.

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

스크립트는 기본적으로 사전 검사만 수행한다. 적용은 같은 인자에 `--apply`를 더한다.
호스트 이름 검사는 필수이고 기존 domain, 디스크 또는 NSS를 새 대상으로 덮어쓰지 않는다.
실행 전 각 파일과 SHA256을 검토하고 운영자 자신의 SSH 터미널에서 sudo를 실행한다.
sudo 비밀번호를 설정 파일이나 실행 로그에 넣지 않는다. root SSH를 활성화하지 않는다.

필요한 입력은 dept-node의 실제 hostname, root 소유 0700의 빈 backup 디렉터리,
공식 checksum으로 검증한 Debian 13 amd64 generic cloud qcow2와 SHA256, 운영자 SSH 공개키다.
Cloud image는 backing file이 없는 완전한 이미지여야 한다. VM 안의 SSH는 이 공개키만 허용한다.
이미지 선택 시 [공식 cloud 이미지 목록](https://cloud.debian.org/images/cloud/trixie/)에서
현재 안정판과 checksum을 확인하고 URL과 hash를 실행 기록에 보존한다.

현재 설치 기준은 PBS 4.2.5-1, NetBird CLI 0.78.2, Ubuntu qnetd 3.0.1-1이다.
APT 서명 키를 검증하며, 필요한 정확한 버전이 없으면 다른 버전으로 조용히 대체하지 않는다.
[PBS 설치](https://pbs.proxmox.com/docs/installation.html),
[NetBird Linux 설치](https://docs.netbird.io/get-started/install/linux)를 기준으로 재확인한다.

## 호스트 준비

1. 첫 root 실행은 상태 수집이다.

   ```bash
   sudo bash scripts/bootstrap-backup-host.sh --expected-host dept-node --check
   ```

   기존 workload, `/home` mount와 여유 공간, RAID 상태 및 cache 보호를 읽어 확인한다.
   `/home`은 ext4이며 PBS data 파일을 둘 위치다. 기존 `/vm` pool을 data 대상으로 쓰지 않는다.
   종료 코드 0은 수집 완료일 뿐 disk가 정상이라는 판정이 아니다. 기존 PERC/StorCLI가 없으면
   `raid_health=UNKNOWN`으로 표시한다. `smartmontools`도 없으면 설치하지 않고 누락을 기록한다.
   도구가 없으면 공식 패키지를 일반 사용자 경로에 내려받고 `dpkg-deb -x`로 추출한다.
   패키지를 설치하거나 maintainer script를 실행하지 않는다. Ubuntu APT의 검증된 패키지
   metadata와 Dell 공식 다운로드의 SHA256을 각각 대조한 뒤 실행 파일의 SHA256도 기록한다.
   H755와 Ubuntu 22.04를 명시하는 [Dell PERCCLI 7.2616 A15](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=pdg3h)를 사용한다.
   새 버전이라도 해당 controller와 OS 지원 목록이 다르면 조용히 바꾸지 않는다.

   ```bash
   sudo python3 -I scripts/check-backup-storage.py --expected-host dept-node \
     --smartctl /absolute/path/to/smartctl --smartctl-sha256 <sha256> \
     --raid-cli /absolute/path/to/perccli64 --raid-cli-sha256 <sha256> \
     > storage-health.json
   ```

   `umask 077`인 운영자 터미널에서 실행한다. 바이너리는 root 소유의 비공개 `/root` 아래 임시 디렉터리에
   복사한 뒤 hash를 확인하고 실행한다. 실행 전에 해당 filesystem의 `noexec` 여부를
   확인하며 mount 보안 옵션은 바꾸지 않는다. `/run`은 `noexec`일 수 있어 실행 파일의
   사본 위치로 사용하지 않는다. 실행 실패·시간 초과·장치 누락은 JSON을 보존하고
   종료 코드 1과 `collection_complete=false`로 표시한다. `collection_complete=true`도
   수집 완료일 뿐 하드웨어 정상 판정이 아니다. 읽는 항목은 controller·virtual disk·physical disk·
   BBU/CacheVault와 NVMe SMART다. Controller 조회는 `noforeign`으로 foreign scan을 피한다.
   SMART 자동 탐색은 ioctl device node를 생성할 수 있어 실행하지 않으며, `/dev/nvme0n1`과
   `/dev/nvme1n1`만 읽는다. 경로가 없으면 명시적 누락으로 남긴다. RAID 물리 disk는 PERC의
   상태·media error·SMART alert로 판정하고 raw SAS SMART attributes는 수집하지 않는다.
   Self-test, firmware, RAID 구성·cache 정책 변경은 하지 않는다.
   BBU와 CacheVault 중 미장착 항목의 오류를 전체 disk 실패로 취급하지 않는다. 출력은
   수동 판정 대상으로 남으며 명령 실패·SMART bitmask·매체 오류·cache 보호를 직접 확인한다.
   실행 종료 때 임시 root 사본을 제거한다. 검토 후 일반 사용자 경로의 진단 묶음도 회수한다.
   RAID와 disk 상태를 판단한 뒤에만 PBS 파일 할당을 진행한다.
2. 새 실행의 backup 디렉터리를 root 소유 0700으로 만들고 다음을 먼저 사전 검사한다.

   ```bash
   sudo bash scripts/bootstrap-backup-host.sh \
     --expected-host dept-node --backup-dir /pickle/backup/dept-node-pbs-preparation
   ```

   `/pickle/backup/...`은 운영자가 정한 이 실행의 보호 위치로 바꾼다. 스크립트는 그 위치에
   기존 네트워크와 패키지 목록, libvirt NAT XML, APT와 firewall 사본을 남긴다.
3. 검토 후 `--apply`로 실행한다. qnetd는 설치 직후 전체 주소에서 시작되지 않도록 먼저 mask한다.
   NSS CA는 패키지가 생성한다. 기존 NSS에 초기화 명령을 다시 실행하지 않는다.
4. dept-node용 일회용 NetBird key를 `/run/`의 root 소유 0600 파일로 전달하고 peer를 등록한다.

   ```bash
   sudo bash scripts/enroll-backup-peer.sh --expected-host dept-node \
     --peer-name dept-node --setup-key-file /run/pickle-dept-node-setup.key --apply
   ```

   성공하면 파일은 삭제된다. 관리 API에서 setup key도 revoke한다. Admin PAT는 호스트에 두지 않는다.
   Host-only peer는 DNS와 client/server route를 변경하지 않으며 NetBird 내장 SSH도 켜지 않는다.
   NetBird 설치 전에 `NB_DISABLE_SSH_CONFIG=true` service drop-in을 두어 SSH client 설정도
   자동 편집하지 않게 한다. Debian signing key는 primary fingerprint
   `EFE37DF047DF7CCDF1FC54FA83F79AD029778355`로 검증한다.
5. PVE 두 peer에서 dept-node peer TCP 5403만 허용하고 운영자 관리 접근을 따로 제한한다.
   실제 `wt0` IPv4를 확인한 뒤 `configure-qnetd.sh --mesh-ip <IPv4> --apply`를 실행한다.
   listener가 그 주소에만 있고 TLS/client certificate가 필수인지 확인한다.

Ubuntu qnetd 3.0.1의 daemon CLI는 TLS 필수 값으로 `-s req`를 받는다. 동봉 man page의
`required` 표기와 다르므로, 변경 전에 실제 binary의 인자 해석을 검사한다. PVE qdevice의
corosync 설정값 `tls=required`와 혼동하지 않는다. stderr는 journal에 남기며 최초 시작이
실패하면 반복 재시도를 중지하고 원인을 확인한다.

qnetd는 NetBird 서비스 이후 시작하고 bind 실패를 재시도한다. 이것은 NetBird peer 준비를
대신하지 않는다. `netbird status`, 실제 연결과 qnetd listener를 함께 확인한다.

## PBS VM

```bash
sudo bash scripts/create-pbs-vm.sh --expected-host dept-node \
  --boot-image /path/to/debian-13-genericcloud-amd64.qcow2 \
  --sha256 <verified-sha256> --ssh-public-key /path/to/operator.pub \
  --uefi-loader /path/from/virsh-domcapabilities/CODE.fd \
  --uefi-vars-template /path/from/qemu-firmware-descriptor/VARS.fd
```

사전 검사 후 `--apply`를 더하면 다음만 생성한다.

- `backup-vm`: 4 vCPU, 8 GiB RAM, 자동 시작, MAC `52:54:00:9e:01:10`.
- `/var/lib/libvirt/images/backup-vm/boot.qcow2`: 새 64 GiB UEFI boot disk와 cloud-init seed.
- `/home/libvirt/backup-vm/datastore.raw`: 사전 할당한 새 1 TiB raw disk.
- Libvirt 관리 경로의 guest별 UEFI NVRAM. 정확한 경로는 생성 뒤 `domain.xml`에 기록한다.
- 기존 libvirt default NAT의 DHCP reservation `198.19.122.10`, gateway/DNS `198.19.122.1`.

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
ext4를 생성한다. Datastore는 UUID로 mount하며 PBS service는 mount와 guest firewall을
기동 조건으로 갖는다. 초기화가 중단되면 cloud-init 로그와 생성한 두 파일을 확인하고
재개한다. 생성 파일을 자동으로 지우는 rollback은 없다.

- Domain이 이미 있으면 `create-pbs-vm.sh`를 다시 실행하지 않는다. domain 상태와 disk 경로,
  DHCP reservation을 조회하고 기존 guest에 접속해 실패 원인을 확인한다.
- Domain 생성 전 실패했으면 만들어진 boot/data/seed와 DHCP reservation을 각각 확인한다.
  새 파일에 보존할 데이터가 없음을 증명한 뒤 이 실행의 파일과 reservation만 회수하거나,
  확인한 기존 파일로 domain 생성을 이어 간다. 포괄 삭제나 disk 재생성은 하지 않는다.
- Guest 설치의 package 다운로드 등이 실패했으면 기존 `/root/install-pbs-guest.sh`를
  같은 인자로 다시 실행할 수 있다. 완료 표식이 있어도 저장한 UUID와 실제 disk/mount,
  datastore 이름과 경로가 모두 일치해야 성공으로 판정한다.
- `.chunks`가 생겼는데 datastore 설정에 없으면 설치는 중단된다. 부분 생성된 빈 디렉터리인지
  기존 backup 데이터인지 확인하고 PBS의 datastore 재사용 절차로 복구한다. 이 상태를
  새 disk로 간주해 mkfs하거나 자동으로 `.chunks`를 삭제하지 않는다.

운영자 공개키로 dept-node SSH jump를 거쳐 `root@198.19.122.10`에 접속해
`cloud-init status --wait`, `/var/lib/pickle-pbs-bootstrap/complete`, `findmnt`, PBS 상태를 확인한다.
Guest의 자체 NetBird peer는 **dept-node key와 다른 일회용 key**로 등록한다.
PVE 두 peer→PBS TCP 8007과 운영자 관리 접근만 허용한다. 공인 8007 port forward는 만들지 않는다.
등록된 PBS mesh 주소와 인증서 fingerprint로 PVE backup storage를 설정하고 전용 최소 권한
token을 사용한다. Encryption key는 PBS VM 밖에도 복구 가능한 형태로 보관한다.

## Root SSH 없는 qdevice 인증서 등록

1. dept-node `/var/lib/pickle/qnetd-public/qnetd-cacert.crt`를 일반 SSH로 가져와 두 PVE에 전달한다.
2. 각 PVE의 `corosync-qdevice-net-certutil -i -c <CA>`로 빈 NSS를 준비한다.
3. pve-node-2에서 `corosync-qdevice-net-certutil -r -n example-prod`를 실행한다.
4. 생성된 `/etc/corosync/qdevice/net/nssdb/qdevice-net-node.crq` 공개 CSR만 dept-node로 전달한다.
   송수신 SHA256을 비교하고 운영자가 다음을 실행한다.

   ```bash
   sudo bash scripts/configure-qnetd.sh --expected-host dept-node \
     --sign-request /path/to/qdevice-net-node.crq --sha256 <verified-sha256> --apply
   ```

5. 출력된 공개 `cluster-example-prod.crt`를 pve-node-2로 옮겨 `-M -c <signed-cert>`로 import한다.
   생성 PKCS12는 private key를 포함한다. PVE root끼리 직접 전달하고 0600으로 보호한다.
   pve-node-3에서는 `-m -c <pkcs12>`로 import하며 임시 PKCS12는 이후 삭제한다.
6. 두 PVE가 online/quorate일 때 `quorum.device`를 `model=net`, `votes=1`,
   `algorithm=ffsplit`, `tls=required`, `host=<dept-node mesh IPv4>`로 등록한다.
   지원되는 PVE lock/atomic-write 경로에서 최신 config_version을 증가시킨다.
7. 두 qdevice service와 `corosync-cfgtool -R` 후 expected/total votes 3, quorum 2와
   TLS client 상태를 확인한다. raw pmxcfs SQLite를 열지 않는다.

6–7단계의 사전 검사, 잠금과 적용 후 검증은 [qdevice 활성화 도구](qdevice-activation.md)를
사용한다. 이 도구는 앞선 인증서 교환을 대신하지 않는다.

## 복구와 완료 확인

### 복구 신원과 설정 보관

복구 사본은 root 소유 0700 디렉터리에 두고 archive 파일은 0600으로 만든다. 원본 파일의
소유자·그룹·mode를 manifest에 별도로 기록한다. 보호 archive의 mode와 live 서비스가 요구하는
원본 mode는 다른 값일 수 있다. Mac으로 가져온 archive도 0600, 추출 디렉터리는 0700으로
두며, 추출 파일을 다시 서비스 위치에 놓을 때는 manifest의 원본 권한을 복원한다.

| 대상 | 보존할 내용과 제외 경계 |
|---|---|
| dept-node/backup-vm NetBird 0.78.2 | 서비스가 사용하는 `/var/lib/netbird/default.json`, 존재하면 `/var/lib/netbird/active_profile.json`, `netbird.service`와 drop-in. root 전용 CLI profile을 실제로 사용했다면 `/root/.config/netbird/`도 별도 보존. `/var/lib/netbird/state.json` 같은 동적 runtime state는 identity 사본에서 제외 |
| backup-vm 영구 네트워크 | `/etc/netplan/`과 적용 전후 renderer 결과. DHCP lease나 일시적인 interface state는 대체 자료가 아님 |
| PBS | `/etc/proxmox-backup/datastore.cfg`, `user.cfg`, `token.shadow`, `acl.cfg`, `authkey.key`, `csrf.key`, `proxy.key`/`proxy.pem`과 별도 보관한 backup encryption key. secret 원문은 manifest나 작업 로그에 넣지 않음 |
| dept-node qnetd와 PVE qdevice | host의 `/etc/corosync/qnetd/nssdb/`와 config/unit, 각 PVE의 `/etc/corosync/qdevice/net/nssdb/`와 Corosync 설정. 실행 중인 qnetd NSS의 0640·service group 권한은 정상 동작 조건일 수 있으므로 일괄 0600으로 바꾸지 않음 |
| PBS VM | `virsh dumpxml backup-vm` 결과, XML이 가리키는 guest NVRAM, boot/data/seed 경로와 hash, data filesystem UUID와 `/etc/fstab`. 원본 cloud image는 별도 immutable source로 보존 |

NetBird profile 이름은 추정하지 않는다. `netbird profile list`, service의 ExecStart/Environment와
실제 identity 파일을 함께 대조해 service active profile과 root CLI profile을 구분한다. 복구 후에는
peer ID, 관리 그룹, policy와 route/DNS 비활성 상태를 확인하며 새 peer를 조용히 만드는 것을
원복으로 간주하지 않는다. 같은 `default.json` identity를 복원할 때는 기존 peer instance가
정지·폐기됐거나 다시 기동되지 않음을 먼저 확인한다. 한 identity를 두 VM에서 동시에 실행하지 않는다.

### Datastore 설정 분리와 재연결

Datastore 설정을 분리하기 전에 같은 filesystem을 쓰는 다른 PBS writer가 없고 backup,
restore, verify, prune, GC job이 실행 중이 아님을 확인한다. `/etc/proxmox-backup/datastore.cfg`,
datastore 경로, 정확한 filesystem UUID, namespace와 snapshot owner, index hash를 보호 사본에
남긴다. 경로만 같거나 mount가 성공한 것은 같은 datastore라는 증거가 아니다. 보존할
backup/verify/prune/GC/sync job의 schedule과 enabled 상태를 기록하고 분리 창에는 실행되지 않게
중지한다. 재연결 검증 뒤에만 원래 상태로 복구한다.

Data를 보존한 채 설정만 분리할 때는 다음 두 옵션을 모두 명시한다.

```bash
proxmox-backup-manager datastore remove <store> \
  --destroy-data false --keep-job-configs true
```

`proxmox-backup-manager datastore remove`는 내부 worker가 끝날 때까지 기다리는 CLI이므로
background나 pipeline으로 분리하지 않고 exit 0을 확인한 뒤 다음 단계로 간다. 같은 delete API를
직접 호출했다면 반환 UPID를 아래와 같은 task status 기준으로 별도 대기해야 한다.

Filesystem UUID와 mount를 다시 확인한 뒤 기존 datastore를 재연결한다. `create` 결과는 설정
완료가 아니라 비동기 worker의 UPID다.

```bash
umask 077
proxmox-backup-manager datastore create <store> <exact-path> \
  --reuse-datastore true --output-format json > <protected-result.json>
```

Result JSON에서 UPID를 읽고 URL path로 안전하게 encode한 뒤, root의 local API wrapper로
`/nodes/localhost/tasks/<UPID>/status`를 JSON 조회한다. `status=stopped`와
`exitstatus=OK`가 함께 나올 때까지 기다린다. 완료 전에 예전 `datastore.cfg`를 덮어써서
원복하지 않는다. task가 실패하면 현재 config와 filesystem을 그대로 보존하고 task log를
확인한다. 완료 뒤 config, filesystem UUID, index와 snapshot owner를 대조하고 server-side
verify를 수행한다. 실제 restore와 SHA-256 비교는 설정 판정과 별도의 마지막 단계다.

### 최소 권한 file-shaped 검증

검증은 전용 namespace, 임시 user와 그 user의 token 하나만 사용한다. Namespace 생성 CLI의
text renderer가 응답을 그리지 못하고 panic할 수 있으므로 root에서 API debug CLI의 JSON
출력을 사용한다.

```bash
proxmox-backup-debug api create /admin/datastore/<store>/namespace \
  --name <validation-namespace> --output-format json
```

하위 namespace라면 `--parent <existing-parent>`를 함께 지정한다. `name`에는 마지막 component만
넣고 전체 경로를 넣지 않는다.

명령이 오류를 냈더라도 실제 namespace 목록을 먼저 JSON으로 읽는다. 이미 생성됐다면 재시도해
중복 상태를 만들지 않는다. `proxmox-backup-manager user generate-token`은
`--output-format`을 지원하지 않는다. `Result` JSON에 secret을 한 번만 반환하므로 stdout 전체를
root 소유 0600 파일로 직접 받아야 한다. 기존 경로나 symlink에 redirect하지 않는다. token 값은
터미널, worklog, Git이나 명령 이력에 출력하지 않는다.

User와 token은 같은 `/datastore/<store>/<validation-namespace>` 경로에 각각
`DatastoreBackup` ACL을 받는다. Token 권한은 user 권한과 교집합이므로 둘 중 하나만 주면
검증이 성립하지 않는다. 다음 순서로 범위를 확인한다.

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
  --auth-id <validation-user>@pbs
proxmox-backup-manager acl update \
  /datastore/<store>/<validation-namespace> DatastoreBackup \
  --auth-id '<validation-user>@pbs!<validation-token>'
```

1. 새 8 MiB synthetic 파일과 note를 client-side encryption key로 암호화해 backup한다.
2. pve-node-2와 pve-node-3에서 각각 restore하고 원본 SHA-256과 비교한다.
3. Namespace 밖 backup이 `Datastore.Backup` 부족으로 거부되는지 확인한다.
4. PBS server-side verify가 성공하는지 확인한다.
5. 위 절차로 datastore 설정을 분리·재연결한 뒤 다시 restore하고 SHA-256과 snapshot owner를 확인한다.
6. Result JSON의 secret을 필요한 client의 별도 0600 payload로 한 번만 옮기고 server의 Result JSON을 즉시 삭제한다.
7. Snapshot과 namespace, 두 ACL(`--delete true`), token(`user delete-token`), user(`user remove`), client payload, 임시 key와 복원 파일 및 `protected_token_dir`을 소유 관계대로 정리하고 목록에서 사라졌는지 확인한다.

Snapshot을 forget해도 참조가 사라진 chunk는 PBS의 기본 GC grace를 거친 뒤 정상 GC가 회수한다.
`.chunks` 아래 파일을 수동 삭제하거나 검증을 빠르게 끝내려고 grace를 우회하지 않는다.
이 시험은 암호화된 file-shaped backup/restore와 권한 경계만 증명한다. VM backup/restore,
database RPO/RTO, dept-node 물리 host 장애와 offsite 사본은 별도 완료 조건이다.

PBS VM 재부팅 중 host qnetd를 확인하고, 두 PVE 정상 상태에서 qnetd 정지와 복귀를 확인한다.
한 PVE씩 재부팅할 때는 생존 노드+qdevice quorum을 검증한다. qnetd가 없는 동안 추가 PVE를
정지하지 않는다. 이 검증은 dept-node 물리 host 장애나 자동 HA가 아니다.

PBS boot를 잃으면 새 boot VM을 만들고 기존 data disk를 재연결한다. UUID mount를 확인해
위 비동기 완료 절차와 `reuse-datastore`로 기존 datastore를 등록한다. 기존 data disk에는
mkfs를 실행하지 않는다.
VM XML, filesystem UUID, PBS 설정과 암호화 키가 같은 dept-node만의 사본이 되지 않도록 보관한다.
같은 datastore에 두 PBS writer를 동시에 붙이지 않는다.

정기 백업 대상은 플랫폼 DB, core LXC, 설정과 복구 키다. 사용자 VM 정기 백업은 이 절차의
대상이 아니다. 소유한 시험 VM만 별도 namespace에서 백업과 복원을 검증한다. DB는 5분
dump 후 PBS 저장 완료를 기준으로 freshness를 계산한다. 최근 하루와 일7/주4 보존을 적용하며
실제 restore에서 데이터 시점과 서비스 복귀 시간을 기록한다. Task OK만으로 복원 완료라고
판정하지 않고 복원 데이터의 서비스 검증을 별도로 수행한다. Prune/GC는 승인한 namespace와
retention에만 적용한다.

qdevice rollback은 두 PVE가 online/quorate일 때 `pvecm qdevice remove`를 먼저 실행한다.
이 명령은 PVE의 qdevice NSS도 제거하므로 필요한 복구 사본을 먼저 확인한다. 이후 dept-node qnetd를
중지하고 이 작업의 서비스 설정과 peer 정책만 회수한다. 기존 SSH, libvirt NAT, Docker 또는
공인 방화벽을 원복 명목으로 초기화하지 않는다.
