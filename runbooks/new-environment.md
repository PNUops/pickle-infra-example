# 런북: 신규 환경 구축 (빈 호스트에서 동작하는 플랫폼까지)

플랫폼 전체를 아무것도 없는 상태에서 세우는 관통 절차다. Proxmox를 올릴 수 있는 빈
기계에서 출발해 VM을 프로비저닝하고 콘솔을 서비스하고 SSH에 응답하며 재부팅을 견디는
배포까지 간다. 이 문서가 담는 것은 순서와 의존, 그리고 환경마다 바뀌는 값이다. 이미
존재하는 절차는 **다시 적지 않는다.** 각 단계는 그것을 소유한 런북이나 스크립트를
가리킨다. 이 표본 사본에 담기지 않은 런북은 비공개 레포지토리에 있고, 아래에서는
이름 대신 설명으로 가리킨다.

이 런북의 대상은 새 플랫폼 전체가 되는 Proxmox 노드다. 이미 있는 플랫폼에 더하는
Proxmox 노드는 [proxmox-node-intake.md](proxmox-node-intake.md)를 따르고(그 노드에는 이
문서의 12단계 스크립트를 돌리면 안 된다), Proxmox 노드가 아닌 호스트는
[node-intake.md](node-intake.md)로 편입한다.

세 가지 표시를 문서 전체에서 쓰고, 그것이 이 문서의 핵심이다.

- **HUMAN**. 운영자만 할 수 있는 단계다. 구매, 계정 생성, 학교에 넣는 요청, 물리 작업.
  스크립트도 세션도 대신할 수 없으므로 작업 중에 발견할 것이 아니라 미리 일정에
  잡아야 한다.
- **BLOCKED**. 커밋된 절차가 없는 단계다. 무엇이 비었는지와 무엇이 있으면 닫히는지를
  적어 둔다. **이 단계들을 기억이나 짐작으로 즉석에서 만들어 내지 마라.** 호스트나 프록시
  골격이 틀리면 그 위 계층의 결함처럼 보이는 방식으로 실패한다.
- **UNVERIFIED**. 절차는 적혀 있으나 그것이 없는 기계에서 아무도 따라가 본 적이 없는
  단계다. 12에서 14단계는 데이터베이스 재구축에서 끝까지 걸어 봤고, 그 앞은 동작 중인
  호스트를 읽어서 복원한 것이라 도착지는 증명하지만 경로는 증명하지 않는다. **다음 실제
  구축이 곧 검증이다.** 걷는 사람은 이 파일을 열어 두고, 각 단계가 끝날 때마다 그대로
  두거나 그 자리에서 고친 다음 해당 단계의 표시를 뗀다. 실패를 눈앞에 두고 한 수정이
  나중에 짐작으로 하는 같은 수정보다 낫다.

## 검증 상태

| 단계 | 상태 |
|---|---|
| 0, 1 | HUMAN이고 일부는 미기록. 각 주석 참조 |
| 2에서 11 | **UNVERIFIED**. 동작 중인 호스트를 읽어서 쓴 것이고 빈 호스트에서 걸어 본 적 없다 |
| 12에서 14 | 걸어서 확인됨. 데이터베이스를 지우고 다시 재생해 다섯 스크립트로 부트스트랩하고 스모크 스위트로 증명했다 |

## 순서

위에서 아래로 실행한다. "선행" 열이 순서를 실제로 만드는 강한 의존을 가리킨다. 상세와
공백은 표 아래 번호 주석에 있다.

| # | 단계 | 절차 | 선행 |
|---|---|---|---|
| 0 | **HUMAN** 이름, 계정, 접근 권한 확보 | 주석 0, 일부 **BLOCKED** | 없음 |
| 1 | **HUMAN** 물리 호스트: 디스크, OS와 Proxmox VE 설치, sshd 포트, 캠퍼스 네트워크 | 주석 1. 결과 상태는 기록되어 있고 설치 프로그램 선택은 아니다 | 0 (캠퍼스 IP, 방화벽 요청 접수) |
| 2 | 호스트 브리지, NAT, 방화벽 | [`hosts/pve-node/interfaces`](../hosts/pve-node/interfaces)를 값 표에 맞게 바꿔 설치. 하드닝과 재부팅 후 점검은 네트워크 런북(비공개 레포) | 1 |
| 3 | 배포용 체크아웃 생성과 볼트 해제 | 주석 3 | 1 |
| 4 | api용 Proxmox API 계정, 역할, ACL | 주석 4 | 1 |
| 5 | 앱 컨테이너 (PostgreSQL + api + 콘솔 nginx) | `scripts/create-app-lxc.sh` 실행 후 볼트에서 `/etc/pickle/api.env`를 채운다(또는 비밀을 새로 발급) | 2, 3 |
| 6 | SSH 게이트웨이 컨테이너 (sshpiperd + WireGuard 종단) | `scripts/create-sshgw-lxc.sh`. 릴레이가 필요로 하는 WG 공개키를 출력한다. `/etc/pickle/sshgw.env`를 채운다 | 2, 3 |
| 7 | **HUMAN** 릴레이 인스턴스 생성 후 기동 | 릴레이 기동 런북(비공개 레포) 전 구간(6단계와 WG 페어링, HAProxy, 방화벽), 에이전트는 `scripts/deploy-relay.sh` | 0, 6 |
| 8 | 리버스 프록시 컨테이너 (공개 웹 진입점) | 주석 8. 컨테이너를 만들면 설정은 에이전트 배포(10단계)와 apply 스크립트(11단계)로 도착한다. 플랫폼 루트마다 Origin CA 와일드카드 쌍을 설치한다 | 2, 3, 그리고 0 (인증서) |
| 9 | 사용자 VM 템플릿 | image-builder 레포지토리(공개. 이 호스트에서는 `/srv/pickle` 아래 체크아웃), OS별 프로파일에서 값 표의 템플릿 VMID로. 재빌드 흐름은 템플릿 재빌드 런북(비공개 레포) | 2 |
| 10 | 서비스 배포 | `scripts/deploy-api.sh`(api 첫 기동이 Flyway V1에서 최신까지 실행), `deploy-console.sh`, `deploy-proxy-agent.sh`, `deploy-sshgw.sh` | 5, 6, 8. api.env 채워져 있을 것 |
| 11 | 인그레스와 호스트 정책 | `scripts/apply-terminal-ingress.sh` → `scripts/apply-main-domain-vhost.sh`(주석 11) → `scripts/apply-tls-ciphers.sh`. 그다음 `scripts/apply-log-retention.sh`와 `scripts/apply-ops-timers.sh`(주석 11a) | 8, 10. 0 (LE 발급을 위해 DNS와 방화벽이 살아 있을 것) |
| 12 | 데이터베이스 부트스트랩: 인벤토리, 설정, 약관, OS 카탈로그, 릴레이 토큰 | [db-restore.md](db-restore.md)의 백지 부트스트랩 절, 거기 적힌 번호 순서 전부. dev 프로파일 정리와 실측 순서는 주석 12 | 5에서 10 완료. 인증서 행 때문에 8 |
| 13 | 최초 운영 데이터 | 주석 13. OS 하나 활성화, 킬 스위치 켜기, 기관 1개 이상과 사양 프리셋 생성, 관리자 계정 확인 | 12 |
| 14 | 스모크 테스트, 헬스 스냅샷, 첫 백업 | 주석 14 | 13 |

## 환경마다 바뀌는 값

이 환경이 딛고 선 모든 리터럴과 그것이 나타나는 파일이다. 새 환경은 1단계 전에 이 표에서
각 값을 **한 번에** 정한다. 실패하는 스크립트를 하나씩 쫓아다니는 것이 구축이 멈추는
방식이다.

| 값 | 이 환경 | 나타나는 곳 |
|---|---|---|
| 게스트 씬풀과 그 볼륨 그룹 | `pve/data`, VG `pve` | `scripts/health-check.sh`의 `THINPOOL_LV`(`vg/lv` 형태여야 한다. `THINPOOL_VG`를 함께 주지 않으면 VG를 여기서 유도한다). 게스트 스토리지 이름이 다른 호스트는 이것을 설정할 때까지 씬풀 행이 영구히 빨갛게 나오고, 스크립트는 볼륨 그룹만 적은 값을 거부한다 |
| LVM 질의 상한 | 10초 (`LVM_TIMEOUT`) | `scripts/health-check.sh`. 각 `lvs`와 `vgs` 읽기의 상한이라, 용량이 소진되어 볼륨이 정지된 풀이 스냅샷을 붙잡고 있을 수 없게 한다. 정상 호스트가 실제로 더 필요할 때만 올린다 |
| DNS 레코드가 가리키는 캠퍼스 공인 IP | 운영자가 보유 (DNS 제공자 대시보드와 학교 DNS) | 공개 A 레코드, 학교 방화벽 요청(0단계), `scripts/health-check.sh`의 `MAIN_DOMAIN_PUBLIC_IP`(**하드코딩된 기본값**이라 그냥 두면 헬스 체크가 매번 다른 사이트의 주소를 기대한다), 그리고 릴레이 기동 런북(비공개 레포)의 릴레이 엣지 방화벽. 그 방화벽은 이 `/32`에서만 관리 SSH를 허용하므로 값이 틀리면 운영자가 릴레이에서 잠긴다 |
| 호스트 LAN 주소, 게이트웨이, NIC 이름 | `192.0.2.10/24`, gw `192.0.2.1`, `nic0` | [`hosts/pve-node/interfaces`](../hosts/pve-node/interfaces)의 vmbr0 스탠자 |
| 인프라 브리지 망 (vmbr1) | `198.18.0.0/16`, 호스트 `.0.1`. 프록시 `.1.10`, 앱 `.1.20`, sshgw `.1.30` | `hosts/pve-node/interfaces`(NAT, DNAT, FORWARD 규칙이 `.1.10`을 고정한다), `create-app-lxc.sh`, `create-sshgw-lxc.sh`, 리버스 프록시 재구축 런북(비공개 레포) §1, `apply-terminal-ingress.sh`, `apply-main-domain-vhost.sh`(`PICKLE_PROXY_IP`, `PICKLE_HOST_PROBE_IP`). **릴레이 쪽에도 있다.** `lightsail/wireguard/wg0.conf.template`이 api의 `.1.20/32`를 터널로 들여보내는데 그것이 `PICKLE_RELAY_SYNC_URL`이 가리키는 바로 그 주소다 |
| 게스트 브리지 망 (vmbr2) | `198.19.0.0/16`, 호스트 `.0.1` | `hosts/pve-node/interfaces`. `PICKLE_POOL_CIDR`, `PICKLE_POOL_GATEWAY`, `PICKLE_POOL_RESERVED`(`apply-platform-inventory.sh`) |
| WireGuard 전송 망 | `100.64.0.0/30`. 릴레이 `.1`, sshgw `.2` | `create-sshgw-lxc.sh`, `lightsail/wireguard/wg0.conf.template`(그 `AllowedIPs`는 게스트 망과 **api 주소**도 담으므로 항목이 틀리면 터널이 아니라 릴레이 sync가 깨진다), `lightsail/haproxy/haproxy.cfg.template`(`server sshgw 100.64.0.2:22`), `lightsail/nftables/nftables.conf`, `hosts/pve-node/interfaces`(`/30` 라우트와 `.1` FORWARD accept), `PICKLE_RELAY_SOURCE_IP` |
| 주 진입 도메인 | `pickle.pusan.ac.kr` | `apply-main-domain-vhost.sh`의 `PICKLE_MAIN_DOMAIN`. `create-sshgw-lxc.sh`의 `PICKLE_TERMINAL_CONSOLE_ORIGIN`. **터미널 브리지는 이 origin만 받아들이고 그 컨테이너는 11단계보다 훨씬 앞인 6단계에서 만들어지므로** 여기 값이 낡으면 웹 터미널이 조용히 죽는다. `create-app-lxc.sh` 콘솔 vhost의 `server_name`, `health-check.sh`(`PICKLE_DEV_DOMAIN` 기본값과 Let's Encrypt 인증서 경로), 그리고 이 이름에서 200을 요구하는 `apply-tls-ciphers.sh`의 전후 단정. 스모크 테스트 기본값(`BASE`), pve-node의 `/etc/hosts` 헤어핀 항목 |
| 플랫폼 루트 도메인 | `pusan.dev` | `PICKLE_ROOT_DOMAIN`(`apply-platform-inventory.sh`와 `apply-settings.sh`. 같은 값을 쓰는 것이 의도다), 프록시 에이전트 환경의 `PICKLE_PROXY_AGENT_WILDCARD_CERTS`(형식은 `<root>=<crt>:<key>`이고 에이전트는 그 루트에 대해 아무것도 렌더링하기 전에 이것이 필요하다. 프록시 에이전트 배포 런북(비공개 레포)), `ROOT`(`smoke-http-publish.sh`), 인증서 경로 `/etc/nginx/pickle-certs/<루트, 점을 하이픈으로>.{crt,key}`, DNS 존 |
| 사용자 SSH 호스트 | `ssh.example.dev` (DNS 전용 A 레코드에서 릴레이 고정 IP로) | 릴레이 기동 런북(비공개 레포) §5, api의 `PICKLE_SSH_HOST`(재정의 지점). **비어 있으면 api가 컴파일된 기본값으로 폴백한다.** meta 엔드포인트와 알림 문구 양쪽에서 그렇게 되므로, 설정하지 않은 변수는 실패하지 않고 틀린 호스트를 광고한다. 콘솔은 비인증 랜딩 페이지용 상수를 따로 갖고 있다 |
| 릴레이 공개 호스트 (포트 포워딩) | `ssh.example.dev`. 위 사용자 SSH 호스트와 같은 이름이다. 둘 다 릴레이로 해석되기 때문이다 | `PICKLE_RELAY_PUBLIC_HOST`(`apply-platform-inventory.sh`, 필수. 기본값이 없고 이 열을 쓰는 API도 없다) |
| 릴레이 고정 IP와 관리 SSH | `198.51.100.10`, 관리 sshd `:22`, 키 `$VAULT/lightsail-ssh.pem` | `RELAY_HOST`, `RELAY_SSH_PORT`, `RELAY_SSH_KEY`(`deploy-relay.sh`). **이름이 다른 두 번째 묶음** `PICKLE_RELAY_SSH_KEY`, `_USER`, `_PORT`(`apply-relay-token.sh`). `RELAY`(`smoke-ssh-gateway.sh`, 14단계 묶음). sshgw `wg0.conf`의 `Endpoint`. 릴레이 기동 런북(비공개 레포) |
| 컨테이너 ID | 프록시 `100`, 앱 `101`, sshgw `102` | 생성, 배포, 백업 스크립트의 `CTID`. 각각 자기 컨테이너를 기본값으로 갖는다. 모든 apply 스크립트의 `PICKLE_PROXY_CTID`와 `PICKLE_APP_CTID`. `apply-relay-token.sh`의 `PICKLE_TUNNEL_CTID`는 sshgw 컨테이너를 세 번째 이름으로 부르는 것인데, 게이트웨이 역할이 아니라 터널 용도로 쓰기 때문이다. 대부분의 런북에는 리터럴로 들어 있다 |
| 템플릿 VMID | `1001`에서 `1005` (Ubuntu 24.04/26.04/22.04, Debian 13/12. `1000`은 폐기된 이전 판) | `apply-os-catalog.sh`의 `CATALOG`(이름과 VMID 둘 다), image-builder의 OS별 프로파일, 템플릿 재빌드 런북(비공개 레포) |
| Proxmox 노드 이름 | `pve-node` | `PICKLE_NODE`. 노드 API 인증서의 SAN이어야 하고 LXC 101 안에서도 해석되어야 한다(`create-app-lxc.sh`가 쓰는 `/etc/hosts` 항목) |
| 스토리지 | `local-lvm` | `PICKLE_NODE_STORAGE`, 생성 스크립트, 템플릿 빌드 |
| 호스트 관리 SSH 포트 | `22` | 호스트 sshd 설정(**이것을 설정하는 커밋된 절차가 없다.** 주석 1). `hosts/pve-node/interfaces`의 방화벽 규칙이 이 값을 전제한다 |

## 단계별 주석

### 0. 이름, 계정, 접근 권한 확보 (HUMAN, 일부 BLOCKED)

1단계 전에 이미 갖고 있어야 하는 것. 학교 쪽 공인 IP와 그 IP로 열린 인바운드 80/443,
위임된 주 진입 도메인, 각 존에 등록된 플랫폼 루트 도메인과 SSH 호스트
도메인과 릴레이 호스트 도메인, 와일드카드 레코드를 만들고 플랫폼 루트마다 Origin CA
인증서를 발급할 수 있는 DNS 제공자 대시보드 로그인, Lightsail 릴레이를 만들 수 있는 AWS
계정, 그리고 SMTP 발신 계정(앱 비밀번호).

**BLOCKED: 취득 경로가 어디에도 기록되어 있지 않다.** 값은 알려져 있으나 각각이 어디서
왔는지는 아니다. 학교 IP와 도메인, 방화벽 작업의 요청 창구나 소요 기간이 없고, 구매한
도메인의 등록기관과 갱신 기록이 없으며, 어떤 외부 계정이 존재하고 누가 그것을 갖고
있는지에 대한 목록이 없다. 그것이 적히기 전까지 이 단계는 운영자의 기억으로 돌아간다.
기록할 때 값이 아니라 위치를 적는다. 어느 쪽이든 소요 기간 경고는 유효하다. 학교 요청은
며칠에서 몇 주 단위이므로 가장 먼저 넣는다.

### 1. 물리 호스트 (HUMAN, 일부 복원됨)

빈 기계를 "2단계를 돌릴 수 있는" 상태로 만드는 절차는 커밋된 적이 없다. 결과 상태는
동작 중인 호스트에서 읽어 아래에 적었다. 그것을 만들어 낸 선택은 기록되어 있지 않고,
물리 작업은 어차피 HUMAN이다.

**라이브 호스트에서 읽은 것. 이대로 재현한다.**

| | 이 호스트 |
|---|---|
| Proxmox VE | Debian trixie 위 9.2 |
| 리포지토리 | `pve-no-subscription` 활성화. enterprise와 Ceph 소스는 비활성화 |
| 디스크 | 두 개. 설치가 올라간 977 GB 장치와 **현재 미사용인** 1.8 TB 장치 |
| 스토리지 | `local`(디렉터리, 96 GB 루트 LV 위)과 `local-lvm`(LVM-thin, 약 839 GB). 플랫폼 변수들이 기본값으로 쓰는 이름이다 |
| 루트와 스왑 | 96 GB 루트 LV, 8 GB 스왑 LV, 나머지는 씬풀 |
| 관리 sshd | 포트 22. 비공개 배포에서는 기본 포트에서 옮겨 두었다 |

**아직 기록되지 않았고, 각각 옮겨 적기가 아니라 결정이 필요한 것.**

- 저 구성을 만들어 내는 설치 프로그램 선택. 파일시스템, 장치의 얼마를 볼륨 그룹이
  가져가는지, 루트와 스왑을 씬풀 대비 어떻게 잡는지. 새 기계의 디스크에 따라 크기 산정이
  달라지므로 이것은 결정이다.
- 두 번째 장치의 용도. 여기서는 붙어 있고 쓰이지 않은 채로 충분히 오래되어서, 정해진
  것이 아니라 열려 있는 질문이다.
- 관리 sshd를 기본 포트에서 옮기는 것. 포트 자체는 값 표에 있고, **이 변경이 구축 전체에서
  가장 위험한 단계다.** 잘못하면 그 작업을 하고 있는 세션이 끊긴다. 릴레이 기동 런북이
  바로 이 상황에 쓰는 방식을 따른다. 새 포트를 기존 포트와 나란히 열고, 두 번째 세션에서
  새 포트를 증명하고, 그다음에야 기존 포트를 없앤다.
- 캠퍼스 쪽 연결. 주소, 방화벽 개방, 그리고 그것을 승인하는 주체. 그것이 주석 0이다.

초기 셋업 로그가 하나 있으나 네트워크 재번호보다 앞선 것이라, 그대로 따라가면 사용자
망과 인프라 망이 뒤바뀐 호스트가 나온다. 값이 아니라 절차의 형태로만 취급한다.

### 3. 체크아웃과 볼트

이 레포의 모든 스크립트는 호스트의 `/srv/pickle` 레이아웃을 전제한다. `/srv/pickle/<repo>` 아래에
이 레포와 이 레포가 배포하는 서비스 레포들(api, console, sshgw, proxy-agent, relay-agent,
image-builder)의 체크아웃이 있고, git-crypt 비밀 볼트가 `$VAULT`에 있다. 이것들을
clone한 다음 **HUMAN**: git-crypt 키는 운영자가 갖고 있고 대역 밖으로 전달되어야 한다.
볼트가 잠긴 채로는 5단계에 설치할 비밀이 없고 `deploy-relay.sh`가 거부한다(SSH 키 없음).
빈 볼트로 시작하는 완전히 새 환경은 모든 비밀을 새로 발급해야 한다. 무엇이 있어야 하는지의
목록은 비밀 교체 런북(비공개 레포) §0이다.

### 4. api용 Proxmox API 계정

api는 전용 사용자와 API 토큰으로 Proxmox에 인증하고, 커스텀 역할 하나와 ACL 네 건이 그것을
인가한다. 이것들을 만드는 커밋된 절차는 없었고 토큰 *교체*만 적혀 있다
(비밀 교체 런북(비공개 레포) §2b). 아래 순서는 라이브 계정을 `pveum`으로 다시
읽어서 복원한 것이라, 누군가의 기억이 아니라 이 호스트가 실제로 돌리는 것을 재현한다.
이것이 없으면 api는 기동하고 모든 프로비저닝 호출이 403으로 답한다.

```sh
# 4a. 역할. 플랫폼이 실제로 쓰는 권한만 담는다.
pveum role add PickleProvisioner --privs \
  "Datastore.AllocateSpace,Datastore.Audit,SDN.Use,Sys.Audit,\
VM.Allocate,VM.Audit,VM.Clone,VM.Config.CPU,VM.Config.Cloudinit,\
VM.Config.Disk,VM.Config.Memory,VM.Config.Network,VM.Config.Options,\
VM.GuestAgent.Unrestricted,VM.PowerMgmt"

# 4b. 사용자. API 전용이다. 비밀번호를 설정하지 않으므로 이 계정은 웹 UI에 아예
#     로그인할 수 없고 토큰이 유일한 자격증명이다.
pveum user add pickle@pve --comment "Pickle platform service account"

# 4c. 권한 부여. 네 경로에 각각 하위로 전파되게 준다. `/`에 주는 것보다 좁다.
#     플랫폼은 다른 스토리지나 존을 건드리지 않는다.
for path in /vms /nodes /storage/local-lvm /sdn/zones/localnetwork; do
  pveum acl modify "$path" --users pickle@pve --roles PickleProvisioner
done

# 4d. 토큰. `--privsep 0`이 사용자의 권한을 그대로 갖게 한다. privsep을 켜면 권한이
#     하나도 없어서 첫 클론이 403이 된다. 비밀은 한 번만 출력되므로 터미널이 아니라
#     mode-600 파일로 받고, api 환경에는 그 파일에서 옮긴다.
umask 077
pveum user token add pickle@pve pickle-api --privsep 0 --output-format json \
  > /root/pickle-api-token.json
```

열다섯 중 둘은 이름을 짚어 둘 만하다. 줄이기는 쉽고 빠뜨리면 비싸다.
**`VM.GuestAgent.Unrestricted`**가 프로비저닝이 에이전트를 통해 게스트의 호스트 키를 읽게
해 주는 권한이다. 이것이 없으면 파이프라인이 모든 VM을 호스트 키 단계에 세운다.
**`SDN.Use`**는 게스트 NIC이 붙는 브리지를 덮는다.

넘어가기 전에 확인한다. 실제로 동작해야 하는 것은 토큰이다.

```sh
pveum acl list          # 네 행, type=user, ugid pickle@pve
pveum user token list pickle@pve   # privsep 0
```

ACL은 토큰이 아니라 **사용자**에 붙으므로 나중의 토큰 교체가 ACL을 건드리지 않는다
(비밀 교체 런북(비공개 레포) §2b가 그것에 의존한다).

### 8. 빈 상태에서 리버스 프록시

리버스 프록시 재구축 런북(비공개 레포)는 가장 최근 백업 아카이브에서 nginx를
*복원*하고, 아카이브가 자기 출처라는 것을 숨기지 않는다. 첫 환경에는 그것이 막다른 길처럼
읽히지만 아니다. **그 컨테이너의 모든 플랫폼 설정 파일은 레포지토리에 출처가 있다.**
아카이브는 재구축의 편의이지 그 상태에 이르는 유일한 길이 아니다.

| 파일 | 쓰는 주체 |
|---|---|
| `conf.d/pickle-base.conf` | proxy-agent 자신의 배포 스크립트(렌더된 vhost가 필요로 하는 websocket upgrade map과 `pickle.d` include glob) |
| `conf.d/pickle-terminal.conf`, `stream-conf.d/*-sni.conf` | `apply-terminal-ingress.sh`. `$pickle_client_ip`와, :443을 소유하고 PROXY 헤더를 앞에 붙이는 stream SNI 라우터를 정의하는 것도 여기다 |
| `conf.d/pickle-tls.conf` | `apply-tls-ciphers.sh` |
| `conf.d/pickle-ratelimit.conf`, `sites-available/pickle-main*.conf`, `pickle-reject*.conf`, `stream-conf.d/pickle-stream-limits.conf`, `snippets/proxy-common.conf` | `apply-main-domain-vhost.sh` |
| `pickle.d/<fqdn>.conf` | proxy-agent가 런타임에. 발행된 도메인마다 하나 |

그래서 빈 컨테이너의 순서는 이렇다. 컨테이너를 만들고(Debian, nginx, 값 표의 인프라
브리지 주소), proxy 에이전트를 배포해서(10단계) base 설정이 내려앉게 한 다음, apply
스크립트 셋을 돌린다(11단계). Origin CA 와일드카드 쌍은 플랫폼 루트마다
`/etc/nginx/pickle-certs/<루트, 점을 하이픈으로>.{crt,key}`에 설치한다. 12단계의 인벤토리가
그것 없이는 거부하고, proxy 에이전트도 자기 환경에 그 이름이 있어야 한다.

두 가지는 어떤 레포지토리에도 없고, 둘 다 플랫폼이 아니라 이 플랫폼이 자란 호스트에
고유하다.

- 이 프록시를 함께 쓰는 **레거시 테넌트 vhost**. 새 환경에는 그런 테넌트가 없고 이것이
  하나도 필요하지 않다. 다만 주석 11을 본다. apply 스크립트 하나가 그 테넌트의 응답을
  실행 전에 확인한다.
- 순정 Debian 파일을 넘어선 `nginx.conf` 자체. `stream-conf.d/`를 include하는 `stream {}`
  블록과, reload가 웹 터미널 websocket을 끊지 않을 만큼 높게 잡은
  `worker_shutdown_timeout`이다. 둘 다 한 줄씩이고 `pickle-base.conf`의 주석에 이름이 있다.

### 11. 인그레스와 호스트 정책

단계 안의 순서가 중요하고 각 스크립트 헤더에 적혀 있다. 터미널 인그레스 배관은 앱 vhost를
쓰지 않으므로 `apply-main-domain-vhost.sh`가 돌기 전까지 플랫폼은 아무것도 응답하지 않는다.
그 스크립트가 최종 인그레스 상태를 소유하고 vhost를 쓰는 것들 중 **마지막**에 돈다.

새 환경은 `apply-main-domain-vhost.sh`에 자기 `PICKLE_MAIN_DOMAIN`과
`PICKLE_LEGACY_TENANT_HOST=none`을 넘긴다. 기본값은 이 배포를 재현한다. 이 단계의 나머지
두 스크립트는 같은 변수에서 컨테이너 id와 주소를 읽는다.

그 안의 LE 발급이 함께 요구하는 것: 0단계의 DNS 레코드와 학교 방화벽 개방이 이미 살아
있어야 하고, 호스트에서 공개 이름으로 닿으려면 pve-node의 `/etc/hosts` 헤어핀 항목
(`198.18.1.10 pickle.pusan.ac.kr`)이 있어야 한다. 캠퍼스 NAT는 헤어핀하지 않는다.

#### 11a. 타이머와 보존 기간

`apply-ops-timers.sh`와 `apply-log-retention.sh`는 **다른 어떤 절차도 이름을 부르지
않는다.** 어떤 구축 순서에서도 이 단계가 그 둘의 유일한 자리다. 첫 번째를 건너뛰면 새
환경에 야간 DB 백업도, 헬스 체크 타이머도, 실패 표시도, 로그인 알림도 없고 **아무것도
그 사실을 말해 주지 않는다.** 두 번째를 건너뛰면 journald와 로그 증가에 상한이 없다. 둘 다
컨테이너가 생긴 뒤에 돌리고, 보존 스크립트는 컨테이너를 재구축할 때마다 다시 돌린다.

### 12. 데이터베이스 부트스트랩

[db-restore.md](db-restore.md)의 백지 부트스트랩 절을 **거기 적힌 번호 순서대로** 따른다.
스크립트별 환경 변수 표와 건너뛰면 무엇이 깨지는지의 열이 거기 있고 여기서 반복하지
않는다. 실제로 수행한 구축에서 나온 두 가지만 덧붙인다.

- 실측한 순서 전체: api 정지 → 데이터베이스 drop 후 create → `deploy-api.sh`(클린 빌드,
  첫 기동이 Flyway 실행) → **dev 프로파일 api에서만**: 개발용 시더가 채운 것을 지운다
  (`delete from user_consents; delete from terms_versions; delete from settings;
  delete from os_images;`. 시더가 첫 기동에 빈 테이블을 채우고, 부트스트랩 스크립트는
  설계상 남의 행을 넘겨받지 않는다) → `apply-platform-inventory.sh`
  (`PICKLE_RELAY_PUBLIC_HOST` 필수) → `apply-settings.sh` → `apply-terms.sh` →
  `apply-os-catalog.sh` → OS 하나 활성화 → 킬 스위치 켜기 → `scripts/apply-relay-token.sh`
  → 기관 생성(콘솔) → 스모크.
- 새 환경이 `dev`와 `prod` 중 어느 프로파일로 도는지가 그 자체로 **정해지지 않은 기준**이다.
  두 번째 호스트가 어느 프로파일이어야 하는지를 아무것도 말하지 않고, 위의 정리 단계는
  오직 `dev` 때문에 존재한다. 이 단계 전에 프로파일을 정하고 그 결정을 기록한다. `prod`
  에서는 개발용 시더가 돌지 않으므로 정리 단계를 건너뛴다.

### 13. 최초 운영 데이터

백지 순서는 스크립트가 쓴 행들로 끝난다. 콘솔과 API만 할 수 있는 것을 운영자가 더하기
전까지 플랫폼은 아직 쓸 수 없다.

- **기관.** `vm_requests.org_id`는 not null이고 prod에서는 아무것도 기관을 시드하지 않는다.
  기관이 0개면 전부 초록불이어도 **아무도 VM을 요청할 수 없다.** 관리자 콘솔에서 최소
  하나를 만든다.
- **사양 프리셋(flavor).** 이것도 prod에서는 시드되지 않는다. 관리자 콘솔에서 만든다.
  없으면 요청 폼이 빈 채로 뜬다.
- **최초 SYS_ADMIN.** `prod`에서는 api가 `/etc/pickle/api.env`의
  `PICKLE_BOOTSTRAP_ADMIN_EMAIL`과 `PICKLE_BOOTSTRAP_ADMIN_PASSWORD`로 정확히 하나를
  시드하고, 값이 없거나 추측 가능하면 기동을 거부한다. 그러니 실제로는 5단계의 입력이고
  여기서는 로그인을 확인한다. `dev`에서는 개발용 시더의 계정
  (`PICKLE_SEED_SYSADMIN_*`)이 대신 존재한다.
- **킬 스위치.** `ssh_gateway_enabled`, `web_terminal_enabled`, `port_forwarding_enabled`는
  설정 스크립트가 의도적으로 **꺼진** 채로 쓴다. 각 경로가 증명된 뒤에만 하나씩 켠다
  (14단계의 스모크 테스트가 그 증명이다).

### 14. 증명

스모크 테스트는 호스트에서 root로 돌린다(게스트 브리지가 필요하다). 기본값은 헤어핀
항목을 통해 주 도메인을 향한다. 선행 조건과 재정의할 변수는 [README.md](../README.md)의
스모크 절을 본다. 새 환경의 최소 묶음을 순서대로 적으면 `smoke-provisioning.sh`
(관통 증명), `smoke-llm-key-lifecycle.sh`, `smoke-http-publish.sh`,
`smoke-ssh-gateway.sh`, `smoke-web-terminal.sh`다. 그다음 `health-check.sh`로 스냅샷을
찍고, 백업 타이머의 첫 실행이 표시와 덤프를 남겼는지 확인하고, 의도적으로 호스트를 한 번
재부팅한 뒤 네트워크 런북(비공개 레포)의 재부팅 후 점검을 다시 돌린다. 첫 재부팅까지만
견디는 플랫폼은 아직 구축된 것이 아니다.
