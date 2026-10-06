# 공개 SSH의 private 중계 준비

환경 예시: `pve-node-1`의 SSH gateway CT1102와 `pve-node-2`의 대상
SSH gateway CT1202. 이 문서는 guest 방화벽 파일의 **선택적 생성 입력**을
설명한다. 이 사본의 코드는 준비용 예시이며 guest 파일·커널 규칙·서비스·
공개 SSH 경로를 변경했다는 증거가 아니다. 실제 공개 전환에는 별도 승인과
현장 검증이 필요하다.

## 영속 unit 후보 생성

`scripts/render-ssh-transit.py`는 파일을 새 디렉터리에 생성하는 offline 도구다.
호스트 SSH, systemctl, nft, HAProxy 명령을 호출하지 않는다. `--apply`가 없으며
기존 출력 디렉터리는 덮어쓰지 않는다. Python 3로 실행한다.

```bash
python3 scripts/render-ssh-transit.py \
  --config /path/to/verified-ssh-transit.json \
  --output-dir /path/to/new-ssh-transit-candidate
```

`hosts/production/ssh-transit.example.json`은 예약 주소로 된 형식 예시다. 실제 입력은
현장에서 확인한 다음 여섯 값으로 구성하며, 예시를 운영 설정으로 사용하지 않는다.

| 필드 | 소유자와 확인할 값 |
| --- | --- |
| `source_listen` | CT1102의 기존 WireGuard 인터페이스 주소. socket은 이 주소의 TCP 2224와 `wg0`에만 바인딩한다. |
| `relay_peer` | CT1102에서 관측한 단일 WireGuard relay peer 주소. source guest input의 출발지다. |
| `target_node` | pve-node-2의 현재 campus 주소. raw proxy가 연결할 TCP 2224 목적지다. |
| `target_listener` | CT1202의 `pinfra` 주소. PROXY-required listener의 TCP 2224 목적지다. |
| `transit_source` | CT1102에서 pve-node-2로 나갈 때 실제 관측되는 단일 IPv4. pve-node-2 host 규칙과 CT1202 input 및 PROXY trust가 같은 값을 사용한다. |
| `target_binary` | CT1202에 이미 검증한 `sshgw-proxyfront` 절대 경로. 준비 디렉터리의 파일이면 같은 SHA의 영속 경로를 먼저 별도로 설치하고 입력을 다시 고정한다. |

출력은 source의 `pickle-ssh-transit.socket`과 `pickle-ssh-transit.service`, target의
`pickle-ssh-transit-front.service`다. source socket은 `wg-quick@wg0`와 nftables
뒤에 시작하고 `sockets.target`에서 활성화되는 구조다. 서비스는 socket activation의
`Accept=no`로 실행하는 systemd-socket-proxyd이며 HAProxy가 보낸 PROXY v2 바이트를
추가하거나 제거하지 않는다. target frontend는 `sshpiperd`, `networking`과 실제
`isolated-services-firewall` 뒤에 시작하며
explicit `--listen`, loopback `--upstream`, 단일 `/32 --peer`를 사용한다. 대상 env의
이전 기본 listen이나 peer 값을 상속하지 않는다. 기존 관리 SSH의 `:22`를 사용하지 않는다.

Source persistent nft 파일 후보도 필요하면 보호된 현재 파일과 독립적으로 읽은 SHA를
`--source-nft`와 `--source-nft-sha256`로 함께 입력한다. 정확한 `inet sshgw input`
peer 전용 `:22` 줄 뒤에 TCP 2224 새 연결 한 줄만 넣는다. 해시가 다르거나 anchor가
없거나 중복되면 생성 전에 실패한다. 그 줄을 제외한 원본 byte는 그대로 보존된다.
Output manifest는 각 파일의 SHA와 size, `activation_authorized: false`를 담는다.

공식 systemd 설명: [socket-proxyd](https://github.com/systemd/systemd/blob/main/man/systemd-socket-proxyd.xml),
[socket unit](https://github.com/systemd/systemd/blob/main/man/systemd.socket.xml).
이 도구의 로컬 테스트는 호스트의 native unit 검증이나 재부팅 증거가 아니다.

## 적용 소유권과 정지점

1. 승인된 전환 창 전에는 source와 target hostname, CTID, binary SHA 및 파일의
   owner와 mode, listener 부재, 기존 nft 파일과 커널 정책을 읽는다. Source receiver의
   두 unit, target frontend unit이 없다는 것과 같은 포트의 이전 시험 socket 및
   raw/front service가 inactive, MainPID 0이며 persistent enable symlink와 drop-in이
   없다는 것을 확인한다. source의 기존 `sshgw-proxyfront`와 시험 raw receiver의
   소유권을 혼동하지 않는다. `:2224` listener와 그 PID의 cgroup/unit도 직접 대응한다.
   다른 unit이나 불명 시도가 있으면 덮어쓰지 않는다.
2. 별도 candidate 경로에서 각 호스트의 `systemd-analyze verify`와 `nft --check`를
   실행한다. guest·PVE host 방화벽의 최종 판단, source 왕복 route와 source IP,
   trust가 일치해야 한다. Native 검사 실패는 적용 전 정지점이다.
3. Source와 target 파일의 original bytes, owner, mode와 unit enable/active 상태를
   새 보호 경로에 보존한다. 기존 backup은 재사용하거나 덮어쓰지 않는다. 상태와
   해시를 compare-and-swap하는 별도 실행 절차에서만 적용한다. 전체 생성기를
   운영 guest에 재실행하지 않는다.
4. 파일 준비는 서비스 enable이나 start를 포함하지 않는다. 활성화 전 root 절차가
   source의 persistent hold와 여섯 unit inactive/PID 0, DB app 연결 0을 수락하고,
   보호 전송한 원본 사용자 SSH host key의 공개 fingerprint가 CT1202의 실제 key와
   같음을 읽어야 한다. Target API/relay 소유권도 단일 주체여야 한다. 이 증거가 없으면
   socket과 frontend의 enable/start는 금지한다.
5. 승인된 활성화 단계에서 대상 host 규칙과 guest input, frontend 및 source private
   receiver를 적용한다. nft service의 restart로 전체 규칙을 flush하지 말고 후보 파일의 native
   check, 필요한 커널 규칙의 직접 apply와 readback을 구분한다. 신규 unit은 root:0644,
   binary는 검증한 SHA와 소유권, guest nft 파일은 원본 소유권을 유지한다. Source
   socket만 enable하며 source service는 socket activation으로 시작한다. Target
   frontend는 enable 상태와 실제 listen 주소를 확인한다.
6. 공개 HAProxy는 아직 기존 CT1102:22를 가리킨다. private 경유 probe에서 PROXY가
   필수인지, 제한 source 외 접속이 거부되는지, 원본 사용자 SSH host key의 공개
   fingerprint가 같은지 확인한다. 기존 host key와 사용자 trust 파일은 덮어쓰지 않는다.
   대상 key 활성화는 별도 보호 전송과 source key pin 검증을 마친 승인 절차로 수행한다.
7. Source writer hold와 target의 유일한 API/relay 소유권을 수락한 뒤에만 public
   HAProxy backend를 기존 tunnel의 CT1102:2224로 바꾼다. 검토한 전체 후보에 대한
   `haproxy -c`와 live file SHA를 확인하고 기존 파일과 process를 보호한다. reload 후
   외부 SSH host key, 인증, client IP audit를 관측한다. relay-agent bearer와 API
   endpoint 인수는 이 HAProxy backend 변경과 별개의 상태다.

## 재부팅과 조건부 원복

재부팅 뒤에는 source WG, nftables, socket enable/active와 listen, target sshpiperd와
frontend, PVE host의 소유 DNAT/FORWARD 및 priority guard를 각각 읽는다. Persisted
파일이 있다는 것만으로 통신 복구를 선언하지 않는다. 실제 재부팅을 포함한 추가
중단은 승인된 창에서만 수행한다. WG나 nft service를 재시작하는 작업도 기존 공개
트래픽에 영향을 줄 수 있으므로 준비용 확인으로 실행하지 않는다.

원복은 이번 시도의 파일 SHA와 unit 상태가 journal과 일치할 때만 수행한다. 먼저
public backend를 안전한 hold 경로로 되돌리고 source socket과 raw proxy를 함께
정지한다. Target frontend를 정지한 뒤 candidate의 파일을 제거하거나 original
bytes와 owner/mode로 되돌린다. original enable/active 상태와 host/guest 규칙까지
확인한다. NFT ruleset 전체 flush나 불명 conntrack 삭제는 하지 않는다.

Source writer 재개는 target의 첫 DB 쓰기뿐 아니라 JobRunr, 메일, DNS, vendor와
usage 등 모든 부작용이 0임을 증명하고 별도로 승인한 경우에만 가능하다. 불명이거나
첫 target 부작용 뒤에는 source API와 agent를 다시 켜지 않는다. 공개 SSH 중계의
원복 성공은 데이터 writer 원복 허가가 아니다.

## Guest 방화벽 생성 입력

CT1202를 새로 준비할 때 `isolated_services.py` 설정에는 다음 두 필드를
함께 지정한다. 생략하면 기존 출력과 정책이 유지된다.

| 필드 | 의미 |
| --- | --- |
| `ssh_transit_ingress_source` | edge에서 대상 node로 도착하는 단일 IPv4 출발지. CIDR이나 `0.0.0.0/0`을 받지 않는다. 대상 내부 subnet·서비스 주소·resolver 주소와 같아서는 안 된다. |
| `ssh_transit_port` | 대상 SSH gateway가 수신할 단일 비특권 TCP 포트. 기존 web-terminal·proxy·node 관리 포트와 겹치면 거부한다. |

생성되는 CT1202 `inet isolated_services` input 규칙은 `ip saddr`,
`ip daddr <CT1202의 sshgw_ip>`, `tcp dport`를 모두 고정한다. 기존
proxy·API 제어 포트와 default DROP은 유지된다. 대상 node의 DNAT/FORWARD,
CT1202 listener와 회신 경로는 이 guest 규칙과 별도로 확인한다.

원본 예시 CT1102를 새로 생성할 때는 `RAW_SSH_TRANSIT_PORT`와
`RAW_SSH_RELAY_IP`를 사용할 수 있다. 전자의 기본값 `0`은 **추가 규칙 없음**이다.
후자는 문서용 WireGuard peer 주소 `100.64.0.1`을 기본값으로 둔다. 포트가
켜지면 기존 peer 전용 `:22` 규칙 바로 뒤에 `iifname "wg0"`, peer 출발지,
`100.64.0.2` 대상, 단일 TCP 포트와 `ct state new`를 가진 규칙만 추가한다.
기존 `:22`, WireGuard peer, relay forwarding과 API sync 규칙은 유지한다.
`nft -c`가 실패하면 파일을 설치하지 않는다.

`create-sshgw-lxc.sh`는 container·package·service까지 다루는 **전체 생성기**다.
기존 운영 guest에 추가 포트 하나를 열기 위해 재실행하지 않는다. 승인된 변경
창에서는 현재 파일 해시와 보호된 원본을 확인하고 검토된 후보 파일을 별도
CAS 절차로 적용한 뒤 guest의 persistent 파일, kernel input rule,
listener와 실제 PROXY 경유 경로를 각각 읽어야 한다. 파일 렌더링만으로
서비스 기동이나 공개 경로 전환을 입증할 수 없다.
