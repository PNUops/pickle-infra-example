# NetBird 관리 peer와 VPN NTP 서버

관리 peer는 기존 SSH host key와 포트를 사용합니다. NetBird SSH, DNS, routing peer와
client route는 비활성으로 등록합니다. 기존 WireGuard·공개 진입·기본 경로·호스트 IP는
유지합니다. 관리 등록은 사용자 VM·추론 경로·운영 클러스터 가입을 수행하지 않습니다.

## 버전과 이번 대상

- NetBird 신규 peer의 선택 버전은 **0.80.0**입니다. 2026-10-11에 공식 latest가
  [v0.80.0, 10월1일](https://github.com/netbirdio/netbird/releases/tag/v0.80.0)임을 확인했습니다.
  기존 peer의 client를 이번 절차로 일괄 업그레이드하지 않습니다.
- chrony upstream latest는 [4.9, 2026-08-27](https://chrony-project.org/news.html)입니다.
  AWS Debian13 server는 서명된 stable APT의
  [4.6.1-3+deb13u2](https://packages.debian.org/trixie/chrony)를 선택합니다.
  같은4.x의 필요한 server-only 기능을 지원하고 현재 distro ABI·보안 backport를
  사용하기 위한 선택입니다. Debian13은 [2028-08-09까지 full 지원,
  2030-06-30까지 LTS](https://www.debian.org/releases/trixie/)입니다.
  Package 전체 설치 대신 추출하므로 보안 업데이트 시 이 별도 binary도 갱신해야 합니다.
- 신규 management peer는 **node-a, relay-node** 두 개입니다. dgx1 추가는 후속입니다.
- NTP client는 **node-a, node-b, node-c, witness-host, backup-guest**의 실제 NetBird IPv4 다섯 개입니다.
  AWS와 node-a의 새 IP/peer ID는 등록 후 실제 조회값을 사용합니다. 미리 만들어 넣지 않습니다.

## 변경 전 기록과 적용 경계

각 host의 새 root0700 journal에 원래 package 상태, netbird unit/config 존재 여부,
SSH·resolver·network/WireGuard의 bytes/권한과 route/rule·listener·nft 상태를 보관합니다.
키와 기존 NetBird config는 host 보호 공간에만 둡니다. NetBird 0.80.0의 실제 default
profile은 `/var/lib/netbird/default.json`이며 private key를 포함하므로 Mac/Git/출력으로
반출하지 않습니다. `/etc/netbird`가 있으면 그 경로도 함께 확인합니다. 기존 profile이나
Cloud peer가 있으면 disconnected 상태만으로 새 peer라고 판단하지 않으며 재등록 전에
기존 상태와 자기 partial을 확인합니다. 다른 backup을 덮어쓰지 않습니다.
이 default profile 확인과 보호 백업은 실행자의 사전 절차입니다. 현재 enrollment
script가 해당 경로까지 자동 검사/백업한다고 가정하지 않습니다.
Cloud peers/groups/policies도 token 없이 보호 기록으로 보관합니다.

Default policy가 disabled이며 동명 peer/group/policy와 기존 `/etc/netbird` 또는
`/var/lib/netbird/default.json` profile이 없는지
확인합니다. 기존 연결 peer나 불명확한 partial은 재등록하지 않습니다. Binary/package
설치와 Cloud 변경 전에 구체 diff·원복을 검토합니다. Client 시각 변경은 별도 검토입니다.

## 1. 보호된 client 설치와 관리 등록

기존 `bootstrap-backup-host.sh`의 NetBird 서명 repo 절차만 재사용합니다. 그 전체 script는
backup/libvirt/qnetd까지 변경하므로 관리 host에서 실행하지 않습니다.

1. 공식 key를 HTTPS로 내려받아 fingerprint
   `EFE37DF047DF7CCDF1FC54FA83F79AD029778355`를 확인합니다.
2. 기존 keyring/repo/drop-in이 있으면 원 bytes를 읽어 대조하고 덮어쓰지 않습니다.
   새 `/etc/systemd/system/netbird.service.d/10-host-only.conf`는 package 설치 **전**에
   `[Service]`와 `Environment=NB_DISABLE_SSH_CONFIG=true`로 root0644 작성합니다.
3. signed repo `https://pkgs.netbird.io/debian stable main`의 실제 candidate를 확인하고
   `apt-get -s install --no-remove netbird=0.80.0`의 변경 목록을 검토합니다.
   완전히 받은 local deb의 signed metadata SHA와 `dpkg-deb -f`의 Package=netbird,
   Version=0.80.0, Architecture=amd64를 확인합니다. Partial은 설치하지 않습니다.
   승인 뒤 `NB_DISABLE_SSH_CONFIG=true NEEDRESTART_MODE=l apt-get install --no-remove
   /root/<새 보호경로>/netbird_0.80.0_amd64.deb`로 local 파일을 설치합니다.
   실제 설치에서 WAN download를 반복하지 않습니다. Signed source/keyring은 후속 유지보수용입니다.
   기존 daemon의 자동 재시작은 하지 않습니다. NetBird unit의 보호 env·버전을 재확인합니다.
4. Cloud에서 각 host별 short-lived **one-off/usage_limit1** setup key를 별도로 만듭니다.
   키는 stdout/argv/Git에 넣지 않고 해당 host `/run`의 root0600 single-link 파일로
   보호 전달합니다. PAT는 host에 전달하지 않습니다.

```bash
# 새 host root. 처음에는 --apply 없이 확인합니다.
bash scripts/enroll-management-peer.sh --expected-host '<actual hostname>' \
  --peer-name node-a --setup-key-file /run/netbird-management.key \
  --journal /root/netbird-management-<new-name>
```

검토 뒤 같은 입력에 `--apply`를 추가합니다. AWS는 peer-name `relay-node`를 사용합니다.
UDP51821을 선택하여 기존 wg0 UDP51820을 유지합니다. Public security group에 새 port를
개방하지 않으며 direct/relay 실제 연결 형태를 기록합니다. 기존 SSH는 기존 pin과
native port(관리 node-a 22, AWS22)로 확인합니다. AWS의 기존 관리 WireGuard 경로는
source CT102의 `nc` transit과 literal HostKeyAlias `[100.64.0.1]:22`를 유지합니다.
SSH config/known_hosts 변경이 있으면 진행하지 않습니다. 성공 후 정확한 setup key만 revoke합니다.

## 2. Cloud의 정확한 추가 범위

Cloud API는 기존 운영자의 보호된 token-file client를 사용합니다. 요청 직전 GET으로
실제 before와 이름·peer ID·정렬된 membership·Default disabled를 대조합니다.
확인과 write 사이의 atomic CAS가 지원된다고 가정하지 않습니다. 단독 실행자가 **새로
만든 ID만** 변경하고 readback합니다. 외부 변경/동명 충돌/응답 불명확은 중단·조회이며
동일 POST를 반복하지 않습니다. 기존 policy/group을 retarget하지 않습니다.

| Method/path | 실제 body/예상 변화 |
|---|---|
| POST `/api/groups` | `management-node-a`, `management-aws`를 각각 `peers:[]`로 생성 |
| POST `/api/setup-keys` | 각 새 group ID를 `auto_groups`로 지정한 host별 one-off key. `usage_limit:1`, 짧은 `expires_in`, `ephemeral:false` |
| GET `/api/peers`, GET 새 group | 실제 node-a/relay-node ID·IPv4·connected와 membership1 확인; All의5→7 자동 membership만 예상 |
| POST `/api/groups` | `platform-ntp-clients`: 실제 node-a/node-b/node-c/witness-host/backup-guest peer ID 다섯 개만 |
| POST `/api/policies` | 아래3개 rule를 각각 **one-rule enabled policy3개**로 생성. source/destination은 실제 group ID |

```json
[
 {"name":"management-node-a-ssh","enabled":true,"rules":[
  {"name":"operator-node-a","enabled":true,"action":"accept","bidirectional":false,
   "protocol":"tcp","ports":["22"],"sources":["<existing operators ID>"],"destinations":["<new management-node-a ID>"]}]},
 {"name":"management-aws-ssh","enabled":true,"rules":[
  {"name":"operator-relay","enabled":true,"action":"accept","bidirectional":false,
   "protocol":"tcp","ports":["22"],"sources":["<existing operators ID>"],"destinations":["<new management-aws ID>"]}]},
 {"name":"platform-host-ntp","enabled":true,"rules":[
  {"name":"host-ntp","enabled":true,"action":"accept","bidirectional":false,
   "protocol":"udp","ports":["123"],"sources":["<new platform-ntp-clients ID>"],"destinations":["<new management-aws ID>"]}]}
]
```

위 배열의 세 객체를 각각 별도 POST합니다. 실제 Cloud 적용에서 한 policy에 세 rule를
보낸 요청이200이었으나 GET에는 첫 rule만 관측됐으므로 one-rule 방식으로 각 scope를
독립 readback합니다. 모든 NetBird 버전의 보편적 제한이라고 단정하지 않습니다.
GET의 source/destination group 객체는 실제 ID로 대조합니다. All group의 자동 peer
count 증가를 policy 설정 변경이라고 오인하지 않으며 membership은 별도로 확인합니다.
위 angle-bracket 값은 실제 응답으로 대체하는 API 입력 자리입니다. 실행 receipt가 아닙니다.
All/default 정책·기존11개 정책·학생 subnet·routing resource·DNS 설정은 변경하지 않습니다.

Setup key의 실제 POST body는 `name`, `type:"one-off"`, `expires_in:86400`,
`auto_groups:["<해당 host의 새 group ID>"]`, `usage_limit:1`, `ephemeral:false`입니다.
[공식 API](https://docs.netbird.io/api/resources/setup-keys)의 최소 만료값86400초를 사용하며
실제 등록 뒤 즉시 revoke하므로 그 기간 동안 유효하게 방치하지 않습니다.
값은 보호된 client 메모리/host key-file에만 보관하고 Cloud 실제 ID/usage 상태를 대조한 뒤
그 ID만 revoke합니다. 실패·unknown에서는 setup key를 재발급하지 않고 실제 peer부터 읽습니다.

## 3. 기존 AWS 시간 client를 유지하는 NTP server

`apt install chrony`는 `time-daemon` conflict로 timesyncd를 제거할 수 있으므로 실행하지
않습니다. 승인된 signed APT metadata의 정확한 chrony version/architecture/SHA를 기록하고
`apt-get download chrony=4.6.1-3+deb13u2`만 수행합니다. 실제 Debian13/amd64와 필요한
shared library를 확인합니다. 다른 Ubuntu host에 이 binary를 복사하지 않습니다.

```bash
# AWS root, protected package file와 실제 다섯 client IP를 사용합니다.
bash scripts/install-vpn-ntp-server.sh --expected-host '<actual hostname>' \
  --package /root/ntp-server-<new-name>/chrony.deb --package-sha256 '<actual signed metadata SHA>' \
  --bind '<actual AWS NetBird IPv4>' --upstream 198.18.0.123 \
  --client '<actual node-a IPv4>' --client 100.64.0.2 --client 100.64.0.3 \
  --client 100.64.0.4 --client 100.64.0.5
```

검토 뒤 `--apply`를 추가합니다. 새 versioned `/opt/pickle-chrony-server`에 `dpkg-deb -x`
추출하고 `ldd`·`chronyd -p`·systemd unit 검사를 합니다. Parse/ABI/start 실패는 partial을
보존하며 재설치하지 않습니다. **chronyd -4 -x**, exact NetBird bindaddress와 client5개의
`allow /32`, cmdport0, Unix-only 관리 socket을 사용합니다. CAP_SYS_TIME 없이
ProtectClock을 적용하며 AWS timesyncd와 Amazon time source는 유지합니다.
`binddevice`와 추가 network capability는 사용하지 않습니다.

`ss`의 IPv4 UDP123 주소, process argv/capabilities, 기존 timesyncd PID/sync, source ref와
Cloud 성공/거부를 확인합니다. 새 unit은 server-only이며 client 시각을 바꾸지 않습니다.

## 4. Client offset과 복구

Client 변경 **전** 실제 시각 offset/round-trip·server leap/stratum을 읽습니다.
Chrony가 이미 있는 host에서는 native `chronyd -Q -t 15 -f /dev/null 'server <AWS-IP> iburst'`
형태를 해당 설치 버전의 help와 비교한 뒤 사용합니다. `-Q`는 시각을 조정하지 않는
측정입니다. 다른 time client의 host는 기존 지원되는 read-only SNTP 방법을 선택합니다.
큰 offset/불명확한 결과는 Root 검토 전 client 설정을 변경하지 않습니다.

Client의 기존 source·설정을 보존하고 exact AWS-IP만 추가하는 변경은 별도로 검토합니다.
Chrony는 기존 config의 `sourcedir`가 읽는 새 `.sources` 파일을 root0644로 설치하고
`chronyc reload sources`로 선택합니다. 기존 daemon PID와 pool을 보존합니다.
Timesyncd는 새 `.conf.d` directory0755와 drop-in0644에 `[Time]`/`NTP=<AWS-IP>`를
설치한 뒤 해당 time unit만 재시작합니다. 초기 작은 clock correction이 발생할 수 있으므로
적용 전 낮은 RTT offset과 영향, 적용 후 실제 source/kernel sync를 확인합니다.
파일 생성 시 mode 인자는 process umask에 의해 제한됩니다. `umask077` 아래에서
0644/0755를 요청해도 실제0600/0700이 될 수 있으므로 새 public config의 정확한
uid/mode/contents를 읽어 확인합니다. Private profile/key/journal은0600/0700을 유지합니다.
기존 chrony restart·makestep/clockstep을 수행하지 않습니다. 새 source 선택·reach·ref time,
NTPSynchronized와 기존 서비스/인증/backup을 확인하고 core monitor의 **자연 실행**을
관측합니다. Monitor gate를 우회하거나 과거 FAILED state를 지우지 않습니다.

원복은 자신의 server unit만 stop/disable하고 원 metadata와 동일한 새 config/unit 경로만
회수합니다. Versioned package와 실패 journal은 보존합니다. 기존 timesyncd/chrony를
교체·재시작하지 않습니다. 새 peer/policy/group을 제거해야 하면 자신이 만든 ID와
membership만 대조하여 Cloud에서 회수합니다. Native SSH·WireGuard·공개 relay는 유지합니다.
시각이 이미 바뀐 client를 자동 clockstep으로 되돌리지 않습니다.

원 명령·exit status·시각·hash·실패/partial과 실제 연결/offset 결과를 기록합니다.
Peer Connected, 서버 active와 실제 client 동기화는 각각 다른 완료 조건입니다.
