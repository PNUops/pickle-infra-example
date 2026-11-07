# 런북: 신규 환경 구축 (빈 호스트에서 동작하는 플랫폼까지)

API와 console 배포는 [명시 환경 artifact 절차](deploy-environments.md)를 따라요.
`deploy-api.sh`와 `deploy-console.sh`는 빌드하지 않으며 check/apply와 dev/prod, 실제 commit,
보호 staging artifact와 SHA를 요구해요. 아래 초기 단일 호스트 CT 배치와 과거 재구축 기록은
현재 보존 source를 재시작할 권한이 아니에요.

빈 호스트에서 플랫폼 전체를 세울 때 필요한 순서와 의존, 환경별 입력값을 안내해요.
Proxmox 설치부터 VM 프로비저닝과 콘솔, SSH, 재부팅 뒤 검증까지 다뤄요.
기존 절차는 **다시 적지 않고** 각 단계를 담당하는 런북이나 스크립트로 연결해요.
원래의 검증 상태와 계정 구성, 재구축 결과는 당시 기록으로 보존해요. 공개 사본에 포함된 설정과 스크립트를 기준으로 필요한 조건을 확인해요.

새 플랫폼 전체가 되는 Proxmox 노드를 대상으로 해요. 이미 있는 플랫폼에 더하는
Proxmox 노드는 [proxmox-node-intake.md](proxmox-node-intake.md)를 따르고(그 노드에는 이
문서의 12단계 스크립트를 실행하면 안 돼요), Proxmox 노드가 아닌 호스트는
[node-intake.md](node-intake.md)를 따라요.

문서의 아래 세 표시를 구분해요.

- **HUMAN**. 구매와 계정 생성, 학교에 넣는 요청, 물리 작업처럼 운영자만 할 수 있는 단계예요.
  스크립트나 세션이 대신할 수 없으므로 미리 일정에 잡아요.
- **BLOCKED**. 커밋된 절차가 없는 단계예요. 빠진 내용과 완료 조건을 기록해요.
  **기억이나 짐작으로 절차를 즉석에서 만들지 마세요.** 호스트나 프록시 구성이 잘못되면
  그 위 계층의 문제로 보일 수 있어요.
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

위에서 아래 순서로 진행해요. "선행" 열의 조건을 먼저 충족해야 해요. 상세 조건과
빠진 내용은 표 아래 번호 주석에서 확인해요.

| # | 단계 | 절차 | 선행 |
|---|---|---|---|
| 0 | **HUMAN** 이름, 계정, 접근 권한 확보 | 주석 0, 일부 **BLOCKED** | 없음 |
| 1 | **HUMAN** 물리 호스트: 디스크, OS와 Proxmox VE 설치, sshd 포트, 캠퍼스 네트워크 | 주석 1. 결과 상태만 기록되어 있고 설치 프로그램 선택은 미기록 | 0 (캠퍼스 IP, 방화벽 요청 접수) |
| 2 | 호스트 브리지, NAT, 방화벽 | [`hosts/pve-node/interfaces`](../hosts/pve-node/interfaces)를 값 표에 맞게 바꿔 설치. 관리 접속 경로를 보존하고 재부팅 뒤 bridge와 NAT, 방화벽을 같은 설정과 대조 | 1 |
| 3 | 배포용 체크아웃 생성과 vault 해제 | 주석 3 | 1 |
| 4 | api용 Proxmox API 계정, 역할, ACL | 주석 4 | 1 |
| 5 | 앱 컨테이너 (PostgreSQL + api + 콘솔 nginx) | `scripts/create-app-lxc.sh` 실행 후 vault에서 `/etc/pickle/api.env`를 채워요(또는 비밀을 새로 발급) | 2, 3 |
| 6 | SSH 게이트웨이 컨테이너 (sshpiperd + WireGuard 종단) | `scripts/create-sshgw-lxc.sh`. 릴레이가 필요로 하는 WG 공개키를 출력해요. `/etc/pickle/sshgw.env`를 채워요 | 2, 3 |
| 7 | **HUMAN** 릴레이 인스턴스 생성 후 기동 | 공개 relay 템플릿으로 6단계의 WG와 페어링하고 HAProxy와 방화벽을 준비해요. 에이전트는 `scripts/deploy-relay.sh` | 0, 6 |
| 8 | 리버스 프록시 컨테이너 (공개 웹 진입점) | 주석 8. 컨테이너를 만들면 설정은 에이전트 배포(10단계)와 apply 스크립트(11단계)로 설치해요. 플랫폼 루트마다 Let's Encrypt 와일드카드를 DNS-01로 발급해요 | 2, 3, 그리고 0 (Cloud DNS 서비스 계정 키) |
| 9 | 사용자 VM 템플릿 | image-builder 레포지토리(공개. 이 호스트에서는 `/srv/pickle` 아래 체크아웃), OS별 프로파일에서 값 표의 템플릿 VMID로. 같은 프로파일과 VMID를 기준으로 재빌드 조건과 기존 템플릿을 대조 | 2 |
| 10 | 서비스 배포 | `scripts/deploy-api.sh`와 `deploy-console.sh`의 명시 환경 artifact 진입점(api 첫 기동에 Flyway 실행), `deploy-proxy-agent.sh`, `deploy-sshgw.sh` | 5, 6, 8. api.env 채워져 있을 것 |
| 11 | 인그레스와 호스트 정책 | `scripts/apply-terminal-ingress.sh` → `scripts/apply-main-domain-vhost.sh`(주석 11) → `scripts/apply-tls-ciphers.sh`. 그다음 `scripts/apply-log-retention.sh`와 `scripts/apply-ops-timers.sh`(주석 11a) | 8, 10. 0 (LE 발급을 위해 DNS와 방화벽이 살아 있을 것) |
| 12 | 데이터베이스 부트스트랩: 인벤토리, 설정, 약관, OS 카탈로그, 릴레이 토큰 | [db-restore.md](db-restore.md)의 백지 부트스트랩 절, 거기 적힌 번호 순서 전부. dev 프로파일 정리와 실측 순서는 주석 12 | 5에서 10 완료. 인증서 행 때문에 8 |
| 13 | 최초 운영 데이터 | 주석 13. OS 하나 활성화, 킬 스위치 켜기, 기관 1개 이상과 사양 프리셋 생성, 관리자 계정 확인 | 12 |
| 14 | 스모크 테스트, 헬스 스냅샷, 첫 백업 | 주석 14 | 13 |

## 환경마다 바뀌는 값

이 문서가 참고한 환경의 값과 해당 파일이에요. 새 환경에서는 1단계 전에
각 값을 **한 번에** 정해요.

| 값 | 이 환경 | 나타나는 곳 |
|---|---|---|
| 게스트 씬풀과 그 볼륨 그룹 | `pve/data`, VG `pve` | `scripts/health-check.sh`의 `THINPOOL_LV`(`vg/lv` 형태여야 해요. `THINPOOL_VG`를 함께 주지 않으면 VG를 여기서 구해요). 게스트 스토리지 이름이 다른 호스트는 이것을 설정할 때까지 씬풀 행에 계속 오류가 표시돼요. 스크립트는 볼륨 그룹만 적은 값도 거부해요 |
| LVM 질의 상한 | 10초 (`LVM_TIMEOUT`) | `scripts/health-check.sh`. 각 `lvs`와 `vgs` 읽기의 상한이에요. 용량 소진으로 볼륨이 멈춰도 스냅샷이 무한히 기다리지 않도록 제한해요. 정상 호스트가 실제로 더 필요할 때만 올려요 |
| DNS 레코드가 가리키는 캠퍼스 공인 IP | 운영자가 보유 (Cloud DNS 콘솔과 학교 DNS) | 플랫폼 루트의 apex A와 serving 이름별 A, 수동 ingress 이름의 개별 A, 학교 방화벽 요청(0단계), `health-check.sh`의 `MAIN_DOMAIN_PUBLIC_IP`, 릴레이 엣지 방화벽. wildcard A는 새 환경의 필수 레코드가 아니에요. 개별 A와 DNS-01은 [플랫폼 DNS 런북](platform-dns.md)으로 따로 검증해요 |
| 호스트 LAN 주소, 게이트웨이, NIC 이름 | `192.0.2.10/24`, gw `192.0.2.1`, `nic0` | [`hosts/pve-node/interfaces`](../hosts/pve-node/interfaces)의 vmbr0 스탠자 |
| 인프라 브리지 망 (vmbr1) | `198.18.0.0/16`, 호스트 `.0.1`. 프록시 `.1.10`, 앱 `.1.20`, sshgw `.1.30` | `hosts/pve-node/interfaces`(NAT, DNAT, FORWARD 규칙이 `.1.10`을 고정해요), `create-app-lxc.sh`, `create-sshgw-lxc.sh`, 프록시 컨테이너의 bridge 주소 대조, `apply-terminal-ingress.sh`, `apply-main-domain-vhost.sh`(`PICKLE_PROXY_IP`, `PICKLE_HOST_PROBE_IP`). **릴레이 쪽에도 있어요.** `lightsail/wireguard/wg0.conf.template`이 api의 `.1.20/32`를 터널로 들여보내는데 그것이 `PICKLE_RELAY_SYNC_URL`이 가리키는 주소예요 |
| 게스트 브리지 망 (vmbr2) | `198.19.0.0/16`, 호스트 `.0.1` | `hosts/pve-node/interfaces`. `PICKLE_POOL_CIDR`, `PICKLE_POOL_GATEWAY`, `PICKLE_POOL_RESERVED`(`apply-platform-inventory.sh`) |
| WireGuard 전송 망 | `100.64.0.0/30`. 릴레이 `.1`, sshgw `.2` | `create-sshgw-lxc.sh`, `lightsail/wireguard/wg0.conf.template`(그 `AllowedIPs`는 게스트 망과 **api 주소**도 담으므로 항목이 틀리면 터널이 아니라 릴레이 sync가 실패해요), `lightsail/haproxy/haproxy.cfg.template`(`server sshgw 100.64.0.2:22`), `lightsail/nftables/nftables.conf`, `hosts/pve-node/interfaces`(`/30` 라우트와 `.1` FORWARD accept), `PICKLE_RELAY_SOURCE_IP` |
| 주 진입 도메인 | `pickle.pusan.ac.kr` | `apply-main-domain-vhost.sh`의 `PICKLE_MAIN_DOMAIN`. `create-sshgw-lxc.sh`의 `PICKLE_TERMINAL_CONSOLE_ORIGIN`. **터미널 브리지는 이 origin만 받아들이고 그 컨테이너는 11단계보다 훨씬 앞인 6단계에서 만들어지므로** 여기 값이 낡으면 웹 터미널을 사용할 수 없어요. `create-app-lxc.sh` 콘솔 vhost의 `server_name`, `health-check.sh`(`PICKLE_DEV_DOMAIN` 기본값과 Let's Encrypt 인증서 경로), 그리고 이 이름에서 200을 요구하는 `apply-tls-ciphers.sh`의 전후 단정. 스모크 테스트 기본값(`BASE`), pve-node의 `/etc/hosts` 헤어핀 항목 |
| 플랫폼 루트 도메인 | `pusan.dev` | `PICKLE_ROOT_DOMAIN`(`apply-platform-inventory.sh`와 `apply-settings.sh`. 같은 값을 써요), 프록시 에이전트 환경의 `PICKLE_PROXY_AGENT_WILDCARD_CERTS`(형식은 `<root>=<crt>:<key>`이고 에이전트는 그 루트에 대해 렌더링하기 전에 이 설정이 필요해요. 해당 root와 인증서 경로의 사전 대조), `ROOT`(`smoke-http-publish.sh`), `PLATFORM_ROOT_DOMAIN`(`health-check.sh`), 계열 경로 `/etc/letsencrypt/live/<루트>/{fullchain,privkey}.pem`(`certbot --cert-name <루트>`가 정해요), Cloud DNS 관리 존과 certbot 서비스 계정의 범위 |
| 플랫폼 DNS 검증 | 환경별 값 | `/etc/pickle/host.env`의 `PLATFORM_DNS_MODE`, 정확한 NS 목록 `PLATFORM_DNS_EXPECTED_NS`, 수동 ingress 목록 `PLATFORM_DNS_MANUAL_FQDNS`. 기본 모드는 `explicit`이고 전환과 롤백 때만 `wildcard`를 사용해요. NS는 해당 관리 존의 전체 집합을 기록해요 |
| 플랫폼 루트 도메인의 소유 기관 | `예시 기관` | `PICKLE_ROOT_ORG`(`apply-platform-inventory.sh`, 필수. 기본값 없음). `orgs.name`과 정확히 한 행이 일치해야 하고, 그 행이 `domain_roots.org_id`가 돼요. **루트 아래 발급되는 이름과 그 신청의 승인 큐는 모두 그 기관에 속해요.** 기관을 먼저 만들어 두지 않으면 이 단계가 실패해요 |
| 사용자 SSH 호스트 | `ssh.example.dev` (DNS 전용 A 레코드에서 릴레이 고정 IP로) | 릴레이의 사용자 SSH 진입 설정, api의 `PICKLE_SSH_HOST`(재정의 지점). **비어 있으면 api가 컴파일된 기본값으로 폴백해요.** meta 엔드포인트와 알림 문구 양쪽에서 그렇게 되므로, 값을 설정하지 않으면 오류 대신 잘못된 호스트를 안내할 수 있어요. 콘솔은 비인증 랜딩 페이지용 상수를 따로 사용해요 |
| 릴레이 공개 호스트 (포트 포워딩) | `ssh.example.dev`. 위 사용자 SSH 호스트와 같은 이름이에요. 둘 다 릴레이를 가리켜요 | `PICKLE_RELAY_PUBLIC_HOST`(`apply-platform-inventory.sh`, 필수. 기본값이 없고 이 값을 변경하는 API도 없음) |
| 릴레이 고정 IP와 관리 SSH | `198.51.100.10`, 관리 sshd `:22`, 키 `$VAULT/lightsail-ssh.pem` | `RELAY_HOST`, `RELAY_SSH_PORT`, `RELAY_SSH_KEY`(`deploy-relay.sh`). **이름이 다른 두 번째 묶음** `PICKLE_RELAY_SSH_KEY`, `_USER`, `_PORT`(`apply-relay-token.sh`). `RELAY`(`smoke-ssh-gateway.sh`, 14단계 묶음). sshgw `wg0.conf`의 `Endpoint`. 공개 relay 템플릿의 주소와 포트 대조 |
| 컨테이너 ID | 프록시 `100`, 앱 `101`, sshgw `102` | 생성, 배포, 백업 스크립트의 `CTID`. 각각 자기 컨테이너를 기본값으로 사용해요. 모든 apply 스크립트의 `PICKLE_PROXY_CTID`와 `PICKLE_APP_CTID`. `apply-relay-token.sh`의 `PICKLE_TUNNEL_CTID`도 터널 용도로 쓰는 sshgw 컨테이너를 가리켜요. 대부분의 런북에는 값이 직접 들어 있어요 |
| 템플릿 VMID | `1001`에서 `1005` (Ubuntu 24.04/26.04/22.04, Debian 13/12. `1000`은 폐기된 이전 판) | `apply-os-catalog.sh`의 `CATALOG`(이름과 VMID 둘 다), image-builder의 OS별 프로파일, 같은 OS 프로파일과 VMID의 재빌드 조건 대조 |
| Proxmox 노드 이름 | `pve-node` | `PICKLE_NODE`. 노드 API 인증서의 SAN이어야 하고 LXC 101 안에서도 해석되어야 해요(`create-app-lxc.sh`가 쓰는 `/etc/hosts` 항목) |
| 스토리지 | `local-lvm` | `PICKLE_NODE_STORAGE`, 생성 스크립트, 템플릿 빌드 |
| 호스트 관리 SSH 포트 | `22` | 호스트 sshd 설정(**이것을 설정하는 커밋된 절차가 없어요.** 주석 1). `hosts/pve-node/interfaces`의 방화벽 규칙이 이 값을 전제로 해요 |

## 단계별 주석

### 0. 이름, 계정, 접근 권한 확보 (HUMAN, 일부 BLOCKED)

1단계 전에 아래 항목을 확보해요. 학교 쪽 공인 IP와 그 IP로 열린 인바운드 80/443,
위임된 주 진입 도메인, 플랫폼 루트 도메인을 산 등록기관 로그인과 그 도메인의 NS 를 위임할
수 있는 권한, 플랫폼 루트의 관리 존을 가진 GCP 프로젝트와 그 존에 쓰기 권한이 있는 certbot
용 서비스 계정(키 JSON 은 vault `gcp/pickle-certbot-dns01.json`), SSH 호스트 도메인과 릴레이
호스트 도메인이 등록된 존, Lightsail 릴레이를 만들 수 있는 AWS 계정, 그리고 SMTP 발신
계정(앱 비밀번호).

**일부 BLOCKED: 취득 경로가 아직 다 적혀 있지 않다.** 어떤 외부 계정이 존재하고 누가
그것을 갖고 있는지는 2026-09-06 에 기록됐으므로 더 이상 기억에 의존하지 않는다. 남은
것은 둘이다. 학교 공인 IP 와 도메인, 방화벽 작업의 요청 창구와 소요 기간이 없고, 구매한
도메인들의 갱신 주기와 만료일이 없다. 뒤쪽은 조용히 실패하는 종류다. 도메인 하나가
갱신되지 않으면 그 순간 이름과 인증서가 함께 사라지는데, 지금 그것을 지켜보는 장치가
없다. 적을 때는 값이 아니라 위치를 적는다. 소요 기간 경고는 그대로 유효하다. 학교 요청은
며칠에서 몇 주 단위이므로 가장 먼저 넣는다.

### 1. 물리 호스트 (HUMAN, 일부 복원됨)

빈 기계를 "2단계를 돌릴 수 있는" 상태로 만드는 절차는 커밋된 적이 없어요.
아래는 이 문서가 참고한 호스트의 결과 상태예요. 설치 때의 선택은 기록되어 있지 않고,
물리 작업은 HUMAN 단계예요.

**문서가 참고한 호스트 구성 기록**

| | 이 호스트 |
|---|---|
| Proxmox VE | Debian trixie 위 9.2 |
| 리포지토리 | `pve-no-subscription` 활성화. enterprise와 Ceph 소스는 비활성화 |
| 디스크 | 두 개. 설치가 올라간 977 GB 장치와 **현재 미사용인** 1.8 TB 장치 |
| 스토리지 | `local`(디렉터리, 96 GB 루트 LV 위)과 `local-lvm`(LVM-thin, 약 839 GB). 플랫폼 변수들이 기본값으로 쓰는 이름이다 |
| 루트와 스왑 | 96 GB 루트 LV, 8 GB 스왑 LV, 나머지는 씬풀 |
| 관리 sshd | 포트 22. 이 예시의 관리 접속 포트 |

**새 호스트에서 결정할 항목**

- 설치 프로그램 선택. 파일시스템과 볼륨 그룹 크기, 루트와 스왑의 크기는 새 기계의 디스크에 맞게 정해요.
- 두 번째 장치의 용도. 참고한 호스트에서는 미사용 상태였으므로 새 환경의 용도를 따로 정해요.
- 관리 sshd를 기본 포트에서 옮기는 것. 포트 자체는 값 표에 있고, **잘못 바꾸면 작업 중인 세션이 끊길 수 있어요.**
  새 포트를 기존 포트와 나란히 열어요. 두 번째 세션에서 새 포트의 접속을 확인한 뒤에만 기존 포트를 없애요.
- 캠퍼스 쪽 연결. 주소와 방화벽 개방, 승인 주체는 주석 0에서 확인해요.

초기 셋업 로그가 하나 있으나 네트워크 재번호보다 앞선 것이라, 그대로 따라가면 사용자
망과 인프라 망이 뒤바뀐 호스트가 나온다. 값이 아니라 절차의 형태로만 취급한다.

### 3. 체크아웃과 vault

이 레포의 스크립트는 호스트의 `/srv/pickle` 레이아웃을 전제로 해요. `/srv/pickle/<repo>` 아래에
이 레포와 이 레포가 배포하는 서비스 레포들(api, console, sshgw, proxy-agent, relay-agent,
image-builder)의 체크아웃이 필요해요. git-crypt 비밀 vault는 `$VAULT`에 있어야 해요. clone한 다음 **HUMAN** 단계에서 운영자가 보관한 git-crypt 키를 대역 밖으로 전달해요.
vault가 잠겨 있으면 5단계에 설치할 비밀이 없고 `deploy-relay.sh`가 거부해요(SSH 키 없음).
빈 vault로 시작하는 새 환경은 필요한 비밀을 모두 새로 발급해야 해요.
각 생성과 배포 스크립트의 필수 입력을 확인해요.

### 4. api용 Proxmox API 계정

새 서비스 principal의 권한 분리와 token custody는 [Proxmox API principal 런북](proxmox-api-principal.md)을 따라요.
아래 pve1 계정 구성은 이 문서가 참고한 원래 구성의 기록이에요. 새 등록 절차와 구분해 읽어요.

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

빈 프록시 컨테이너는 아래 설정 파일의 생성 주체를 따라 구성해요.
**플랫폼 설정 파일은 이 레포지토리와 proxy-agent에 출처가 있어요.**
기존 백업 아카이브 없이 처음 구성할 때도 해당 출처를 확인해요.

| 파일 | 쓰는 주체 |
|---|---|
| `conf.d/pickle-base.conf` | proxy-agent 자신의 배포 스크립트(렌더된 vhost가 필요로 하는 websocket upgrade map과 `pickle.d` include glob) |
| `stream-conf.d/*-sni.conf` | `apply-terminal-ingress.sh`. :443을 소유하고 PROXY 헤더를 앞에 붙이는 stream SNI 라우터예요. 폐지된 CDN 시절의 `conf.d/pickle-terminal.conf` 와 그 대역 목록이 남아 있으면 같은 스크립트가 지워요 |
| `conf.d/pickle-tls.conf` | `apply-tls-ciphers.sh` |
| `conf.d/pickle-ratelimit.conf`, `sites-available/pickle-main*.conf`, `pickle-reject*.conf`, `stream-conf.d/pickle-stream-limits.conf`, `snippets/proxy-common.conf` | `apply-main-domain-vhost.sh` |
| `pickle.d/<fqdn>.conf` | proxy-agent가 런타임에. 발행된 도메인마다 하나 |

빈 컨테이너는 Debian과 nginx, 값 표의 인프라 브리지 주소로 만들어요.
proxy 에이전트를 배포해(10단계) base 설정을 설치한 다음, apply 스크립트 셋을 실행해요(11단계). Let's Encrypt 와일드카드는 플랫폼 루트마다 DNS-01 로
`/etc/letsencrypt/live/<루트>/` 에 발급해요(Cloud DNS 서비스 계정 키가 필요하며 DNAT가 열려 있을 필요는 없어요). 12단계의 인벤토리는
그 인증서가 없으면 적용을 거부해요. proxy 에이전트의 환경에도 해당 경로가 있어야 해요.

아래 두 가지는 이 플랫폼이 사용한 호스트에 고유하며 레포지토리만으로 재현할 수 없어요.

- 이 프록시를 함께 쓰는 **레거시 테넌트 vhost**. 새 환경에는 그런 테넌트가 없고 이것이
  하나도 필요하지 않아요. 다만 주석 11도 확인해요. apply 스크립트 하나가 그 테넌트의 응답을
  실행 전에 확인해요.
- 순정 Debian 파일을 넘어선 `nginx.conf` 자체. `stream-conf.d/`를 include하는 `stream {}`
  블록과, reload가 웹 터미널 websocket을 끊지 않을 만큼 높게 잡은
  `worker_shutdown_timeout`이에요. 둘 다 한 줄이며 `pickle-base.conf`의 주석에서 확인해요.

### 11. 인그레스와 호스트 정책

단계 안의 순서는 각 스크립트 헤더를 확인해요. 터미널 인그레스 배관은 앱 vhost를
쓰지 않으므로 `apply-main-domain-vhost.sh`가 돌기 전까지 플랫폼은 아무것도 응답하지 않아요.
그 스크립트가 최종 인그레스 상태를 소유하며 vhost를 쓰는 도구 중 **마지막**에 실행돼요.

새 환경은 `apply-main-domain-vhost.sh`에 자기 `PICKLE_MAIN_DOMAIN`과
`PICKLE_LEGACY_TENANT_HOST=none`을 넘겨요. 기본값은 이 문서가 참고한 배포를 재현해요.
나머지 두 스크립트도 같은 변수에서 컨테이너 id와 주소를 읽어요.

LE 발급 전에는 0단계의 DNS 레코드와 학교 방화벽 개방이 준비되어 있어야 해요.
호스트에서 공개 이름으로 닿으려면 pve-node의 `/etc/hosts` 헤어핀 항목
(`198.18.1.10 pickle.pusan.ac.kr`)이 있어야 해요. 캠퍼스 NAT는 헤어핀하지 않아요.

#### 11a. 타이머와 보존 기간

`apply-ops-timers.sh`와 `apply-log-retention.sh`는 **이 단계에서 별도로 실행해야 해요.**
첫 번째를 건너뛰면 야간 DB 백업과 헬스 체크 타이머, 실패 표시, 로그인 알림이 없으며
**누락 자체도 알리지 않아요.** 두 번째를 건너뛰면 journald와 로그 증가에 상한이 없어요.
둘 다 컨테이너를 만든 뒤 실행하고, 보존 스크립트는 컨테이너를 재구축할 때마다 다시 실행해요.

두 스크립트 모두 대상 인자를 받는다. 인자 없이 돌리면 pve-node 의 개발 환경이다(TLS 는 LXC 100·101,
로그 보존은 pve-node 과 LXC 100~103).
운영 host 에서는 `apply-tls-ciphers.sh production` 과 `apply-log-retention.sh production` 을,
그 밖의 플랫폼 host(대기 노드 등)에서는 `apply-log-retention.sh host` 를 돌린다. 2026-10-24
전에는 대상이 pve-node 번호로 고정되어 있어서 이후에 만든 host 와 운영 컨테이너는 어느 쪽 정책도
받지 못했다. 절차는 [production-ops.md](production-ops.md) 의 "TLS 암호군과 로그 보존" 절이다.

### 12. 데이터베이스 부트스트랩

[db-restore.md](db-restore.md)의 환경과 스키마 조건을 먼저 확인하고, 적용할 수 있는 백지 부트스트랩을
**거기 적힌 번호 순서대로** 따라요. 스크립트별 입력과 생략할 때의 영향도 해당 절에서 확인해요.
아래 실제 재구축 순서는 당시 기록으로 보존해요.

- 실측한 순서 전체: api 정지 → 데이터베이스 drop 후 create → `deploy-api.sh`(클린 빌드,
  첫 기동이 Flyway 실행) → **dev 프로파일 api에서만**: 개발용 시더가 채운 것을 지운다
  (`delete from user_consents; delete from terms_versions; delete from settings;
  delete from os_images;`. 시더가 첫 기동에 빈 테이블을 채우고, 부트스트랩 스크립트는
  설계상 남의 행을 넘겨받지 않는다) → `apply-platform-inventory.sh`
  (`PICKLE_RELAY_PUBLIC_HOST` 와 `PICKLE_ROOT_ORG` 필수) → `apply-settings.sh` →
  `apply-terms.sh` → `apply-os-catalog.sh` → OS 하나 활성화 → 킬 스위치 켜기 →
  `scripts/apply-relay-token.sh` → 기관 생성(콘솔) → 스모크.
- **위 순서는 그때의 실측이고 지금 그대로 따르면 멈춘다.** 도메인 루트 등록이 나중에
  `apply-platform-inventory.sh` 에 붙으면서 그 스크립트가 `PICKLE_ROOT_ORG` 가 가리키는
  `orgs` 행을 요구하게 됐는데, 위 순서는 기관을 맨 뒤에 만든다. 일치하는 행이 0개이거나 2개
  이상이면 스크립트가 0이 아닌 코드로 끝나고, 그 실패는 백업을 뜬 뒤에 나므로 아무것도
  담기지 않은 덤프를 가리키는 안내가 함께 나온다. **기관 생성을 `apply-platform-inventory.sh` 앞으로
  옮긴다.** 콘솔이 아직 없으면 그 행만 데이터베이스에 직접 넣는다.
- 새 환경의 `dev`와 `prod` 중 사용할 프로파일은 이 단계 전에 정하고 기록해요. 위의 정리 단계는
  오직 `dev` 때문에 필요해요. `prod`에서는 개발용 시더가 돌지 않으므로 정리 단계를 건너뛰어요.

### 13. 최초 운영 데이터

스크립트가 기록한 행 외에도 운영자가 콘솔과 API에서 아래 항목을 준비해야 해요.

- **기관.** VM 신청에는 ACTIVE 기관이 필요하며 prod는 기관을 자동으로 시드하지 않아요.
  기관이 0개면 **VM을 요청할 수 없어요.** 관리자 콘솔에서 최소 하나를 만들어요.
- **사양 프리셋(flavor).** prod는 프리셋을 자동으로 시드하지 않아요. 제공할 기본 사양은 관리자 콘솔에서 만들어요.
  프리셋이 없어도 직접 입력을 선택할 수 있어요. 신청할 OS의 활성화는 별도 조건이에요.
- **최초 SYS_ADMIN.** `prod`에서는 api가 `/etc/pickle/api.env`의
  `PICKLE_BOOTSTRAP_ADMIN_EMAIL`과 `PICKLE_BOOTSTRAP_ADMIN_PASSWORD`로 정확히 하나를
  시드하고, 값이 없거나 추측 가능하면 기동을 거부해요. 따라서 5단계에서 입력하고
  여기서는 로그인을 확인해요. `dev`에서는 개발용 시더의 계정
  (`PICKLE_SEED_SYSADMIN_*`)이 대신 존재해요.
- **킬 스위치.** `ssh_gateway_enabled`, `web_terminal_enabled`, `port_forwarding_enabled`는
  설정 스크립트가 **꺼진** 채로 기록해요. 각 경로가 검증된 뒤에만 하나씩 켜요
  (14단계의 스모크 테스트에서 경로를 검증해요).

### 14. 증명

스모크 테스트는 호스트에서 root로 실행해요(게스트 브리지가 필요해요). 기본값은 헤어핀
항목을 통해 주 도메인으로 접속해요. 선행 조건과 재정의할 변수는 [README.md](../README.md)의
스모크 절을 확인해요. 새 환경의 최소 묶음을 순서대로 적으면 `smoke-provisioning.sh`
(VM 관통 증명), `smoke-llm-key-lifecycle.sh`, `smoke-http-publish.sh`,
`smoke-ssh-gateway.sh`, `smoke-web-terminal.sh`, `smoke-signup.sh`예요. 새 사용자가 필요한
앞의 스모크는 사용자를 DB에 직접 만들므로, 회원가입과 메일 인증은 마지막 `smoke-signup.sh`만
확인해요. 이 스모크는 가입 주소 도메인의 메일이 스모크 메일함으로 라우팅되고 그 메일함의 앱
비밀번호가 볼트에 있어야 실행할 수 있어요. 그다음 `health-check.sh`로 스냅샷을
찍고, 백업 타이머의 첫 실행이 표시와 덤프를 남겼는지 확인하고, 의도적으로 호스트를 한 번
재부팅한 뒤 공개 interfaces 설정과 실제 bridge, NAT, 방화벽, 관리 접속 경로를 다시 대조해요. 첫 재부팅까지만
견디는 것만으로 구축이 완료됐다고 판단하지 않아요.

최종 갱신: 2026-11-08
