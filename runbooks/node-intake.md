# 런북: 비Proxmox 노드 편입

Proxmox 노드가 아닌 호스트를 플랫폼에 편입한다. 실측, SSH 운영자 접속 경로 확보,
그리고 이 레포지토리가 남기는 기록까지가 범위다.

편입은 그 상자에 서비스가 올라가기 전에 끝난다. 서빙 설정과 배포 스크립트,
`hosts/<name>/` 아래의 호스트별 디렉터리는 그것이 처음 필요해지는 작업에서 만든다.
Proxmox 노드는 절차가 아예 다르다. 이미 있는 플랫폼에 더하는 Proxmox 노드는
[proxmox-node-intake.md](proxmox-node-intake.md)를 따르고, 빈 호스트에 플랫폼 전체를
세우는 것은 [new-environment.md](new-environment.md)다.

두 가지 경계가 전 구간에 걸린다.

- **플랫폼 인벤토리는 Proxmox 노드 전용이다.** 비Proxmox 호스트는
  `apply-platform-inventory.sh`로 등록하지 않고 VM 배치 후보가 되지도 않는다.
  인벤토리 행의 API 엔드포인트와 브리지, 스토리지 필드는 하이퍼바이저에서만 뜻이 있다.
- **공용 호스트는 최소한으로만 바꾼다.** 그 상자가 이미 다른 사람의 작업을 돌리고
  있다면 편입이 더하는 것은 `authorized_keys` 한 줄뿐이고 나머지는 전부 읽기만 한다.
  sshd를 고치지 않고, 패키지를 깔지 않고, 방화벽을 건드리지 않는다.

## 1. 접속 경로

개발 머신에서 그 상자에 어떻게 닿을지 정하고 `~/.ssh/config`에 적는다. 이후 모든 명령이
이 별칭을 쓰므로 전송 방식(포트, 점프, 키)이 정의되는 곳은 이 한 군데다.

- 직접 닿는 경우(DNS 이름이나 라우팅되는 주소가 있다): 평범한 `Host` 항목.
- 캠퍼스 대역에만 있는 경우: Proxmox 호스트를 경유한다. 점프의 사용자와 포트를 명시해서, 다른
  pickle SSH 설정이 없는 머신에서도 이 항목만으로 동작하게 한다.

```
Host <name>
  HostName <campus address>
  User <account>
  ProxyJump root@pve-node:22
  IdentityFile <노드 키 경로, §2 이후에 채운다>
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

`accept-new`는 첫 접속에서 호스트 키를 기록한다. 나중의 불일치는 불편이 아니라 신호다.

## 2. 운영자 키

노드마다 전용 ed25519 키 하나를 만들고 주석은 `pickle-node-<name>`으로 둔다.
패스프레이즈는 두지 않는다. 점프 경로와 점검에서 이 키를 쓰는데 그 자리에서는 아무도
패스프레이즈를 입력할 수 없다. 대신 정본 사본이 운영자의 암호화된 키 보관소에만 있고
노드에도 이 레포지토리에도 없는 것이 보완 통제다. 키는 그 보관소에서 만든다.

```bash
ssh-keygen -t ed25519 -N "" -C "pickle-node-<name>" -f <name>
```

상자에 이미 있는 것의 지문을 먼저 뜬 다음 append로만 설치한다. 아직 비밀번호 인증밖에
없으면 운영자가 직접 설치해서 비밀번호를 대화식으로 입력하고 어디에도 남기지 않는다.

```bash
ssh <name> "ssh-keygen -lf ~/.ssh/authorized_keys"   # 기준선, 비어 있으면 실패할 수 있다
ssh-copy-id -i <name>.pub <name>
```

그다음 키가 단독으로 동작하는지, 기준선 대비 증분이 새 지문 하나뿐인지 확인한다.

```bash
ssh -o BatchMode=yes <name> hostname
ssh <name> "ssh-keygen -lf ~/.ssh/authorized_keys"
```

기존 키는 건드리지 않는다. 비밀번호 인증 차단이나 계정 비밀번호 변경을 비롯한 모든 sshd
변경은 운영자가 따로 내리는 결정이고 편입의 일부가 아니다. 공용 호스트에서는 애초에 이
플랫폼이 정할 일이 아니다.

**이탈**: 노드의 `~/.ssh/authorized_keys`에서 `pickle-node-<name>` 줄을 지우고 지문
목록을 다시 뜬다. 편입이 그 상자에 놓은 것은 그 한 줄이 전부다.

## 3. 실측

새 키로 읽기만 한다. 기대한 값이 아니라 있는 그대로 기록한다.

| 항목 | 방법 |
|---|---|
| 호스트명, OS, 커널, 아키텍처 | `hostnamectl` |
| 하드웨어 모델 | `cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name` |
| CPU, 메모리 | `lscpu`, `free -h` |
| 디스크와 마운트 | `lsblk -d -o NAME,SIZE,TYPE,MODEL`, `df -h` |
| GPU | `lspci \| grep -iE 'vga\|3d\|nvidia'`, `nvidia-smi -L` |
| 인터페이스, 라우트 | `ip -br addr`, `ip route`. 어느 캠퍼스 대역인지, Proxmox 호스트와 링크를 공유하는지 함께 적는다 |
| sshd 상태 | `/etc/ssh/sshd_config*`에서 포트와 `PasswordAuthentication`. 항목이 없으면 기본값 yes다 |
| 열린 포트 | `ss -tln` |
| 시각 동기화 | `timedatectl` |
| 기존 사용자(공용 호스트) | `ls /home`, `docker ps`, 동작 중인 서비스. 기록만 하고 건드리지 않는다 |

aarch64 상자는 x86 전제를 조용히 깨뜨린다(컨테이너 이미지, 미리 빌드된 바이너리, 빌드
툴체인). 아키텍처는 눈에 띄게 적는다.

## 4. 대역외 관리 평면

서버급 상자에는 sshd와 방화벽, 플랫폼의 모든 통제를 우회하는 BMC가 붙어 있을 수 있다.
편입 시점에 세 가지를 답하고, 답이 "없음"이나 "모름"이어도 그대로 기록한다.

1. **있는가?** `dmidecode -t 38`이 정본 확인 방법이고 **root가 필요하다**. root를 가질
   가능성이 가장 낮은 공용 호스트에서는 이 질문을 미답으로 두지 말고 소유자에게 넘긴다.
   `ls /sys/class/ipmi/`는 권한이 필요 없지만 **등록된** 장치만 보여주므로 드라이버가
   바인딩된 적 없는 상자에서는 비어 보인다. 빈 결과는 "없음"이 아니라 "등록되지 않음"으로
   읽는다. OS 쪽 pass-through 인터페이스가 또 하나의 단서다(일부 벤더가 link-local 주소에
   컨트롤러 이름을 딴 인터페이스로 노출한다).
2. **닿는가, 어디에서?** 경로가 둘인데 두 번째를 잊는다. 전용 BMC 포트가 네트워크에
   물려 있는지는 상자 소유자에게 물어야 하는 물리적 질문이다. 그런데 pass-through
   인터페이스가 올라와 있으면 **이 호스트에 셸을 가진 사람은 누구나 BMC의 인증 표면에
   닿는다.** 그 링크에는 방화벽이 없고, 공용 상자에서는 그 호스트의 모든 계정이
   해당된다. 전용 포트가 미배선이어도 두 번째 경로는 닫히지 않으므로 둘 다 기록한다.
3. **기본 자격증명이 남아 있는가?** 로그인해서 확인하지 않는다. 플랫폼이 소유한
   호스트에서는 운영자가 확인하고, 공용 호스트에서는 상자 소유자에게 질문을 넘긴다.

## 5. 이 레포지토리가 남기는 것

- [README](../README.md)의 `## 운영 대상` 블록에 노드 한 행. 편입과 같은 작업 단위에서
  적는다.
- `hosts/<name>/`와 apply 스크립트. 첫 설정 산출물이 생겼을 때만 만들고, README의 구성
  절이 정한 형태를 따른다.
