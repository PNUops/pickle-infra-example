# 런북: GPU 패스스루 (Proxmox 노드의 GPU를 VM에 통째로 넘기기)

GPU가 꽂힌 Proxmox 노드에서 카드 한 장을 VM 한 대에 패스스루하는 절차다. 호스트가 카드를
잡지 않게 하는 부팅 시 `vfio-pci` 바인딩은 [proxmox-node-intake.md](proxmox-node-intake.md)
§4c가 갖고, 이 문서는 그 위에서 시작한다. 2026-09-08 `pve-node-3`(RTX 5090)에서 처음 걸어 본
결과로 쓴 것이고, 다른 카드나 다른 노드에서는 표시한 값을 다시 잰다.

세 가지가 전 구간에 걸린다.

- **실행 중인 VM에는 붙이지도 떼지도 못한다.** Proxmox의 `hotplug` 집합에 PCI가 없다.
  실행 중 VM에 `hostpci`를 쓰면 오류 없이 `[PENDING]`에 남고, **VM을 끄는 순간 설정에
  확정되어** 다음 기동에 적용된다. 게스트 안에서 재부팅해도 적용되지 않는다(QEMU 리셋일 뿐
  프로세스 재시작이 아니다). 붙이는 것도 떼는 것도 그 VM의 종료와 기동이다.
- **VM을 기동할 때마다 호스트가 카드에 FLR을 건다**(`vfio-pci … resetting / reset done`,
  종료 때도 한 번). 리셋 시점에 카드를 무엇이 잡고 있는가에 민감한 카드(§4b)라면 그 조건이
  기동마다 걸린다.
- **API 토큰은 Resource Mapping으로만 `hostpci`를 쓴다.** raw BDF(`host=0000:43:00`)는
  `root@pam` 실사용자 전용이고 root 토큰도 거부된다(`only root can set 'hostpci0' config
  for non-mapped devices`, HTTP **500**). 플랫폼은 매핑 이름을 저장하고 BDF는 저장하지 않는다.

## 1. 매핑 만들기 (root, 노드마다 한 번)

```bash
# 카드의 id와 subsystem-id, IOMMU 그룹을 읽는다
lspci -nn -s 43:00.0                       # [10de:2b85]
lspci -vvnns 43:00.0 | grep Subsystem      # [1569:f318]
ls /sys/kernel/iommu_groups/13/devices/    # 43:00.0 과 43:00.1 만 있어야 한다

pvesh create /cluster/mapping/pci --id rtx5090 \
  --description "pve-node-3 RTX 5090 43:00.0+.1" \
  --map node=pve-node-3,path=0000:43:00,id=10de:2b85,iommugroup=13,subsystem-id=1569:f318
pvesh get /cluster/mapping/pci --check-node pve-node-3 --output-format json   # "checks":[] 이어야 한다
```

`path`에 기능 번호를 빼면(`0000:43:00`) VGA와 오디오 두 기능이 함께 넘어간다. `--check-node`의
`checks`가 비어 있지 않으면 `id`, `iommugroup`, `subsystem-id` 중 하나가 sysfs와 다르다는
뜻이고, 그 문구를 그대로 기록한다. 매핑은 `/etc/pve/mapping/pci.cfg`에 남는다.

## 2. 토큰에 줄 최소 권한 (실측 2026-09-08)

플랫폼 역할(`PickleProvisioner`, 15권한)에 더할 것은 둘이고 그 이상은 필요 없다.

| 무엇 | 어디에 | 없으면 |
|---|---|---|
| `VM.Config.HWType` | 역할에 추가(`/vms`에 이미 부여된 역할) | 403 `Permission check failed (/vms/<id>, VM.Config.HWType)`. `machine=q35`도 이 권한이다 |
| `Mapping.Use` | 매핑 경로 `/mapping/pci/<name>`에 `Mapping.Use`만 가진 역할로 ACL | 403 `Permission check failed (/mapping/pci/<name>, Mapping.Use)` |

```bash
pveum role modify PickleProvisioner --privs "<기존 15개>,VM.Config.HWType"
pveum role add PickleMappingUse --privs Mapping.Use
pveum acl modify /mapping/pci/rtx5090 --users pickle@pve --roles PickleMappingUse
```

- `Mapping.Audit`은 필요 없다. `Mapping.Use`만으로 `GET /cluster/mapping/pci`와 개별 매핑
  조회가 200으로 전체 내용을 준다.
- 권한이 없을 때 **목록 조회는 200에 빈 배열**이고 개별 조회만 403이다. 목록만 보고 「매핑이
  없다」로 읽지 않는다.
- `GET /nodes/<n>/hardware/pci`는 `/`에 `Sys.Audit`을 요구해 이 역할로는 403이다. 플랫폼은
  장치를 스스로 찾지 못하고 운영자가 이름 붙인 매핑만 쓴다.
- `delete=hostpci0`도 같은 두 권한을 본다(옛 값도 검사). 붙일 수 있는 토큰은 뗄 수 있다.
- `bios=ovmf`는 `VM.Config.Options`(보유), `efidisk0`는 `VM.Config.Disk`(보유)로 된다.

## 3. VM 모양과 붙이기

플랫폼 템플릿은 `qm create` 기본값(i440fx, SeaBIOS, EFI 디스크 없음)이다. 그 모양에서:

```bash
qm shutdown <vmid> --timeout 120            # 정지 상태에서만 쓴다
qm set <vmid> --hostpci0 mapping=rtx5090,rombar=0
qm config <vmid> --current | grep hostpci   # 현재 설정에 있고
qm pending <vmid> | grep hostpci            # cur 이어야 한다 (new 면 아직 pending)
qm start <vmid>
```

**`rombar=0`이 없으면 SeaBIOS가 카드의 옵션 ROM(VBIOS)을 실행하다 멈춘다**(2026-09-08 실측:
`qm start`는 돌아오고 카드도 예약·리셋되고 메모리 16 GiB가 핀되지만 시리얼 콘솔에 아무것도
안 나오고 vCPU 하나가 100%로 돈다). ROM을 숨기면 부팅하고 게스트 `nvidia-smi`가 카드를
Gen5 x16으로 본다. 드라이버가 `int10h(4f03) vesa call failed`를 남기는데 ROM이 없다는 뜻이고
연산에는 무해하다.

같은 날 q35 모양도 재봤다. **SeaBIOS 경로는 i440fx든 q35(`pcie=1`)든 `rombar=0`이 없으면 같은
자리에서 멈추고**(시리얼 로그 0바이트), 있으면 부팅한다. **OVMF(`bios ovmf` + `efidisk0`)는
기본 ROM으로도 부팅한다.** 즉 멈추는 것은 SeaBIOS이고 카드가 아니다. 플랫폼 템플릿 모양을
바꿀 이유는 실측에서 나오지 않았다. q35로 가면 `machine` 쓰기에 `VM.Config.HWType`이 들고
OVMF는 VM마다 EFI 디스크가 든다. `pcie=1`을 i440fx에 쓰면 QEMU가 뜨기 전에
`q35 machine model is not enabled`로 거부된다(VM은 정지 상태 유지).

기동 시간은 GPU 없이 약 1.4초(`qm start` 반환)/12초(agent 응답)이던 것이 약 3.5초/14.5초가
된다. qemu-server 9.2.7이 패스스루 VM에 주는 기동 타임아웃은 `config_aware_timeout` 30초×4 = 120초다(300초는
suspend 재개 때만. 소스 `PVE/QemuServer/Helpers.pm`). 게스트 메모리는 QEMU 프로세스가 첫 순간부터 전부 잠근다(`VmLck` =
설정 메모리). 밸룬은 듣지 않는다.

토큰으로 하면 같은 일이 `PUT /nodes/<n>/qemu/<vmid>/config`에 `hostpci0=mapping=rtx5090,rombar=0`
이고, 응답 200은 「썼다」이지 「붙었다」가 아니다. 붙었는지는 `GET …/config?current=1`에
`hostpci0`가 있고 `GET …/pending`에 없는 것으로 본다.

## 4. 떼기와 인계

```bash
qm shutdown <A> --timeout 120 && qm set <A> --delete hostpci0     # A 에서 뗀다
qm set <B> --hostpci0 mapping=rtx5090,rombar=0 && qm start <B>     # B 에 붙인다
```

한 매핑을 두 VM 설정에 동시에 적어 둘 수는 있고, 카드를 쥔 VM이 있을 때 다른 VM을 기동하면
qemu-server가 `PCI device '0000:43:00.0' already in use by VMID '90001'`(exit 255)로 **카드를
건드리지 않고** 기동을 거부한다(예약 파일 `/var/run/qemu-server/pci-id-reservations`). 그래서
인계 절차는 「A 종료 → B 기동」이면 되고, A의 `hostpci0`를 지우는 것은 다음에 A를 켤 때 GPU
없이 올리기 위해서다. 2026-09-08 실측으로 인계 10회는 한 번에 약 19초(종료 5초, 기동에서
agent 응답까지 14초)였고, 게스트 재부팅 3회·종료와 기동 3회·`qm reboot` 3회·강제 종료 3회,
그리고 100% 부하(354 W)에서의 강제 종료 뒤에도 카드는 정상이었다. 게스트 안 재부팅은 호스트
쪽 리셋을 일으키지 않고(예약 PID 유지), 종료와 기동은 각각 한 번씩 리셋한다.

## 5. 카드 상태 확인과 손실 대응

```bash
lspci -D -nn -d 10de:                                   # 두 기능이 보여야 한다
setpci -s 43:00.0 VENDOR_ID.w                           # 10de. ffff 면 잃은 것
ls /sys/kernel/iommu_groups | wc -l                     # pve-node-3 는 125. 123 이면 루트 포트까지 사라졌다
cat /sys/bus/pci/devices/0000:43:00.0/power/runtime_status   # 게스트 없을 때 suspended, 있을 때 active
cat /var/run/qemu-server/pci-id-reservations            # 어느 VMID 가 쥐고 있나
dmesg -T | grep -iE 'vfio|43:00|Xid|reset' | tail
```

잃었으면 rescan에 시간을 쓰지 않는다(§4b). S5(`poweroff` 뒤 전원 버튼)로 되살린다. vfio 상태의
웜 리부트는 2026-09-08 실측 3회가 무사했고, 그 모양에서 잃는 날이 오면 그때 볼 손잡이가
`options vfio-pci disable_idle_d3=1`이다(§4c). **FLR이
`giving up`으로 끝난 뒤에는 그 카드를 여는 어떤 `qm start`도 하지 않는다**(2026-09-08에 그
기동이 호스트를 멈춰 원격 `poweroff`조차 들어가지 않았다). `vendor_id`가 `10de`가 아니면(`0001`,
`ffff`) 바로 사람을 부른다.

## 6. 게스트 안의 GPU 사용률 읽기

호스트 `nvidia-smi`는 카드가 vfio에 있는 동안 드라이버와 통신하지 못한다. 읽는 길은 guest
agent이고 플랫폼 역할이 이미 가진 `VM.GuestAgent.Unrestricted`로 된다.

```bash
qm guest exec <vmid> -- nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw,pstate --format=csv,noheader
# API: POST /nodes/<n>/qemu/<vmid>/agent/exec (argv 원소마다 command= 필드 하나) -> {"data":{"pid":N}}
#      GET  /nodes/<n>/qemu/<vmid>/agent/exec-status?pid=N -> {"exited":1,"exitcode":0,"out-data":"..."}
```

API 왕복은 약 200 ms, CLI는 perl 기동 때문에 600~870 ms다. agent가 꺼져 있으면 API는
HTTP 500 `QEMU guest agent is not running`이고, 바이너리가 없으면 exit code가 아니라 오류
응답이다. `utilization.gpu`는 짧은 부하에서 전력보다 늦게 반응하므로 전력과 pstate를 함께
읽는다. GeForce 패스스루 게스트에서는 프로세스별 사용량(`--query-compute-apps`)이 비어 있다.

## 7. 개발·시험용 게스트를 띄울 때 (플랫폼 노드가 아닐 때)

노드에 게스트 브리지가 없으면 비영속 NAT 브리지로 충분하다. `/etc/network/interfaces`에
쓰지 않으므로 재부팅에 사라진다.

```bash
ip link add vmbr9 type bridge && ip addr add 203.0.113.225/27 dev vmbr9 && ip link set vmbr9 up
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -s 203.0.113.224/27 -o vmbr0 -j MASQUERADE
```

VMID는 플랫폼 대역(100–999 LXC, 1000–9999 템플릿, 100000– 사용자 VM) 밖의 90000번대를
쓰고, 끝나면 `qm destroy <vmid> --purge`, 위 세 줄의 역순(`iptables -t nat -D …`,
`sysctl -w net.ipv4.ip_forward=0`, `ip link del vmbr9`), 시험용 사용자·역할(`pveum user delete`,
`pveum role delete`), 매핑(`pvesh delete /cluster/mapping/pci/<name>`)까지 지운다. 게스트
접속은 노드 안에서만 하고 개발 머신의 `~/.ssh`에 아무것도 남기지 않는다.

**게스트 이미지는 카드를 붙이기 전에 `nouveau`를 blacklist하고 드라이버를 깐다.** 카드를 잃은
유일한 상태가 「nouveau가 잡은 채 리셋」이고 기동마다 리셋이 있기 때문이다. **2026-09-08에 게스트
안에서 재현됐다**: HWE 커널 7.0.0-31의 nouveau(GSP 570.144)가 카드를 잡은 게스트를 `qm shutdown`
하자 호스트 FLR이 실패하고(`not ready 65535ms after FLR; giving up`, vendor id `0001`), 다음
`qm start`가 걸리며 호스트 ssh가 끊겼다. 복구는 S5(전원 버튼)였다. Ubuntu 24.04의 기본 cloud
커널 6.8은 nouveau가 이 칩을 몰라(`unknown chipset`) 우연히 안전했을 뿐이고, HWE 커널 7.0에서는
잡았다(실측). Rocky의 전체 커널처럼 nouveau가 이 칩을 아는 다른 이미지도 같을 것으로 보지만 재지 않았다. Ubuntu 24.04
cloud image 기준으로 NVIDIA CUDA 저장소(`cuda-keyring_1.1-1_all.deb`, `repos/ubuntu2404`)의
`nvidia-open`(2026-09-08 시점 610.57.04)과 `cuda-toolkit-13-3`, `linux-headers-$(uname -r)`,
그리고 `qemu-guest-agent`(cloud image에 없다)를 깐다.

최종 갱신: 2026-09-08
