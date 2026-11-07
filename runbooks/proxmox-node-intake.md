# 런북: Proxmox 노드 인수 (초안)

플랫폼이 소유한 서버를 **Proxmox 배치 노드 후보**로 편입하는 절차예요. OS 초기화 전 실측과 설치,
이 레포지토리의 기록까지 다뤄요. 설치 절은 첫 실행 전에 작성한 초안이며 검증 상태를 표시해 두었어요.
아래 GPU 실측과 준비 절은 당시 장치와 구성의 기록으로 보존해요. 새 노드에 적용할 조건은 별도로 확인해요.

Proxmox 노드가 **아닌** 호스트의 편입은 [node-intake.md](node-intake.md)를 따라요.
[new-environment.md](new-environment.md)는 빈 호스트에 플랫폼 전체를 세우고, 이 런북은
이미 있는 플랫폼에 노드를 더하며 **노드 등록 직전에 멈춰요**.

아래 두 가지 경계를 지켜요.

- **인수와 설치는 노드를 등록하지 않아요.** `nodes` 행과 OS 카탈로그 행, 풀을 만들지 않아요.
  등록은 [노드 단독 등록](node-registration.md)과 [노드별 이미지 등록](image-registration.md)의
  현재 조건을 따르는 별도 작업이에요. 기존 전역 writer를 새 노드 등록에 쓰지 않도록 아래 "실행 금지" 절도 확인해요.
  ACTIVE로 등록한 `nodes` 행은 신규 배치와 기관의 승인 여유에 영향을 줘요.
  `disk_capacity_gb`가 없으면 실측 전까지 디스크 용량은 미확인이에요(대시보드의
  디스크 막대와 추이).
- **클러스터 참여는 별도 결정이에요.** 설치는 standalone으로 해요. 클러스터 형태가 정해지고
  노드에 게스트가 없을 때 참여해요.

## 1. 초기화 전 실측

벤더 계정으로 읽기만 해요(벤더 이미지에는 비밀번호 계정이 있을 수 있어요). `sshpass`가
있는 pve-node에서 돌리고 비밀번호를 셸 히스토리에 남기지 않아요.
`read -rs SSHPASS; export SSHPASS` 다음 `sshpass -e ssh <account>@<addr> ...`,
끝나면 `unset SSHPASS`로 비워요. 기대한 값 대신 실제 값을 기록해요.

| 항목 | 방법 | 여기서 중요한 이유 |
|---|---|---|
| 보드, BIOS, 시리얼 | `/sys/class/dmi/id/{sys_vendor,product_name,bios_version}`, `dmidecode -t 1,3` (root) | 벤더 이미지의 시리얼이 플레이스홀더라면 UUID와 MAC으로 호스트를 식별하고 README 행에 적어요 |
| GPU | `lspci -D -nn -d 10de:`(벤더 id는 꽂힌 것에 맞춰요), `lspci \| grep -iE 'vga\|3d\|nvidia'`, `nvidia-smi -L`, `ls /sys/kernel/iommu_groups \| wc -l` | GPU가 있으면 설치 전에 BIOS 설정과 드라이버 설치 여부를 정해요(§2). **BDF와 IOMMU 그룹 수를 기록해 §4b와 대조해요.** 커널이 다르면 그룹 수는 참고값이에요 |
| CPU와 ISA 레벨 | `lscpu`, `/lib64/ld-linux-x86-64.so.2 --help \| grep x86-64-v` | Rocky 10 템플릿은 x86-64-v3를 요구해요. 그보다 낮은 노드는 클론을 받아들여도 게스트가 멈춰요 |
| 메모리 | `free -g`, `dmidecode -t 17` (root) | 빈 슬롯을 세고 등록 시점에 용량을 실측해요. `apply-platform-inventory.sh`는 전역 인벤토리용이므로 노드 추가나 증설에는 전용 등록 절차의 조건을 확인해요 |
| 디스크 | `lsblk -o NAME,SIZE,TYPE,ROTA,MODEL`, `nvme smart-log` 또는 `smartctl` (root) | 컨슈머 NVMe 한 장이면 OS와 씬풀이 전원 손실 보호 없는 장치를 공유해요. 두 번째 장치나 엔터프라이즈 장치 사용은 **설치 전에** 결정해요 |
| NIC | `ip -br link`, `/sys/class/net/*/speed` | 미배선 10G 포트를 적고 게스트 VLAN 트렁크와 마이그레이션 트래픽 경로를 검토해요 |
| 대역과 L2 | `ip -br addr`, `ip route`, pve-node에서 `ip neigh show <addr>`와 `ping -c 1000 -i 0.01 -q <addr>` | pve-node와 같은 L2면 게스트 VLAN 확장은 스위치 설정에도 영향을 줘요. 평균만 보지 말고 max와 mdev도 적어요 |
| BMC | `ls -l /dev/ipmi0`, `ipmitool mc info`, `ipmitool lan print 1`, `ipmitool user list 1` (root) | 아래 세 가지를 확인해요. BMC가 쓰는 포트가 네트워크에 물려 있는가(**NIC selection을 먼저 읽고, `IP Address Source`가 DHCP일 때만 답을 얻을 수 있어요.** 0.0.0.0이면 임대를 못 받은 것이고, 주소가 있으면 적어도 임대 시점에는 배선됐다는 뜻이에요. DHCP가 아니면 이 명령으로는 알 수 없으므로 물리적으로 확인해요), 어떤 사용자가 있는가, 이 호스트의 누가 `/dev/ipmi0`에 닿는가. 기본 자격증명을 로그인으로 확인하지 않아요 |
| sshd, 리스너, 시각 | `ss -tlnup`, `/etc/ssh/sshd_config*`, `timedatectl` | 초기화 전 스냅샷이에요. 초기화하면 모두 바뀌어요 |
| 호스트 키 | `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub` | 설치 후 새 키인지 확인할 수 있도록 기록해요 |
| 주소 소유 | 네트워크 담당자에게 확인 | 벤더 이미지가 들고 있는 캠퍼스 주소가 이 호스트에 고정 할당된 것인지 DHCP 임대인지. 설치 시 static으로 지정하므로 이 호스트에 할당된 주소여야 해요 |

결과는 초기화 전 스냅샷임을 명시해 인수와 같은 작업 단위에 기록해요. 설치 후에는 상태가 달라져요.

벤더 OS에 `ipmitool`이 있을 때 BMC `admin` 비밀번호 변경을 검토해요
(`/dev/ipmi0` 경유 `ipmitool user set password <id>`, root). 초기화 뒤에는 `apt install ipmitool`부터 필요해요.

아래 두 자료도 확인해요.

- **FRU**. `ipmitool`이 이미 있으면 확인해요. `dmidecode`의 섀시와 제품, 자산 시리얼이
  플레이스홀더여도 `ipmitool fru`에는 실제 보드 시리얼과 제조일이 있어요. FRU는 BMC 자체 저장소에
  있어 초기화로 **사라지지 않아요**. 나중에 `apt install ipmitool`로 다시 확인할 수도 있어요.
- **파티션 테이블**. 초기화하면 사라져요. `sfdisk -d /dev/<disk>`로 벤더 출고 GPT를 재현할 수 있는
  텍스트를 보관해요. 베어메탈 복원 때 추측으로 대신할 수 없는 자료예요.

## 1b. 납품 상태 보존

벤더 OS를 보존할지 정하고 아래 세 방법의 보존 범위와 비용을 구분해요.

**첫째, 벤더에게 원본 이미지를 요청해요.** 맞춤 설치 이미지(Cubic 빌드나 OEM preseed)의
빌드 날짜는 `/etc/lsb-release`에서 확인해요. 블록 복사 전에 벤더가 원본 ISO를 보관하는지 물어봐요.
"없다"는 답을 받아도 다른 방법을 준비할 수 있도록 일찍 확인해요.

**둘째, 텍스트를 수집해요.** 패키지 목록과 활성 유닛, 네트워크 설정, 펌웨어 버전, 계정, FRU,
파티션 테이블을 남기면 나중 상태와 대조할 수 있어요. 블록 이미지는 "정확히 이것을 다시 부팅한다"는
복원 목적이 있을 때 필요해요.

**셋째, 블록 이미지가 필요하면 사용 블록을 복사해요.** 갓 설치한 벤더 OS는 테라바이트 장치에서
수십 기가바이트만 사용하므로 장치 전체를 복사할 필요는 없어요.

**파티션 테이블과 ESP, 루트 파일시스템이 모두 필요해요.** 하나라도 빠지면 아래 절차로 복원할 수 없어요.
장치를 `lsblk`로 확인해 `<disk>`와 파티션 자리에 넣어요. 아래 예시는 NVMe 한 장 구성에만 맞아요.

라이브 미디어에서 오프라인으로 복사해요(SystemRescue와 Clonezilla 모두
`partclone`과 `zstd`를 포함해요). 받는 호스트에는 충분한 공간이 있어야 해요.

```bash
sfdisk -d /dev/<disk> > gpt.txt                       # 산출물 1
dd if=/dev/<disk>p1 bs=1M | zstd -T0 | ssh <target> 'cat > esp.img.zst'      # 2
partclone.ext4 -c -s /dev/<disk>p2 | zstd -T0 | ssh <target> 'cat > root.pcl.zst'  # 3
```

라이브 미디어 없이 온라인으로 복사하는 방법은 곧 지울 호스트에만 사용해요. 순정 벤더 이미지에는
`partclone`이 **없어** 설치부터 해야 하며, 그 설치가 보존하려던 상태를 바꿔요. 사용 블록 맵을 읽은 뒤
복사하는 동안 데이터가 바뀔 수 있어 여러 시점이 섞이고 저널 재생으로도 고쳐지지 않아요.
멈출 수 있는 작업을 멈추고 `sync`한 뒤, 결과를 정확한 사본으로 간주하지 않아요.

```bash
sudo apt install partclone
sudo sfdisk -d /dev/<disk> > gpt.txt
sudo dd if=/dev/<disk>p1 bs=1M | zstd -T0 | ssh <target> 'cat > esp.img.zst'
sync
sudo partclone.ext4 -c -s /dev/<disk>p2 --force | zstd -T0 | ssh <target> 'cat > root.pcl.zst'
```

`--force`는 마운트된 소스를 읽게 하지만 복사 중 읽기 오류로 종료하는 동작도 억제해요.
온라인 이미지는 불완전할 수 있어요. 어느 방법으로 복사했든 복원한 파일시스템은 `e2fsck -f`로 확인해요.

복원할 때는 GPT를 쓰고 ESP를 `dd`로, 루트를 `partclone.ext4 -r`로 되돌린 뒤
Proxmox 설치 프로그램이 바꾼 EFI 부트 항목을 고쳐요. **디스크에 쓰는 작업이므로 매 단계 전에
`lsblk`로 대상을 확인해요.** 백업과 달리 복원은 기존 데이터를 파괴해요.

**이미지는 소스에서 압축해요.** 압축하지 않으면 장치 전체가 네트워크를 건너요.
**이미지에는 벤더 계정의 비밀번호 해시와 디스크의 모든 키가 들어 있어요.** 플랫폼 소유 호스트에 보관하고
어떤 레포지토리에도 넣지 않아요. 위치와 체크섬은 이미지 옆이 아닌 이 README의 해당 호스트 `## 운영 대상` 행에 적어요.

## 2. 설치 전에 운영자가 정할 것

설치 프로그램을 실행하기 전에 아래 항목을 정해요. 설치 값과 배선 대상이 달라져요.

1. **스토리지 구성.** 두 번째 장치나 엔터프라이즈 NVMe는 설치 전에 꽂는다. 설치 프로그램의
   디스크 값(`swapsize`, `maxroot`, `minfree`, `maxvz`)을 프롬프트에서 기록한다. pve-node는
   그것을 기록하지 않았고 VG에 16 GiB만 남은 채로 끝났다. 스토리지 id는 등록 스크립트가
   기본값으로 쓰는 `local-lvm`을 유지한다.
2. **관리 NIC와 10G.** 게스트 VLAN 트렁크가 10G 포트를 탈 것이면 그쪽을 먼저 배선하고 1G
   포트를 관리용으로 골라요.
3. **호스트명.** 이 이름이 pveproxy 인증서 SAN이자 api가 고정하는
   `api_host`가 돼요. 등록 뒤 이름을 바꾸려면 인증서와 데이터베이스도 바꿔야 해요.
4. **BMC.** 전용 포트를 배선하기 **전에** `admin` 비밀번호를 바꿔요. DHCP면 링크가 올라오는
   순간 BMC가 캠퍼스 대역에 나타나요. 관리 네트워크를 먼저 정하고 배선해요.
5. **BIOS와 GPU.** GPU가 있는 호스트에서 패스스루를 언젠가 쓸 생각이면 설치 프로그램을
   띄우기 전에 BIOS 선택을 끝내야 하고(VT-d/IOMMU, Above-4G decoding, 제공되면 SR-IOV,
   Secure Boot 끄기) 첫 부팅부터 vfio-pci가 카드를 먼저 잡는 설정이 필요해요(§4c. 커널
   커맨드라인이 아닌 modprobe.d로 해요). 용도가 정해지지
   않았으면 IOMMU는 켠 채로 두고 드라이버는 설치하지 않아요. 카드에 바인딩된 드라이버가
   나중에 패스스루할 때 해제해야 할 대상이에요. **설치 뒤에도 카드가 열거되는지 확인해요.**
   아래 4b를 확인해요.
6. **클러스터 형태.** 설치 때는 필요 없지만 참여 전에는 정해야 해요. 참여하려면 pve-node와 Proxmox
   메이저 버전이 같아야 해요(`pveversion`).

## 3. Standalone 설치 (미검증, 첫 실행 전에 작성)

1. pve-node와 같은 메이저의 Proxmox VE 설치 프로그램으로 부팅해요. 위에서 정한 디스크 구성,
   호스트명, 1G 관리 인터페이스, 그리고 §1에서 이 호스트의 것으로 확인한 캠퍼스 주소를
   넣어요.
2. 첫 부팅에서 리포지토리를 pve-node와 같게 맞추고(enterprise와 Ceph 목록 비활성화,
   `pve-no-subscription` 활성화, [new-environment.md](new-environment.md) 참조),
   `timedatectl`이 pve-node가 쓰는 소스에 동기화되었는지 확인하고(pve-node에서 `chronyc sources`로
   읽어요), `pveproxy`가 `:8006`에 응답하는지 확인해요.
3. 운영자 키를 먼저 준비해요. 노드마다 ed25519 키 하나, 주석 `pickle-node-<name>`,
   [node-intake.md](node-intake.md) §2가 설명하는 대로 운영자의 암호화된 키 보관소에
   보관해요. `~/.ssh/config` 항목에는 pve-node 경유 ProxyJump를 사용자와 포트, 키까지 명시해요.
   캠퍼스 주소는 개발 머신에서 직접 닿지 않아요. 다른 세션에서 확인해요.
   `ssh -o BatchMode=yes <name> hostname`
4. 그다음 sshd를 설정해요. `root`는 키 전용, `PasswordAuthentication no`. 증명에 쓴 세션을 열어
   둔 채로 다른 세션에서 확인될 때까지 유지해요([new-environment.md](new-environment.md)가
   포트 이동을 안내하는 방식과 같아요). 포트를 22로 둘지 pve-node처럼 옮길지는 호스트별
   선택이고, 이 README의 `## 운영 대상` 행에 적어요. 이후 명령은 호스트 기록에서
   포트를 읽어요.
5. 방화벽. pve-node의 `interfaces` 파일을 통째로 복사하지 **않아요**. 그 파일의 8006 규칙은 인프라
   브리지만 제한하고(`-i vmbr1`), 이 예시의 캠퍼스 쪽 pve-node 8006은 캠퍼스 대역 전체에서
   닿는 구성이에요. 인프라 브리지가 없는 새 호스트에 그대로 복사하면 같은 허용 범위가 돼요. 여기서
   그 허용 범위를 명시적으로 정해요. 노드가 등록되면 pve-node의 api가 캠퍼스 네트워크
   너머로 이 8006에 닿아야 한다는 점도 고려해요.
6. `vmbr2`를 아직 만들지 않아요. 이 호스트의 게스트 브리지는 VLAN 트렁크나 SDN 존이 이어
   주기 전까지 pve-node의 것과 별개 L2예요. 일찍 만들면 풀이 잘못된 네트워크를 가리킬 수 있어요.
7. 노드가 클러스터에 참여할 수 있으면 템플릿을 아직 빌드하지 않아요. 참여하는 노드는
   게스트를 갖고 있으면 안 돼요.

## 4. 새 노드 등록에 쓰면 안 되는 작업

아래 두 레거시 스크립트는 새 노드 **위에서는** 실행할 수 없어요(거기에 LXC 100과 101이 없어요).
pve-node에서 `PICKLE_NODE=<새 노드>`로 실행해도 노드 단독 등록을 대신하지 못해요.

- `apply-platform-inventory.sh`는 환경 전체를 등록하는 writer예요. 새 노드를 pve-node의 풀에 묶을 수 있으므로
  노드 추가에 사용하지 않아요.
- `apply-os-catalog.sh`를 `PICKLE_NODE=<새 노드>`로 실행하지 않아요. 이 writer는 레거시 전역 제약에서만
  실행하고 그 밖의 스키마는 거부해요. 대상 이름과 버전이 일치하는 기존 행을 새 노드로 옮길 수 있으며
  상태는 유지해요. ACTIVE 행이 옮겨지면 해당 노드로 배치한 VM의 클론이 실패할 수 있어요
  (이 예시의 템플릿 VMID는 pve-node에 있어요).
- `nodes`에 손으로 insert하지 않아요. 이미 insert했다면 즉시 MAINTENANCE로 PATCH해요.
- 클러스터 형태가 정해지기 전의 pve-node `pvecm create`나 아무 곳에서의 `pvecm add`. pve-node에서
  `pvecm create`를 처음 돌리기 전에, 클러스터 생성이 pveproxy 인증서나 CA를 재생성하는지
  버려도 되는 호스트에서 먼저 확인해요. api가 pve-node의 CA를 고정하고 있어서 그것이 바뀌는
  순간 Proxmox API 연결이 끊겨요.

## 4b. 웜 리부트 뒤에 열거되지 않는 GPU

컨슈머 NVIDIA 카드(RTX 5090)가 꽂힌 노드에서 2026-09-02에 겪었고, 2026-09-07에 서버실
현장에서 원인과 복구 경로를 확정했다. 드라이버 문제나 설치 실패로 오인하기 전에 알아 둘
것이 있다.

**증상.** 웜 리부트 뒤 카드가 `lspci`에서 통째로 사라지고 IOMMU 그룹 수가 준다(카드의 그룹과
그 슬롯 루트 포트의 그룹, 둘). "드라이버 없음"이 아니라 그 버스 주소에 장치가 없다. **슬롯의
루트 포트 자체가 사라진다.** 그래서 `dmesg`에 PCIe 오류가 없고 `echo 1 > /sys/bus/pci/rescan`으로도
돌아오지 않는다. 다시 훑을 포트가 없다. POST에서 링크가 안 뜬 슬롯의 포트를 BIOS가 숨기는
것으로 읽히지만 그것은 해석이다.

**원인은 `nouveau`가 카드를 잡고 있는 채로 웜 리부트에 들어가는 것이다.** 웜 리셋 자체가
아니다.

| 웜 리부트에 들어갈 때 카드를 잡고 있던 것 | 웜 리부트 뒤 카드 | 횟수 |
|---|---|---|
| 없음(NVIDIA 드라이버가 잡았다 놓은 뒤 언로드, 오디오 기능 unbind) | 있음 | 1 |
| NVIDIA 610 open kernel module | 있음 | 4 |
| `nouveau`(GSP 펌웨어까지 초기화) | **없음** | 2 |
| vfio-pci(부팅 시 바인딩, D3hot, 게스트 미기동, 2026-09-08) | 있음 | 2 |
| vfio-pci, NVIDIA 게스트가 쓰고 정상 종료한 뒤(FLR 완료, D3hot, 2026-09-08) | 있음 | 1 |

같은 날 **게스트 안**에서도 재현했다(호스트는 vfio-pci로 카드를 잡은 채였다). 게스트 커널 7.0.0-31의
`nouveau`가 GSP로 카드를 초기화한 뒤 그 VM을 `qm shutdown` 하자 호스트의 FLR이
`not ready 65535ms after FLR; giving up`으로 끝나고 vendor id가 `0001`로 읽혔다. 이어진 `qm start`가
걸려 호스트가 응답 불능이 됐고 복구는 S5였다. 반면 NVIDIA 610 드라이버 게스트는 리셋 12회(게스트 재부팅, 종료와 기동, `qm reboot`, 강제 종료 각 3),
인계 10회, 부하 중 강제 종료 1회 전부 무사했다. 그러니 이 표의 조건은 「호스트가 잡고 있는가」가 아니라
**「리셋 시점에 카드를 초기화해 둔 것이 nouveau인가」**다. 게스트 쪽 절차와 결과는
[gpu-passthrough.md](gpu-passthrough.md).

NVIDIA 드라이버를 깔기 전의 리눅스 커널은 NVIDIA 카드를 보면 `nouveau`를 자동으로 붙이고, 이
세대의 카드에서 `nouveau`는 GSP 펌웨어까지 올려 카드를 완전히 기동시킨다(`dmesg`의
`gsp: RM version` 줄). 그 상태로 웜 리셋에 들어가면 카드가 다음 POST의 열거에 응답하지 않는다.
NVIDIA 드라이버가 한 번 잡았다 놓은 카드는 견딘다. 그 부팅에서 앞서 `nouveau`가 초기화했더라도
그렇다(첫 행이 그 경우다). **어떤 드라이버도 닿지 않은 부팅에서의 재부팅은 안 쟀다.** 2026-09-08에 가장 가까운 것을 쟀다: vfio-pci가 부팅 때 잡고 게스트는 한 번도 열지 않은 상태(D3hot)의 웜 리부트 2회, 표의 아래 두 행이다.

**복구는 S5로 된다.** `systemctl poweroff` 뒤 전원 버튼이고, 꺼져 있던 시간이 24초여도
됐다(2회, 65초와 24초). AC 차단은 필요 없다. BMC가 결선된 노드라면 `chassis power cycle`이
같은 것을 원격으로 한다. 종전에 「BMC 전원 제어는 대기 전원을 유지하므로 소용없다」고 적었던
것은 추론이었고 실측으로 틀렸다.

**언제 만나는가.** `nouveau`가 잡고 있는 채로 하는 웜 리부트다. 설치 매체 부팅 뒤 첫 부팅,
설치 직후 첫 재부팅, blacklist 파일을 잃은 뒤의 재부팅. NVIDIA 드라이버가 깔리고 `nouveau`가
blacklist된 노드는 평소 재부팅에서 이 문제를 만나지 않는다. 커널 업그레이드 뒤 DKMS 빌드가
실패하면 아무 드라이버도 안 붙은 채 부팅하는데, 그 상태의 재부팅은 위 표에 없다.
`nouveau`가 아니므로 위험 조건은 아니지만 실측은 아니다.

**편입 때 할 것.**

- 설치 뒤에 카드가 그대로인지 본다. `lspci -D -nn -d 10de:`(꽂힌 것의 벤더 id로 바꾼다)와
  `ls /sys/kernel/iommu_groups | wc -l`을 §1에서 적어 둔 값과 견준다.
- **설치 직후 첫 부팅은 `nouveau` 상태다.** 이 상태에서 웜 리부트하면 카드를 잃는다. 순서를
  지킨다. NVIDIA 드라이버 설치와 `nouveau` blacklist, `modprobe -r nouveau`까지를 재부팅 없이
  먼저 한다(2026-09-02에 재부팅 없이 됐다). 그 다음에 재부팅한다. 재부팅 전에
  `lspci -nnk -s <bdf> | grep 'driver in use'`가 `nouveau`가 아닌 것을 확인한다.
- 그 순서를 지킬 수 없으면(설치 매체 부팅 직후처럼) 카드를 잃을 것을 알고 재부팅하고, 뒤에
  S5로 되살린다. 사람이 서버실에 있을 때 하거나 BMC를 결선한다.
- 사라졌으면 rescan에 시간을 쓰지 말고 S5로 간다.
- 커널을 올릴 때 `proxmox-headers-<새 커널>`이 함께 안 깔린다. 헤더를 넣고
  `dkms autoinstall -k <새 커널>`로 빌드한 뒤 재부팅한다. 안 하면 새 커널에서 GPU를 못 쓴다.
- **어느 쪽이든 결과를 [README](../README.md)의 `## 운영 대상` 행에 적는다.** "쟀을 때 됐다"와
  "재부팅을 견딘다"는 다른 주장이고, 계획을 떠받치는 것은 둘째뿐이다.

**안 잰 것.** 운용에 필요하지 않아 남겨 둔 것이고, 원인을 더 좁히려면 이쪽이다.
`nouveau`를 `modeset=0`으로 붙인 뒤의 웜 리부트(GSP가 올라오는지부터), 어떤 드라이버도
닿지 않은 부팅에서의 웜 리부트, 그리고 보드 펌웨어를 갱신한 뒤 `nouveau` 상태의 웜 리부트가
견디는지. 카드를 잃을 수 있는 시험은 S5를 누를 사람이 있을 때만 한다.

**패스스루에 주는 함의.** 호스트 층에서 본 것은 「정리되지 않은 채 남은 카드는 리셋을
못 견딘다」이고, 게스트 재부팅에서 같은 모양이 나올 수 있다. vfio 경로에서 카드가 어떤
상태로 리셋에 들어가는지는 패스스루 개발 라운드가 재야 한다. 그때 견줄 기준은 위 표다.

## 4c. GPU 패스스루 준비 (pve-node-3, 2026-09-07)

> 이 절은 호스트가 카드를 잡지 않게 하는 준비까지다. 매핑을 만들고 VM에 붙이고 떼는 절차, 토큰 권한,
> 게스트 요건은 [gpu-passthrough.md](gpu-passthrough.md)에 있다(2026-09-08 신설).

GPU가 있는 노드를 VM 패스스루용으로 두는 호스트 설정이다. §2의 5번이 「설치 전에 정할
것」으로 적어 둔 것의 실행이고, pve-node-3에서 2026-09-07에 처음 걸었다. 설정 파일은
`hosts/pve-node-3/`에 있고 노드에 이 레포의 체크아웃이 없으므로 사람이 복사해 적용한다.

**전제.** 전부 pve-node-3에서 2026-09-07 실측한 값이고 새 노드에서는 다시 잰다.

| 항목 | 확인 명령 | pve-node-3 |
|---|---|---|
| UEFI, Secure Boot 꺼짐 | `ls /sys/firmware/efi`, `mokutil --sb-state` | UEFI, disabled |
| IOMMU 켜짐 | `ls /sys/kernel/iommu_groups \| wc -l` | 125 (커널 파라미터 없이. 이 커널은 기본 켜짐이다) |
| 카드가 자기 기능만으로 한 그룹 | `ls /sys/kernel/iommu_groups/<n>/devices/` | 그룹 13에 `43:00.0`과 `43:00.1`뿐 |
| 인터럽트 리매핑 | `dmesg \| grep DMAR-IR` | 있음. 없으면 VM이 그룹을 붙일 때 EPERM이고 `allow_unsafe_interrupts`를 따로 판단한다 |
| 카드가 부팅 VGA가 아님 | `cat /sys/bus/pci/devices/<bdf>/boot_vga` | 0 (ASPEED `02:00.0`이 1). BIOS 설정 하나로 바뀌는 값이다 |
| Above 4G decoding | `lspci -vvs <bdf> \| grep Region` | BAR1이 `0x202fe0000000`, 4 GiB 위에 있다 |
| 리셋 방법 | `cat /sys/bus/pci/devices/<bdf>/reset_method` | `flr bus` |

그룹에 다른 장치가 섞여 있으면 그 장치도 함께 넘어가야 하므로 여기서 멈추고 슬롯을 바꾼다.

**호스트가 카드를 잡지 않게 한다.** 방침은 「vfio-pci가 부팅 때 먼저 잡는다」이다. 호스트의
NVIDIA와 CUDA 패키지는 VM 패스스루에 필요하지 않다. 호스트 GPU 시험 패키지를 제거할 때는
실행 중인 소비자와 패키지 의존성을 확인하고 정확한 제거 목록을 시뮬레이션한다. `autoremove`로
범용 관리 도구까지 지우지 않는다. 별도 blacklist와 VFIO 설정, IOMMU 부팅 옵션은 보존한다.

VFIO 설정은 드라이버 패키지의 설치 여부와 독립적으로 유지한다. 커널 커맨드라인에
`vfio-pci.ids=`를 적는 방법도 있지만 여기서는 쓰지 않는다. `cat /proc/cmdline`에 vfio가 없는
것이 정상이다.

1. `hosts/pve-node-3/modprobe.d/vfio.conf`를 `/etc/modprobe.d/vfio.conf`로,
   `hosts/pve-node-3/modules-load.d/vfio.conf`를 `/etc/modules-load.d/vfio.conf`로 복사한다.
   `hosts/pve-node-3/modprobe.d/blacklist-nouveau.conf`도 같은 이름으로 `/etc/modprobe.d/`에 둔다.
   nouveau와 nova_core 차단을 NVIDIA 패키지의 설정 파일에 의존하지 않게 한다. VFIO 파일은
   장치 id로 vfio-pci를 지정하고 `softdep`으로 nvidia, nouveau, snd_hda_intel보다 먼저 올라오게
   한다. 어느 경로로 그 드라이버가 올라오든 libkmod가 `softdep pre`를 처리하고, 한 번 붙은
   장치는 다른 드라이버가 빼앗지 못하므로 순서 경쟁이 없다. 카드가 다르면 `lspci -nn`의
   `[vendor:device]` 둘(VGA와 오디오)로 id를 바꾼다.
2. `/etc/default/grub`의 `GRUB_CMDLINE_LINUX_DEFAULT`에 `intel_iommu=on iommu=pt`를 더한다.
   IOMMU는 이 커널에서 기본으로 켜져 있어(위 표) 앞엣것은 의도를 적는 것이고, `iommu=pt`는 호스트
   장치의 DMA 변환을 건너뛰는 성능 선택이다. 바꾸기 전 사본을 `/root/prep-backup/`에 둔다.
3. `update-initramfs -u -k all`과 `update-grub`. **initrd 단계는 이 호스트에서 바인딩에 관여하지
   않는다.** initrd에는 카드를 잡을 드라이버가 하나도 없고(`lsinitramfs /boot/initrd.img-$(uname
   -r) | grep -E 'nouveau|nvidia|vfio'`가 설정 파일 셋만 보여 준다. nouveau는 initrd에 없고
   DKMS 모듈은 initramfs-tools가 복사하지 않는다) 바인딩은 루트 전환 뒤 `systemd-modules-load`가
   vfio_pci를 `ids` 옵션으로 올리는 순간 일어난다. `update-initramfs`는 initrd 안의 modprobe.d
   사본을 같은 내용으로 두기 위한 것이다. 조기 바인딩이 정말 필요해지면(initrd에 카드를 잡는
   드라이버가 들어가는 구성) `/etc/initramfs-tools/modules`에 `vfio_pci`를 넣어야 한다.
4. NVIDIA host 패키지가 설치된 경우 `systemctl disable --now nvidia-persistenced`를 실행한다.
   패키지를 제거한 호스트에는 이 유닛이 없을 수 있다. 카드가 없으면 쓸모없고 nvidia 모듈을
   올려 두게 하므로 설치된 경우에만 끈다.
5. 재부팅 전에 동적으로 확인할 수 있다. `nvidia-persistenced`를 멈추고 두 기능을
   `unbind`한 뒤 `driver_override`에 `vfio-pci`를 쓰고 `drivers_probe`로 다시 붙이면
   `lspci -nnk`의 `driver in use`가 `vfio-pci`가 되고 `/dev/vfio/<그룹>`이 생긴다. pve-node-3에서
   그렇게 됐다. **이것은 부팅 시 바인딩의 증거가 아니다.** 재부팅하면 `driver_override`는
   사라지고 그때부터는 1번의 설정이 잡는다.
6. 재부팅하고, **어떤 VM도 띄우기 전에** 같은 것을 본다. `lspci -nnk -s <bdf>`가 두 기능 다
   `vfio-pci`, `/dev/vfio/<그룹>` 있음, `dmesg | grep -i vfio`에 오류 없음, 그리고 **카드가
   열거돼 있는지**(§4b). VM을 먼저 띄우면 안 되는 이유는 Proxmox가 VM 기동 때 `hostpci`
   장치를 스스로 unbind해 vfio-pci에 붙이기 때문이다. VM에서 GPU가 보였다는 것은 부팅 시
   바인딩과 무관하다.

**첫 부팅 결과(2026-09-07 14:36, 콜드 부팅).** 위 설정으로 부팅해 VM 없이 확인했다. 두 기능 다
`vfio-pci`, `/dev/vfio/13` 있음, IOMMU 그룹 125, `dmesg`에 vfio 오류 없음. nvidia 모듈은 올라왔으나
`NVRM: GPU 0000:43:00.0 is already bound to vfio-pci`로 물러났다. `softdep`이 의도대로 동작한 것이다.
**이것은 콜드 부팅이라 §4b의 웜 리부트 질문에는 답하지 않는다.** 그 시험은 아직 남아 있다.

**첫 재부팅은 S5를 누를 사람이 있을 때 한다.** (2026-09-08에 했다: 무접촉 2회, NVIDIA 게스트 사용 뒤 1회 모두 카드 유지, §4b 표. `disable_idle_d3=1`은 필요 없었다. 아래 문단은 그 시험 전의 판단이다.) vfio-pci가 잡은 카드는 게스트가 열기 전까지
runtime PM으로 D3hot에 들어간다(`/sys/bus/pci/devices/<bdf>/power/runtime_status`가
`suspended`. pve-node-3에서 동적 바인딩 직후 그랬다). 그 상태의 웜 리부트는 §4b 표의 세 행 어디에도
없는 네 번째 조합이라, 이 설정을 넣고 하는 첫 재부팅이 그것을 잰다. 카드를 잃으면 S5로
되살리고 결과를 §4b 표에 더한다. 그때 원인을 nouveau 쪽에서 찾지 말 것. 먼저 돌릴 손잡이는
`options vfio-pci disable_idle_d3=1`이다. 게스트를 붙였다 뗄 때의 리셋(FLR)도 마찬가지로
안 잰 것이었고, 패스스루 개발 라운드가 2026-09-08에 쟀다([gpu-passthrough.md](gpu-passthrough.md)): NVIDIA 드라이버 게스트는 어떤 리셋 모양에서도 무사했고, nouveau(GSP) 게스트의 종료가 FLR을 실패시켜 카드를 잃었다.

**`disable_vga=1`의 뜻.** vfio-pci가 VGA 리전(legacy I/O와 `0xa0000`)을 노출하지 않고 카드의
legacy VGA decoding을 끈다. ASPEED가 부팅 VGA인 이 호스트에서 호스트 쪽으로는 맞는 선택이고
헤드리스 패스스루에는 그대로 쓴다. **다만 Proxmox `hostpci`의 `x-vga=1`과 양립하지
않는다.** 그 옵션은 VGA 리전을 요구하므로 VM 기동이 그 기능을 지원하지 않는다는 오류로 죽는다.
게스트에 주 디스플레이로 넘기려면 이 파일에서 `disable_vga=1`을 빼고 initrd를 다시 만든다.

**되돌리기.** 재부팅 없이 되돌리려면 두 기능의 `driver_override`를 비우고
(`echo > /sys/bus/pci/devices/<bdf>/driver_override`) unbind한 뒤 `drivers_probe`로 다시 붙이면
nvidia가 잡는다. 영속 설정을 걷으려면 `/etc/modprobe.d/vfio.conf`와
`/etc/modules-load.d/vfio.conf`를 지우고, `/etc/default/grub`을 `/root/prep-backup/`의 사본으로
되돌리고(두 파라미터가 빠져도 IOMMU는 커널 기본으로 켜진 채다), `update-initramfs -u -k all`,
`update-grub`, `systemctl enable --now nvidia-persistenced`, 재부팅. NVIDIA 패키지가 그대로
있으므로 그러면 nvidia가 다시 잡는다.

## 5. 이 레포지토리가 남기는 것

- [README](../README.md)의 `## 운영 대상` 블록에 노드 한 행. 인수와 같은 작업 단위에서
  적어요.
- `hosts/<name>/`와 apply 스크립트. 첫 설정 산출물이 생겼을 때만 만들고(설치의
  `interfaces`와 sshd 선택이 첫 후보예요) README의 구성 절을 따라요.

최종 갱신: 2026-11-08
