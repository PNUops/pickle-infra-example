# Ubuntu 호스트의 PBS VM과 qnetd

대상은 Ubuntu 22.04와 libvirt를 유지하는 dept-node이다. 아직 설치하지 않은 PBS와 qnetd를
준비하는 절차다. PBS는 Debian 13 전용 VM, qnetd는 Ubuntu 호스트에 설치한다. 기존 Docker, 관리 IP, 기본 경로와 SSH
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

   `umask 077`인 운영자 터미널에서 실행한다. 바이너리는 root 전용 `/run` 임시 디렉터리에
   복사한 뒤 hash를 확인하고 실행한다. 읽는 항목은 controller·virtual disk·physical disk·
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

qnetd는 NetBird 서비스 이후 시작하고 bind 실패를 재시도한다. 이것은 NetBird peer 준비를
대신하지 않는다. `netbird status`, 실제 연결과 qnetd listener를 함께 확인한다.

## PBS VM

```bash
sudo bash scripts/create-pbs-vm.sh --expected-host dept-node \
  --boot-image /path/to/debian-13-genericcloud-amd64.qcow2 \
  --sha256 <verified-sha256> --ssh-public-key /path/to/operator.pub
```

사전 검사 후 `--apply`를 더하면 다음만 생성한다.

- `backup-vm`: 4 vCPU, 8 GiB RAM, 자동 시작, MAC `52:54:00:9e:01:10`.
- `/var/lib/libvirt/images/backup-vm/boot.qcow2`: 새 64 GiB boot disk와 cloud-init seed.
- `/home/libvirt/backup-vm/datastore.raw`: 사전 할당한 새 1 TiB raw disk.
- 기존 libvirt default NAT의 DHCP reservation `198.19.122.10`, gateway/DNS `198.19.122.1`.

Libvirt capabilities의 실제 KVM DAC uid/gid로 디스크와 seed 소유권을 지정하고,
동일 uid/gid에서 read/write 접근을 검사한 뒤 VM을 시작한다. 자동 ownership 변경에 의존하지 않는다.

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

PBS VM 재부팅 중 host qnetd를 확인하고, 두 PVE 정상 상태에서 qnetd 정지와 복귀를 확인한다.
한 PVE씩 재부팅할 때는 생존 노드+qdevice quorum을 검증한다. qnetd가 없는 동안 추가 PVE를
정지하지 않는다. 이 검증은 dept-node 물리 host 장애나 자동 HA가 아니다.

PBS boot를 잃으면 새 boot VM을 만들고 기존 data disk를 재연결한다. UUID mount를 확인해
`reuse-datastore`로 기존 datastore를 등록한다. 기존 data disk에는 mkfs를 실행하지 않는다.
VM XML, filesystem UUID, PBS 설정과 암호화 키가 같은 dept-node만의 사본이 되지 않도록 보관한다.
같은 datastore에 두 PBS writer를 동시에 붙이지 않는다.

정기 백업 대상은 플랫폼 DB, core LXC, 설정과 복구 키다. 사용자 VM 정기 백업은 이 절차의
대상이 아니다. 소유한 시험 VM만 별도 namespace에서 백업과 복원을 검증한다. DB는 5분
dump 후 PBS 저장 완료를 기준으로 freshness를 계산한다. 최근 하루와 일7/주4 보존을 적용하며
실제 restore에서 데이터 시점과 서비스 복귀 시간을 기록한다. Task OK만으로 복원 완료라고
판정하지 않는다. Prune/GC는 승인한 namespace와 retention에만 적용한다.

qdevice rollback은 두 PVE가 online/quorate일 때 `pvecm qdevice remove`를 먼저 실행한다.
이 명령은 PVE의 qdevice NSS도 제거하므로 필요한 복구 사본을 먼저 확인한다. 이후 dept-node qnetd를
중지하고 이 작업의 서비스 설정과 peer 정책만 회수한다. 기존 SSH, libvirt NAT, Docker 또는
공인 방화벽을 원복 명목으로 초기화하지 않는다.
