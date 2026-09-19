# 후보 proxy/SSH gateway core 준비

이 절차는 이미 준비된 API와 DB를 다시 만들지 않고, 같은 후보 환경에 새 proxy와 SSH gateway
guest 두 대만 추가한다. `scripts/bootstrap-isolated-services.sh`는 기본적으로 계획 JSON만
출력한다. `--apply`를 명시해야 PVE를 변경하며, 기존 CTID·volume·주소·state directory 중
하나라도 발견하면 중단한다.

이 도구는 기존 운영 proxy/SSH gateway를 업그레이드하지 않는다. candidate 전용 token을 새로
만들어 사용하며 기존 환경의 agent URL이나 token을 복사하지 않는다. proxyfront, WireGuard,
relay, public DNAT, DNS, 인증서 발급, API 설정, 사용자 VM은 범위 밖이다.

## 결과 경계

- proxy guest에는 nginx와 proxy-agent만 설치한다. guest firewall은 API 주소에서 오는
  TCP 9443만 받는다. public 80/443 ingress는 열지 않는다.
- SSH gateway guest에는 loopback sshpiperd, route plugin, terminal bridge만 설치한다.
  guest firewall은 proxy 주소에서 TCP 8082, API 주소에서 TCP 8083만 받는다. TCP 22 ingress와
  WireGuard는 없다.
- 두 guest는 unprivileged LXC, MTU 1370, `onboot=0`이다. application service는 모두
  disabled/stopped 상태로 끝난다. nftables를 소유하는 bootstrap service만 networking 전에
  활성화된다.
- optional `--validate-services`는 listener와 unit을 잠깐 확인한 뒤 성공·실패와 관계없이
  application service를 다시 disable/stop한다. API와 실제 호출 경로를 확인하는 시험은 아래
  별도 단계 뒤에 한다.
- 실패한 guest와 volume은 자동 삭제하지 않는다. state manifest의 ownership과 마지막 완료
  단계를 확인한 뒤 별도 복구 결정을 내린다.

## 1. Artifact와 입력 준비

모든 입력과 state parent는 실행 node의 root 소유 0700 directory 아래에 둔다. 두 env 파일과
legacy token SHA-256 목록은 root 소유 0600 regular file이어야 하며 symlink는 거부된다. 도구는
secret 값을 stdout, manifest 또는 오류문에 쓰지 않는다.

proxy artifact:

- proxy-agent source의 `cmd/proxy-agent`에서 빌드한 Linux amd64 static binary
- 같은 source tree의 `scripts/proxy-agent.service`
- 같은 source tree의 `scripts/nginx/pickle-base.conf`

SSH gateway artifact:

- sshgw source의 `scripts/build.sh`가 만든 `sshgw-route-plugin`과
  `sshgw-terminal-bridge`
- 같은 source tree의 `scripts/systemd/sshpiperd.service`와
  `scripts/systemd/sshgw-terminal-bridge.service`
- sshpiper 공식 GitHub release의 Linux x86_64 archive

2026-09-19에 공식 latest release API로 확인한 stable은 `v1.6.1`이다. 대상 asset은
`sshpiperd_with_plugins_linux_x86_64.tar.gz`, SHA-256은
`95d423a70e843a7512a72fbb16c0fa5dc59217cf064cb3e277520fddffded1e1`이다. 도구는 이 버전과
hash만 받으며 다운로드하지 않는다. 확인 출처는
`https://github.com/tg123/sshpiper/releases/tag/v1.6.1`이다.

현재 SSH gateway source가 사용하는 sshpiper SDK v1.5.4와 daemon v1.6.1의 plugin handshake도
2026-09-19에 로컬에서 확인했다. v1.6.1 daemon이 route plugin을 시작했고 실제 SSH public-key
callback이 plugin의 API client까지 도달했으며, 닫힌 fixture API에서 route를 fail-closed로
거부했다. listener가 떴다는 사실만으로 호환성을 판단하지 않았다.

각 artifact의 SHA-256과 source commit/tree는 실행 receipt에 따로 기록한다. JSON에는 artifact의
absolute path와 SHA-256을 함께 쓴다. 도구는 모든 파일을 non-symlink regular file로 확인하고,
guest에 `pct push`한 뒤 content SHA-256을 다시 읽는다.

proxy env는 아래 두 key만 담는다.

```text
PICKLE_CANDIDATE_ID=<configuration과 같은 UUID>
PICKLE_PROXY_AGENT_TOKEN=<새 candidate 전용 값>
```

SSH gateway env는 아래 세 key만 담는다.

```text
PICKLE_CANDIDATE_ID=<configuration과 같은 UUID>
PICKLE_SSHGW_TOKEN=<새 candidate 전용 값>
PICKLE_TERMINAL_CONTROL_TOKEN=<새 candidate 전용 값>
```

세 token은 서로 달라야 한다. 값은 32~128자의 base64-safe 한 줄이어야 한다. 기존 환경의
token 값은 열거나 복사하지 않는다. 기존 세 control token을 원래 보관 위치에서 SHA-256으로만
계산해 `forbidden_token_hashes_file`에 한 줄씩 둔다. 도구는 새 세 token hash가 이 목록에 없는지
확인하고, manifest에는 목록 자체의 hash와 항목 수만 기록한다. 목록 생성과 candidate token
생성은 값이 stdout이나 shell history에 남지 않는 보호 절차에서 수행한다.

## 2. JSON configuration

`examples/isolated-services.json`을 새 보호 directory로 복사한 뒤 모든 placeholder를 실제
사전검사 값으로 바꾼다. 레포지토리의 example 파일에는 실제 hostname, IP, artifact path나
secret을 쓰지 않는다.

필수 값은 다음과 같다.

- exact PVE node hostname과 cluster name
- proxy/SSH gateway CTID, hostname, IP, CPU, RAM, disk
- 기존 API CTID와 IP, terminal bridge가 받을 exact HTTPS console Origin. 도구는 API guest를
  변경하지 않는다.
- infrastructure bridge/subnet/gateway, MTU 1370, nameserver
- storage와 두 disk 외에 남겨 둘 reserve GiB
- 이미 cache된 Debian 13 amd64 template volume과 archive SHA-256
- 검토한 nginx.org stable package version
- candidate UUID, 두 protected env path, legacy token hash 목록 path, 새 state directory
- artifact 여덟 개의 read-only absolute path와 SHA-256

`state_dir` 자체는 없어야 하고 parent는 root 소유 mode 0700이어야 한다. 계획 확인은 secret,
PVE, artifact body를 읽지 않는다.

```bash
bash scripts/bootstrap-isolated-services.sh --config /root/<protected>/isolated-services.json
```

계획에서 두 CT만 보이고 `onboot=false`, `services.enabled=false`, `api_changed=false`인지 확인한다.

## 3. Apply의 보호 순서

`--apply`는 다음 순서를 고정한다.

1. root와 exact node/cluster/quorum, bridge gateway/MTU를 확인한다.
2. cluster 전체 CTID, pmxcfs config, storage volume, headroom과 ARP duplicate를 확인한다.
3. cached template와 모든 artifact SHA-256, protected env owner/mode와 candidate UUID/token 분리를
   확인한다.
4. 새 unprivileged guest를 `onboot=0,start=0`으로 만들고 ownership description을 기록한 뒤
   시작한다.
5. ifupdown/ifupdown2 hook parent를 검사하고 MTU hook을 설치한 뒤 live MTU를 readback한다.
6. APT source URI에서 package endpoint를 뽑고 guest 안에서 각 hostname의 IPv4 DNS resolution과
   TCP 연결을 10초씩 확인한다. 하나라도 확인되지 않으면 index나 package를 변경하기 전에
   중단한다.
7. package autostart를 막은 상태에서 Debian update/upgrade와 fresh package install을 수행한다.
   update는 APT 3의 `--error-on=any`로 transient warning도 실패로 올린다. 각 package command는
   run UUID, role, phase가 들어간 guest systemd transient service에서 실행한다. service는
   `RuntimeMaxSec`, `TimeoutStopSec`, `KillMode=control-group`, `SendSIGKILL=yes`를 가져 host의
   `pct exec` wait가 먼저 끊겨도 apt와 method child가 deadline 뒤 남지 않는다. `UMask=0022`를
   명시해 private bootstrap state의 0077 umask가 package helper로 번지지 않게 한다. unit name이
   이미 있으면 그 unit이나 다른 apt/dpkg process를 stop하지 않고 거부한다.
8. proxy nginx는 별도로 공식 nginx.org endpoint, signing fingerprint와 exact candidate
   version을 검사한다.
9. guest firewall syntax를 검사하고 firewall service를 먼저 활성화한다. networking과 각
   application unit은 firewall을 `Requires`/`After`로 참조한다.
10. artifact와 env를 no-overwrite 방식으로 설치하고 unit/nginx syntax를 검사한다.
11. SSH upstream, terminal, sshpiperd host key를 guest에서 one-time/no-clobber 방식으로 만든다.
   private half는 guest 밖으로 내보내지 않는다.
12. application service를 disabled/stopped로 확인하고 manifest를 완료한다.

최초 provisioning의 APT 통신은 guest에 nftables package가 생기기 전이라 PVE/host의 기존
infrastructure guard 아래에서 진행된다. 위 firewall → networking → service 순서는 package 설치
이후의 cold boot와 application service 시작 순서를 뜻한다. 최초 APT 트래픽까지 guest nft가
보호했다고 해석하지 않는다.

Transient package unit의 stdout/stderr는 journal에 남는다. 이름은
`pickle-isolated-services-<run UUID>-<role>-<phase>.service`이고 완료 뒤 collect된다. 실패 조사는
같은 이름으로 journal을 읽으며, bootstrap은 기존 apt/dpkg process나 이름이 겹친 unit을 임의로
종료하지 않는다. update 240초, upgrade 840초, install 540초 안에 끝나지 않으면 guest systemd가
먼저 unit 전체를 종료하고, host wait timeout은 stop 시간과 40초 여유 뒤에 온다.

실제 apply 명령과 configuration은 대상 host preflight 결과를 검토한 뒤 실행 기록에서 작성한다.
이 런북은 특정 운영 node의 command line을 미리 고정하지 않는다.

## 4. Public key custody

완료 state directory의 `public-keys/`에는 다음 public half만 export된다.

- `upstream_ed25519_key.pub` → API `PICKLE_SSH_PLATFORM_PUBLIC_KEY`
- `terminal_ed25519_key.pub` → API `PICKLE_TERMINAL_PUBLIC_KEY`
- `ssh_host_ed25519_key.pub` → sshpiperd server identity 확인용

directory는 0700이고 public file은 0644다. manifest에는 path와 SHA-256만 기록된다. private key와
token은 manifest나 Git에 복사하지 않는다.

## 5. 별도 API 단계

이 bootstrap은 API guest를 변경하지 않는다. SSH gateway는 API의 `/internal/**`를 직접
호출하고 API는 TCP peer 주소를 검사하므로 다음 변경이 별도로 필요하다.

- API listener를 guest 주소에서도 받을 수 있게 bind하고, 기존 loopback nginx path를 유지한다.
- API guest firewall에 SSH gateway IP에서 API TCP 8080으로 가는 rule 하나를 추가한다.
- API에 candidate `PICKLE_SSHGW_SOURCE_IP`, proxy-agent URL/token, terminal bridge URL/control
  token, 위 두 platform public key를 넣는다.
- console Origin과 terminal bridge의 exact Origin을 맞춘다.

이 단계 전에는 SSH gateway application service를 상시 enable하지 않는다. API 변경 뒤에는
API guest에서 proxy-agent `/status`, SSH gateway guest에서 API internal route의 source/token
검사, API guest에서 terminal control의 source/token 검사를 순서대로 확인한다. 잘못된 token과
잘못된 source가 각각 거부되는 것도 함께 본다.

## 6. 사용자 VM 전 확인

VM clone 전에 두 public key가 API 환경에 모두 들어갔는지 확인한다. VM firewall system source는
SSH gateway와 terminal 모두 SSH gateway guest IP, proxy는 proxy guest IP를 사용한다. firewall
기능을 켤 때는 barrier group과 relay source를 포함한 모든 필수 입력이 있어야 한다. relay
external path는 이 작업에서 구성하거나 완료로 보지 않는다.

clone 뒤에도 QGA가 guest SSH host key를 수집하고 API가 pin할 때까지 gateway route가 열리지
않는 것이 정상이다. 이 작업만으로 외부 SSH, web terminal, public HTTP packet proof를 주장하지
않는다.
