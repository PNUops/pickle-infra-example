# pickle-infra-example

부산대학교 클라우드 플랫폼(Pickle)의 인프라 계층이 어떻게 구성돼 있는지 보여주는 공개
예시본입니다. 실제 인프라 레포지토리는 비공개이며, 여기에는 프로비저닝·배포·검증 스크립트와
호스트 설정을 옮겨 담고 민감한 값을 치환해 두었습니다.

> **여기 적힌 IP 주소와 관리 포트, 볼트 경로는 실제 값이 아닙니다.** 아래
> [무엇을 바꿨나](#무엇을-바꿨나)에 치환 목록이 있습니다. 도메인 이름은 치환하지
> 않았습니다 — 이 플랫폼이 서비스하는 이름이라 공개돼 있고, 가려 두면 스크립트가
> 무엇을 하는지 읽을 수 없게 됩니다.

플랫폼 전체는 [PNUops](https://github.com/PNUops) 프로필에서, 애플리케이션 코드는
[pickle-api](https://github.com/PNUops/pickle-api)와
[pickle-console](https://github.com/PNUops/pickle-console)
같은 공개 레포지토리에서 볼 수 있습니다.

## 운영 대상

캠퍼스 방화벽이 인바운드를 막기
때문에 오프캠퍼스 SSH는 외부 릴레이가 받아, 캠퍼스가 아웃바운드로 개통한 WireGuard
터널로 넘깁니다.

```
pickle.example.ac.kr ──┐                   외부 릴레이 (오프캠퍼스 SSH)
*.example.dev        ──┤ :80/:443           HAProxy(send-proxy-v2)
                       ▼                      │ 캠퍼스발 아웃바운드 WireGuard
pve-node (Proxmox VE) ────────────────────────────┘
 ├─ vmbr1 (인프라)
 │   ├─ LXC 100 reverse-proxy  nginx(HTTP vhost·SNI 스트림) + proxy-agent + certbot
 │   ├─ LXC 101 pickle-app     PostgreSQL + pickle-api + nginx(콘솔 정적·/api 프록시)
 │   └─ LXC 102 pickle-sshgw   proxyfront + sshpiperd + 터미널 브리지 + WireGuard 종단
 └─ vmbr2 (사용자, 격리)       사용자 VM (Ubuntu cloud-init 템플릿에서 클론)

비Proxmox 노드:
 ├─ gpu-node   aarch64 GPU 노드        캠퍼스망 192.0.2.20 — vLLM 서빙 :8000 (pickle-vllm)
 └─ dept-node  x86 서버(Ubuntu)        dept-node.example.ac.kr:22 — 학과 공유 서버

Proxmox 노드 후보 (인수 실측만, OS 초기화 전, 미등록):
 ├─ pve-node-2  x86 서버               캠퍼스망 192.0.2.30 — pve-node와 같은 L2
 └─ pve-node-3  x86 서버, GPU 1장      캠퍼스망 192.0.2.31
```

vmbr1은 인프라 전용, vmbr2는 사용자 전용이고 둘 사이에 직접 경로가 없습니다. 산출물은
전부 셸과 마크다운입니다. 주 대상은 Proxmox 호스트이고, 플랫폼에 편입된 비Proxmox
노드의 편입 절차도 함께 다룹니다. 비Proxmox 노드 두 대는 접속 경로만 구성된 상태이고,
플랫폼 서비스는 아직 배치되어 있지 않습니다.

## 주요 기능

플랫폼은 VM 신청·승인·생성, SSH와 웹 터미널 접속, 도메인 공개, 만료와
삭제까지를 다룹니다. 이 레포지토리가 맡는 부분은 아래와 같습니다.

- **프로비저닝**: 호스트 위의 컨테이너와 사용자 VM 템플릿을 스크립트로 만듭니다.
- **배포**: 각 서비스를 올리고, 헬스 체크를 통과하지 못하면 직전 상태로 되돌립니다.
- **정책 적용**: TLS 암호군, 로그 보존, 인그레스 설정 같은 호스트 정책을 한 번에 맞춥니다.
- **점검**: 살아 있는 시스템에 실제 요청을 보내는 스모크와 읽기 전용 헬스 스냅샷으로
  상태를 확인합니다.
- **예약 작업**: 백업과 헬스체크를 타이머로 돌리고, 실패가 조용히 묻히지 않게 기록과
  알림을 붙입니다.

## 동작 방식

- **파괴적 변경 전에 백업합니다.** 설정 파일을 덮어쓰는 스크립트는 손대는 파일을
  타임스탬프 폴더에 먼저 복사합니다.
- **배포는 readiness 게이트를 통과해야 끝납니다.** `deploy-api.sh`는 새 jar로 서비스를 올린 뒤
  readiness가 통과하지 못하면 직전 아티팩트로 되돌립니다. SMTP 같은 외부 dependency는 별도
  전체 health가 감시합니다. `deploy-sshgw.sh`는 바이너리
  세 개를 한 세트로 원자 교체해, 절반만 새 버전인 상태를 만들지 않습니다.
- **정제 검사에 셀프테스트가 붙어 있습니다.** `sanitization-check.sh`는 매 실행마다 합성
  위반 케이스로 자기 검사 로직이 살아 있는지 먼저 확인한 뒤 본 검사를 수행합니다.
- **주기 작업의 실패가 남습니다.** `cron-wrap.sh`가 성공과 실패 마커를 기록하고, systemd
  `OnFailure`가 `ops-unit-failed.sh`로 알림을 띄웁니다.

## 구성

```
hosts/pve-node/       호스트 네트워크 설정, systemd 유닛과 타이머   // 백업·헬스체크·실패 알림
lightsail/        외부 릴레이 설정: HAProxy, nftables, WireGuard, sysctl, 커널 모듈
scripts/          프로비저닝·배포·정책 적용·검증·스모크
runbooks/         운영 절차                                    // 이 예시본에는 일부만 포함
```

호스트별 설정 파일은 `hosts/<이름>/` 아래에 둡니다. 새 노드의 디렉터리는 첫 설정
산출물이 생길 때 만들고, 편입 자체는 `runbooks/node-intake.md`를 따릅니다.

### scripts/

| 분류 | 스크립트 |
|---|---|
| 프로비저닝 | `create-app-lxc.sh`, `create-sshgw-lxc.sh` |
| 배포 | `deploy-api.sh`, `deploy-console.sh`, `deploy-proxy-agent.sh`, `deploy-relay.sh`, `deploy-sshgw.sh`, `sync-systemd-units.sh`, `apply-gpu-node-vllm.sh` |
| 정책 적용 | `apply-tls-ciphers.sh`, `apply-terminal-ingress.sh`, `apply-log-retention.sh`, `apply-main-domain-vhost.sh`, `apply-ops-timers.sh`, `apply-platform-inventory.sh`, `apply-settings.sh`, `apply-terms.sh`, `apply-os-catalog.sh`, `apply-relay-token.sh` |
| 운영 | `db-backup.sh`, `health-check.sh`, `cron-wrap.sh`, `ops-unit-failed.sh` |
| 검증 | `verify.sh`, `sanitization-check.sh`, `hook-verify.sh` |
| 스모크 | `smoke-provisioning.sh`, `smoke-llm-key-lifecycle.sh`, `smoke-http-publish.sh`, `smoke-ssh-gateway.sh`, `smoke-web-terminal.sh`, `smoke-account-ops.sh`, `smoke-dashboards-notify.sh`, `smoke-prod.sh` |

스모크는 목이 아니라 살아 있는 시스템에 실제 요청을 보냅니다. `smoke-provisioning.sh`는
회원가입부터 인증, 워크스페이스 생성, VM 신청, 관리자 승인, 프로비저닝 완료 대기, SSH 도달 확인,
전원 왕복, 삭제, DB 정합 검증까지 한 번에 통과시킵니다.
`smoke-llm-key-lifecycle.sh`는 LLM gateway까지 배포한 뒤 평문 키를 출력하지 않고 실제
1-token 호출과 snapshot 기반 정지·재개·폐기 반영을 확인합니다. 이 일반 lifecycle은
OpenRouter 사업 account가 없어도 실행할 수 있도록 금액 한도를 0으로 둔 TOKEN 축 smoke입니다.
호출 모델은 기본 `pickle-general`이며 다른 TOKEN 모델을 검증할 때만 `LLM_SMOKE_MODEL`로
바꿉니다. 지정한 모델이 gateway `/models`에 없으면 호출 전에 실패합니다.
양수 CREDIT 최초 binding과 cross-org account isolation은 실제 account 준비·binding ON 뒤 별도
smoke를 구현해 검증해야 하며 현재 이 script의 coverage가 아닙니다.

사용자 VM 템플릿을 만드는 빌드 레시피는 이 레포지토리에 없습니다. 공개 레포지토리
**pickle-image-builder**가 그 역할을 맡습니다.

### runbooks/

이 예시본에는 `new-environment.md`(신규 환경 관통 구축 순서 — 환경별로 바꿀 값 표와
사람만 할 수 있는 단계·절차가 없는 지점 명시), `node-intake.md`(비Proxmox 노드 편입 절차 —
실측 체크리스트, 운영자 접속 키 설치, 대역외 관리 평면 점검), `drift-resolution.md`(DB와
하이퍼바이저 상태가 어긋났을 때의 판정 절차), `db-restore.md`(백업 복원),
`gpu-node-vllm.md`(GPU 노드 vLLM 서빙 운영 — 시작·종료, 모델·플래그 교체와 롤백, 장애
복구, 재부팅), `proxmox-node-intake.md`(Proxmox 노드 후보 인수 절차 초안 — 초기화 전
실측, 설치 전 결정 항목, standalone 설치, 등록 전에 실행하면 안 되는 스크립트)를
담았습니다. 나머지 재구축과 복구 절차는 비공개 레포지토리에 둡니다.

## 검증

```bash
scripts/verify.sh        # 모든 셸 스크립트 shellcheck 전수 + 정제·스케줄 유닛 검사
```

`verify.sh`는 커밋 전 필수입니다. shellcheck 위반이 하나라도 있으면 실패하고, 이어서 도는
정제 검사는 이 샘플에 실제 주소나 실제 값이 섞이지 않았는지 확인합니다.

## 무엇을 바꿨나

원본에서 이 레포지토리로 옮기며 치환한 값입니다. 아래는 전부 실제 값이 아닙니다. **도메인 이름은 이 목록에 없습니다**: 플랫폼이 실제로 서비스하는 이름이 그대로 들어 있습니다.

| 항목 | 이 레포지토리의 값 |
|---|---|
| 호스트 LAN 주소·게이트웨이 | `192.0.2.10/24`, `192.0.2.1` |
| 리버스 프록시 공인 IP | `203.0.113.10` |
| 패스스루 대상 IP | `203.0.113.20` |
| 외부 릴레이 공인 IP | `198.51.100.10` |
| 관리 SSH 포트 | `22` |
| 시크릿 볼트 경로 | `$VAULT/` |
| 플랫폼 브리지 대역 | `198.18.0.0/16` |
| 게스트 대역 | `198.19.0.0/16` |
| 릴레이 터널 대역 | `100.64.0.0/30` |
| 호스트 이름 | `pve-node`, `gpu-node`, `dept-node`, `pve-node-2`, `pve-node-3` |
| 비Proxmox 노드 주소·접속명 | `192.0.2.20`, `dept-node.example.ac.kr` |
| Proxmox 노드 후보 주소 | `192.0.2.30`, `192.0.2.31` |
| 비Proxmox 노드 하드웨어 모델 | 아키텍처만 남기고 제조사·모델명 생략 |

`192.0.2.0/24`와 `198.51.100.0/24`, `203.0.113.0/24`는 RFC 5737이 문서화 용도로 예약한
대역이라 실제 인터넷에 존재하지 않습니다. 관리 SSH 포트도 한눈에 자리표시자로 보이도록
표준값을 적었습니다.

내부 대역도 치환했습니다. 사설 대역은 인터넷에서 도달할 수 없지만, 어떤 컨테이너가 어느
주소를 쓰고 터널이 어디로 이어지는지는 그 자체로 내부 지도입니다. 구조는 그대로 두고 숫자만
바꿨으므로(3·4번째 옥텟 유지) 스크립트가 무슨 일을 하는지는 그대로 읽힙니다.
`198.18.0.0/15`는 RFC 2544가 성능 시험용으로, `100.64.0.0/10`은 RFC 6598이 사업자 설비용으로
예약한 대역이라 실제 서비스 주소로 쓰이지 않습니다. LXC 번호와 서비스 포트는 그대로입니다.

검사기(`scripts/sanitization-check.sh`)는 **허용 목록**으로 동작합니다 — 위 자리표시자
대역만 통과하고 사설 대역을 포함한 나머지는 거부합니다. 원본에서 값을 옮겨 오다 실제 주소가
섞이면 그 자리에서 걸립니다.

자격증명 값은 원본에도 없습니다. 인프라 레포지토리는 자격증명을 담지 않고, 볼트 구성은
[pickle-secrets-example](https://github.com/PNUops/pickle-secrets-example)에서 따로
설명합니다.

## 전체 아키텍처

<!-- arch:begin -->
```mermaid
flowchart LR
    subgraph ext [외부]
        B[콘솔 접속]
        V[VM 도메인 접속]
        S[VM SSH 접속]
        PC[VM 포트 접속]
        L[LLM API 호출]
    end

    subgraph relay [오프캠퍼스 릴레이]
        HA[HAProxy :22]
        NFT[nftables DNAT]
        RA[pickle-relay-agent]
    end

    subgraph campus [부산대학교 서버팜]
        PN[Pickle nginx]
        VN[VM nginx]
        C[pickle-console]
        A[pickle-api]
        J[JobRunr]
        G[pickle-sshgw]
        P[pickle-proxy-agent]
        DB[(PostgreSQL)]
        PVE[Proxmox VE]
        VM[사용자 VM]
        IB[pickle-image-builder]
        LG[pickle-llm-gateway]
        UP[업스트림 모델 서버]
    end

    B --> PN
    V --> VN
    S --> HA
    PC --> NFT
    L --> LG

    HA -->|WireGuard| G
    NFT -->|WireGuard| VM
    NFT -. 규칙 적용 .- RA
    RA -->|sync| A

    PN -->|/| C
    PN -->|/api| A
    PN -->|/terminal| G

    G -->|인가 질의| A
    LG -->|키·모델 동기화| A
    LG --> UP
    G --> VM
    VN --> VM

    A --> DB
    A -->|작업 등록| J
    J -->|Proxmox API| PVE
    A -->|도메인 설정| P
    P -.->|vhost 적용| VN
    PVE -.->|생성/제어| VM
    IB -.->|템플릿 빌드| PVE
```

| 레포지토리 | 역할 |
|---|---|
| [pickle-api](https://github.com/PNUops/pickle-api) | REST API와 프로비저닝 워커 (Spring Boot 4, Java 25, PostgreSQL 18, JobRunr) |
| [pickle-console](https://github.com/PNUops/pickle-console) | 사용자·관리자 웹 콘솔 (React 19, TypeScript) |
| [pickle-sshgw](https://github.com/PNUops/pickle-sshgw) | SSH 게이트웨이와 웹 터미널 브리지 (sshpiperd, Go) |
| [pickle-proxy-agent](https://github.com/PNUops/pickle-proxy-agent) | nginx 리버스 프록시 제어 에이전트 (Go) |
| [pickle-relay-agent](https://github.com/PNUops/pickle-relay-agent) | 오프캠퍼스 릴레이의 nftables DNAT 에이전트 (Go) |
| [pickle-llm-gateway](https://github.com/PNUops/pickle-llm-gateway) | 교내 LLM API 게이트웨이 (Go) |
| [pickle-image-builder](https://github.com/PNUops/pickle-image-builder) | 사용자 VM OS 이미지 빌드 레시피 (shell, virt-customize) |
| [pickle-infra](https://github.com/PNUops/pickle-infra) (비공개) | 인프라 프로비저닝 스크립트와 운영 런북 (shell) |
| [pickle-infra-example](https://github.com/PNUops/pickle-infra-example) | 프로비저닝·배포 스크립트와 런북 샘플 |
| [pickle-secrets](https://github.com/PNUops/pickle-secrets) (비공개) | 호스트 시크릿 볼트 (git-crypt) |
| [pickle-secrets-example](https://github.com/PNUops/pickle-secrets-example) | 볼트 레이아웃과 git-crypt 운용 절차 |
<!-- arch:end -->
