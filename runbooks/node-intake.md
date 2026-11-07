# 런북: 비Proxmox 노드 편입

Proxmox 노드가 아닌 호스트의 실측과 운영자 SSH 접속 경로 확보, 이 레포지토리의 기록까지 다뤄요.

편입 뒤의 서빙 설정과 배포 스크립트,
`hosts/<name>/` 아래 호스트별 디렉터리는 서비스가 처음 필요해지는 작업에서 만들어요.
Proxmox 노드 편입은 별도 절차예요. 이미 있는 플랫폼에 더하는 Proxmox 노드는
[proxmox-node-intake.md](proxmox-node-intake.md)를 따르고, 빈 호스트에 플랫폼 전체를
세울 때는 [new-environment.md](new-environment.md)를 따라요.

아래 두 가지 경계를 지켜요.

- **플랫폼 인벤토리는 Proxmox 노드 전용이에요.** 비Proxmox 호스트는
  `apply-platform-inventory.sh`로 등록하지 않고 VM 배치 후보가 되지도 않아요.
  인벤토리 행의 API 엔드포인트와 브리지, 스토리지 필드는 하이퍼바이저에서만 뜻이 있어요.
- **공용 호스트는 최소한으로만 바꿔요.** 다른 사람의 작업이 실행 중이면
  `authorized_keys` 한 줄만 추가하고 나머지는 읽기만 해요.
  sshd와 패키지, 방화벽은 바꾸지 않아요.

## 1. 접속 경로

개발 머신에서 호스트에 닿는 경로를 정해 `~/.ssh/config`에 적어요. 이후 명령이 이 별칭을 쓰므로
포트와 점프, 키도 이곳에 정의해요.

- 직접 닿는 경우: DNS 이름이나 라우팅되는 주소가 있으면 `Host` 항목을 써요.
- 캠퍼스 대역에만 있는 경우: Proxmox 호스트를 경유해요. 점프의 사용자와 포트를 명시해서, 다른
  pickle SSH 설정이 없는 머신에서도 이 항목만으로 동작하게 해요.

```
Host <name>
  HostName <campus address>
  User <account>
  ProxyJump root@pve-node:22
  IdentityFile <노드 키 경로, §2 이후에 채운다>
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

`accept-new`는 첫 접속에서 호스트 키를 기록해요. 이후 키가 다르면 원인을 확인해요.

## 2. 운영자 키

노드마다 전용 ed25519 키 하나를 만들고 주석은 `pickle-node-<name>`으로 둬요.
점프 경로와 점검에서는 패스프레이즈를 입력할 수 없어 패스프레이즈를 두지 않아요.
개인키 정본은 운영자의 암호화된 키 보관소에서 만들고 그곳에만 보관해요.
노드나 이 레포지토리에는 개인키를 두지 않아요.

```bash
ssh-keygen -t ed25519 -N "" -C "pickle-node-<name>" -f <name>
```

기존 키의 지문을 먼저 기록하고 append로만 설치해요. 비밀번호 인증만 가능하면
운영자가 대화식으로 직접 입력해 설치하고 비밀번호는 어디에도 남기지 않아요.

```bash
ssh <name> "ssh-keygen -lf ~/.ssh/authorized_keys"   # 기준선, 비어 있으면 실패할 수 있다
ssh-copy-id -i <name>.pub <name>
```

그다음 키가 단독으로 동작하는지, 기준선 대비 증분이 새 지문 하나뿐인지 확인해요.

```bash
ssh -o BatchMode=yes <name> hostname
ssh <name> "ssh-keygen -lf ~/.ssh/authorized_keys"
```

기존 키는 건드리지 않아요. 비밀번호 인증 차단이나 계정 비밀번호 변경을 비롯한 sshd 변경은
운영자의 별도 결정이며 편입에 포함하지 않아요. 공용 호스트의 설정은 플랫폼이 정하지 않아요.

**이탈**: 노드의 `~/.ssh/authorized_keys`에서 `pickle-node-<name>` 줄을 지우고 지문
목록을 다시 확인해요. 편입으로 추가한 것은 그 한 줄뿐이에요.

## 3. 실측

새 키로 읽기만 하고 실제 값을 기록해요.

| 항목 | 방법 |
|---|---|
| 호스트명, OS, 커널, 아키텍처 | `hostnamectl` |
| 하드웨어 모델 | `cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name` |
| CPU, 메모리 | `lscpu`, `free -h` |
| 디스크와 마운트 | `lsblk -d -o NAME,SIZE,TYPE,MODEL`, `df -h` |
| GPU | `lspci \| grep -iE 'vga\|3d\|nvidia'`, `nvidia-smi -L` |
| 인터페이스, 라우트 | `ip -br addr`, `ip route`. 어느 캠퍼스 대역인지, Proxmox 호스트와 링크를 공유하는지 함께 적어요 |
| sshd 상태 | `/etc/ssh/sshd_config*`에서 포트와 `PasswordAuthentication`. 항목이 없으면 기본값은 yes예요 |
| 열린 포트 | `ss -tln` |
| 시각 동기화 | `timedatectl` |
| 기존 사용자(공용 호스트) | `ls /home`, `docker ps`, 동작 중인 서비스. 기록만 하고 건드리지 않아요 |

aarch64 호스트는 컨테이너 이미지와 미리 빌드한 바이너리, 빌드 툴체인의 x86 전제가 맞지 않을 수 있어요.
아키텍처를 눈에 띄게 적어요.

## 4. 대역외 관리 평면

서버급 호스트에는 sshd와 방화벽, 플랫폼 통제를 우회하는 BMC가 있을 수 있어요.
편입할 때 아래 세 가지를 확인하고, 답이 "없음"이나 "모름"이어도 그대로 기록해요.

1. **있는가?** `dmidecode -t 38`이 정본 확인 방법이고 **root가 필요해요**. root를 가질
   가능성이 가장 낮은 공용 호스트에서는 이 질문을 미답으로 두지 말고 소유자에게 넘겨요.
   `ls /sys/class/ipmi/`는 권한이 필요 없지만 **등록된** 장치만 보여주므로 드라이버가
   바인딩된 적 없는 호스트에서는 비어 보여요. 빈 결과는 "없음"이 아니라 "등록되지 않음"으로
   읽어요. OS 쪽 pass-through 인터페이스도 단서예요(일부 벤더가 link-local 주소에
   컨트롤러 이름을 딴 인터페이스로 노출해요).
2. **닿는가, 어디에서?** 전용 BMC 포트와 pass-through 경로를 모두 확인해요. 전용 포트가 네트워크에
   물려 있는지는 호스트 소유자에게 물어봐야 해요. pass-through
   인터페이스가 올라와 있으면 **이 호스트에 셸을 가진 사람은 누구나 BMC의 인증 표면에
   닿아요.** 그 링크에는 방화벽이 없고, 공용 호스트에서는 그 호스트의 모든 계정이
   해당돼요. 전용 포트가 미배선이어도 pass-through 경로는 열려 있으므로 둘 다 기록해요.
   **pass-through 주소는 배선의 증거가 아니에요.** 그런 인터페이스가 있는 벤더에서는 그것이
   내부 USB 링크이고 주소도 벤더 기본값이라, 전용 포트 배선과 무관하게 같은 값이 나와요.
   링크 로컬 대역이라 DHCP 실패로 읽기 쉬운데 그 읽기가 틀렸다. 실제로 그렇게 읽은 사례가
   있다. root가 있으면 `ipmitool lan print 1`이 단서를 하나 더 주지만 아래 두 조건을 구분해요.
   그 명령이 말하는 것은 **BMC가 지금 쓰는 포트**이므로 NIC selection(전용 포트인지 LOM
   공유인지)을 먼저 읽어야 그 답을 전용 포트의 것으로 읽을 수 있고, 답이 나오는 것은
   `IP Address Source`가 DHCP일 때뿐이에요. 0.0.0.0이면 임대를 못 받은 것이고(미배선이거나
   DHCP 없는 세그먼트), 주소가 있으면 적어도 임대 시점에는 배선됐다는 뜻이에요. DHCP가 아니면 이
   명령으로는 알 수 없어요. 그때는 **물리 확인이 먼저예요**. Redfish `EthernetInterfaces`의
   `LinkStatus`는 pass-through 링크가 있고 플랫폼이 BMC 자격증명을 소유한 호스트에서만
   대안이 되며(인증이 필요하고 스키마상 선택 속성이에요), **공용 호스트를 거쳐 BMC 자격증명을
   제시하지 않아요.** 셸을 가진 누구나 같은 링크에 닿을 수 있어요.
3. **기본 자격증명이 남아 있는가?** 로그인해서 확인하지 않아요. **주소와 SNMP community
   (`lan print`의 `SNMP Community String`)가 공장 기본값이면 비밀번호도 그럴 가능성을 함께
   봐요.** 다만 최근 세대는 기기별 고유 비밀번호가 기본이라 문서화된 옛 기본값을 단정하지
   않아요. 플랫폼이 소유한
   호스트에서는 운영자가 확인하고, 공용 호스트에서는 호스트 소유자에게 질문을 넘겨요.

## 5. 이 레포지토리가 남기는 것

- [README](../README.md)의 `## 운영 대상` 블록에 노드 한 행. 편입과 같은 작업 단위에서
  적어요.
- `hosts/<name>/`와 apply 스크립트. 첫 설정 산출물이 생겼을 때만 만들고, README의 구성
  절이 정한 형태를 따라요.

최종 갱신: 2026-11-08
