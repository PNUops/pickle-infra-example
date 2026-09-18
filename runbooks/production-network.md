# 두 운영 노드의 SDN과 수동 gateway

이 절차는 pve-node-2와 pve-node-3에 운영 guest L2와 gateway를 준비한다. 아직 실제 호스트에
적용하지 않은 도구다. pve-node 서비스, Corosync의 campus 주소와 클러스터 신원은 변경하지 않는다.
PVE가 생성하는 `/etc/network/interfaces.d/sdn`은 API가 관리하며 수동 편집하지 않는다.

## 구성과 소유권

정본 입력은 `hosts/production/network.json`이다. 예시 운영망은 RFC 6598의
`100.65.0.0/16`과 `100.66.0.0/16`을 사용해 기존 개발망 예시 `198.18.0.0/16`·
`198.19.0.0/16`, BMC와 mesh 예시 주소를 구분한다. 실제 배포 전에는 이 예약 주소를
그대로 사용하지 않고 모든 host와 tunnel route에 충돌하지 않는 주소 계획으로 교체한다.

| 항목 | 값 |
|---|---|
| Cluster / zone | `example-prod` / `prodvx` |
| Infra | `pinfra`, VNI 927000, `100.65.0.0/16`, gateway `100.65.0.1` |
| Guest | `pguest`, VNI 928000, `100.66.0.0/16`, gateway `100.66.0.1` |
| VTEP | pve-node-2 `100.64.0.30`, pve-node-3 `100.64.0.31` |
| MTU | NetBird 1420, VNet와 guest NIC 1370 |
| 초기 gateway | pve-node-2만 두 gateway IPv4와 campus SNAT, IPv4 forwarding 소유 |
| Standby | pve-node-3는 gateway IPv4와 SNAT 규칙 없음, IPv4/IPv6 forwarding 0 |

IPv6는 host의 `pinfra`와 `pguest`에서만 주소와 autoconf를 비활성화한다. `wt0`, `vmbr0`와
호스트 전체 IPv6를 끄지 않는다. Guest IPv6 L2 자체와 host IPv6 관리면 보호는 구분한다.
IP 할당은 플랫폼 DB와 cloud-init이 소유한다. 이 zone에는 DHCP와 SDN subnet/gateway를
만들지 않으며, 모든 노드에 같은 gateway alias가 자동 생성되는 구성을 사용하지 않는다.

| 도구 | 실행 위치와 역할 |
|---|---|
| `scripts/apply-production-sdn.sh` | pve-node-2 root. PVE global SDN lock 아래 정확한 zone/VNet만 생성·삭제 |
| `scripts/apply-production-network.sh` | 각 PVE root. node 준비, guard/gateway 적용과 rollback |
| `scripts/verify-production-network.py` | 운영자 머신. native/mesh SSH와 CA 검증 HTTPS, BMC 확인 후 commit proof 생성 |

실행은 기본적으로 사전 검사다. 변경은 `--apply`가 있어야 한다. `pve-node`이나 다른 cluster,
불명확한 기존 리소스, 예상과 다른 NetBird flags/mark 형식을 만나면 중단한다.

## 적용 순서

1. 두 PVE가 online이고 guest가 비어 있으며 HA resource가 없어야 한다. 초기 변경은
   dept-node qdevice가 투표하는 expected/total votes 3 상태에서만 시작한다. 설정 백업과
   SSH fallback, CA와 BMC 복구 경로를 먼저 확인한다.
2. 운영자 NetBird 세션과 두 host peer의 실제 연결을 확인한다. Cloud policy는 운영
   두 peer 사이 UDP 4789만 추가하고 기존 관리 정책을 보존한다. 이 도구는 Cloud
   계정이나 정책을 변경하지 않는다. Direct/relay 상태와 MTU 수용 여부는 실제 트래픽으로 확인한다.
3. 각 노드에 root 소유 0700의 이 실행 전용 backup 디렉터리를 준비한다. 노드별로
   아래 사전 검사를 하고 적용한다. MTU 변경은 native SSH 또는 현장 console에서 실행한다.

   ```bash
   bash scripts/apply-production-network.sh prepare --backup-dir /pickle/backup/production-network
   bash scripts/apply-production-network.sh prepare --backup-dir /pickle/backup/production-network --apply
   ```

   복구 timer 기본값은 300초이며 `--rollback-seconds`로 60–900초 안에서 정한다.
   스크립트는 먼저 `/bin/bash` 경유 1초 시험 timer가 실제로 nonce proof를 쓰고
   정상 종료하는지 확인한다. 그 뒤 실제 rollback timer를 arm하고 BMC guard를 설치한
   다음 NetBird MTU를 변경한다. 재시작 직후 mark rule이 아직 비어 있으면 최대 15초 동안
   0.25초 간격으로 기다리되, IPv4/IPv6에서 원래 확인한 tuple이 연속 두 번 같아야 준비로
   판정한다. 다른 mark/mask나 잘못된 rule은 즉시 중단하며 attempts와 elapsed를 state에
   남긴다. `/run`의 스크립트를 직접 실행하지 않는다.
4. 두 node 준비가 성공하면 pve-node-2에서 SDN을 적용한다.

   ```bash
   bash scripts/apply-production-sdn.sh
   bash scripts/apply-production-sdn.sh --apply
   ```

   다른 pending 변경이나 global lock을 강제로 가져오지 않는다. Zone/VNet의 기존 값이
   다르면 덮어쓰지 않는다. Parent UPID뿐 아니라 두 node의 새 `networking` reload UPID가
   모두 OK여야 성공이다. 기존 running reload나 겹치는 reload가 있으면 소유권을 추정하지 않는다.
5. 두 node에서 `activate --apply`를 실행한다. Kernel의 VNI와 master, FDB peer, 실제
   route의 `wt0`/source, 두 MTU를 확인한 뒤에만 L3를 활성화한다.

   ```bash
   bash scripts/apply-production-network.sh activate --apply
   ```

   pve-node-2 BMC `nic1`의 host 신규 입력과 양방향 forwarding을 먼저 차단한다. pguest에서
   두 host의 campus/mesh 주소로 가는 트래픽도 포트 일부만이 아니라 전체를 차단한다.
   host gateway echo 진단은 별도 허용한다. IPv4 forwarding은 마지막에 켠다.
6. 운영자 머신에서 보호 evidence 디렉터리(0700)와 기존 공개 cluster CA로 양 경로를 확인한다.

   ```bash
   python3 scripts/verify-production-network.py --ca-file /path/to/pve-root-ca.pem \
     --output-dir /path/to/private-evidence
   ```

   Native SSH는 기존 `pve-node-2`/`pve-node-3` 별칭을 사용한다. Mesh SSH는 ProxyJump/ProxyCommand를
   해제하고 native host key alias를 고정한다. 두 SSH 경로는 기존 multiplex socket을
   재사용하지 않으며 모든 curl 검사는 proxy 환경변수를 무시한다. Native HTTPS는 반대 PVE에서 campus 경로로,
   mesh HTTPS는 운영자 머신에서 직접 검증한다. BMC HTTPS는 pve-node-2에서 확인한다.
7. 각 `*-commit-proof.json`을 해당 host root 전용 경로에 전달한 뒤 180초 이내에 commit한다.

   ```bash
   bash scripts/apply-production-network.sh commit --proof /root/node-commit-proof.json --apply
   ```

   Commit은 guard fingerprint와 현재 주소·route·MTU·hook, forwarding 및 관리 경로를
   읽기 전용으로 확인한 뒤 timer만 취소한다. IPv4/IPv6 bridge netfilter가 모두 켜져 있는지,
   guest가 있으면 PVE firewall enable과 두 family의 guest chain도 검사한다.
   Proof 뒤에 네트워크를 다시 적용하지 않는다.
   Guest 정책과 성능 검증은 이 관리 경로 commit과 별도로 남는다.

## 방화벽 경계

VM별 IN/OUT 정책은 기존 `pve-firewall` backend와 플랫폼 API가 소유한다. 이 도구는
per-VM allowlist를 만들거나 PVE/NetBird firewall을 비활성화하지 않는다. 새
`proxmox-firewall` backend로 전환하지도 않는다.

- Legacy chain은 `PKL-PROD-*`만 소유한다. Hook과 해당 chain만 원자적으로 갱신하며
  다른 chain이나 전체 table을 flush하지 않는다. Guest 허용 경로는 `RETURN`으로
  나가므로 per-VM PVE 판정을 건너뛰는 전역 ACCEPT를 추가하지 않는다.
- 별도 `inet pickle_production_guard`의 INPUT/FORWARD hook은 priority -20이다.
  NetBird가 legacy rule을 앞에 재삽입해도 BMC와 host 관리면의 거부가 먼저 적용된다.
- `bridge pickle_production_l2`는 확인한 `vxlan_pinfra`/`vxlan_pguest` ingress에서만
  mark를 처리한다. 설치된 NetBird의 setter/accept value와 full mask, PVE reserved mask를
  읽어 일치·비중첩을 확인한다. PVE bits는 보존하며 다른 non-PVE mark는 제거하지 않고
  drop한다. NetBird가 알 수 없는 mark 형식으로 바뀌면 guest 경로는 fail-closed이며
  재검증 전에는 정상으로 판정하지 않는다.
- 두 VNet은 IPv4/IPv6/ARP 이외의 EtherType을 거부한다. VLAN/QinQ trunk는 지원하지 않는다.
  VLAN-aware=false만으로 tagged frame의 firewall 우회가 차단된다고 가정하지 않는다.

NetBird 재시작 후에는 node `reconcile --apply`와 운영자 관리 경로 검증을 다시 한다.
독립 nft guard는 legacy hook 순서 변경 중에도 유지된다. `status`는 mark/L2/priority guard
counter를 함께 출력한다. 같은 node와 다른 node의 실제 guest로 새 IN 거부, 명시적 허용,
OUT과 반환, IP/MAC/ARP 위조, IPv6 link-local, tagged frame을 검사하고 counter의 전후를
기록한다. 순서를 바꾼 규칙이나 가짜 mark를 주입하는 시험은 소유한 격리 guest 경로에만 한다.

## 부팅과 재적용

Node 입력은 `/etc/pickle/production-network.json`, 변경 상태는 root 전용
`/var/lib/example-production-network/state.json`에 남는다. Runtime은
`/usr/local/libexec/example-production-network/`에 설치한다. `example-production-network.service`는
network-online, NetBird, pve-cluster와 pve-firewall 뒤에 실행하고 guest 자동 시작은 이 service 뒤에 둔다.
VNet의 if-up hook은 같은 service를 비동기로 재시작한다. 생성된 SDN interface 파일에
host IP를 덧붙이는 방식을 쓰지 않는다.

부팅 시 quorum이나 NetBird 연결이 아직 준비되지 않았으면 network service는 실패하고
10초 뒤 재시도한다. Dependency 실패로 이미 중단된 guest 시작은 자동 복구됐다고
가정하지 않는다. 운영자가 network 상태와 PVE firewall을 확인한 뒤 `pve-guests.service`를
다시 시작한다. PVE firewall을 중지하거나 bridge netfilter를 끈 상태에서는 guest 정책을
검증했다고 볼 수 없으며 신규 guest 시작과 운영 검증을 중단한다.

Gateway owner 기록은 `/etc/pve/priv/example-production-network-owner.json`이다.
일반 부팅은 이 기록과 현재 quorum을 확인하고 해당 owner만 주소와 SNAT를 갖는다.
Standby에서는 IPv6 link-local도 host 관리 경로로 노출되지 않는다. 이 기록은 자동
장애 감지나 fencing, conntrack 복제를 구현하지 않는다.

## PVE 방화벽 API getter 진단

PVE 9.2.11의 `pvesh get /cluster/firewall/options`가 `unknown schema type`으로
실패하더라도 이를 전체 PVE API 장애나 방화벽 정책 적용 성공으로 해석하지 않는다.
이 조합에서는 `pve-manager 9.2.11`, `pve-firewall 6.0.5`,
`libpve-common-perl 9.2.1`의 CLI schema compiler가 `get_options`의
parameters schema에 `properties`가 없는 경우를 처리하지 못한 것으로 확인됐다.
Node options GET은 정상이다.

root가 직접 읽기 전용으로 다음 getter를 실행해 실제 옵션 읽기 경로를 확인할 수 있다.

```bash
perl -MPVE::API2::Firewall::Cluster -MPVE::RPCEnvironment -MJSON \
  -e 'PVE::RPCEnvironment->setup_default_cli_env(); print encode_json(PVE::API2::Firewall::Cluster->get_options({}));'
```

이 Perl 명령은 CLI 경로만 분리해 진단한다. HTTPS API 호출, VM firewall policy
write/live 차단 또는 정책 집행을 증명하지 않는다. 일반 API 오류를 무시하는 범용
fallback을 추가하지 않는다. `pve-firewall status`가 disabled/running이면 정책
집행 완료로 표시하지 않는다. vendor 파일이나 설정은 변경하지 않는다.

## Rollback과 수동 gateway 전환

확정 전에는 timer 또는 같은 operation ID의 `rollback --apply`가 node 변경을 되돌린다.
먼저 routing과 두 gateway 주소를 내리고, guard가 남아 있는 동안 NetBird MTU를 복원한다.
Forwarding 0을 다시 확인한 뒤 owned rule/table만 회수한다. Native 설정과 Corosync hash,
HTTPS 및 BMC를 다시 확인한다. Runtime 파일만 정리하고 baseline/rollback 기록은 보존한다.
수동 rollback은 이 확인이 모두 성공한 뒤 owned rollback timer만 정리한다. 확인이 실패하면
timer를 보존하고, timer가 실행한 service 안에서도 자기 service는 중지하지 않는다.
Guest가 생긴 경우 bridge netfilter를 임의로 끄지 않는다.

Node rollback 뒤 두 PVE가 online/quorate이고 guest가 없는 상태에서만 다음으로
owned zone/VNet을 회수한다. Witness가 일시적으로 없어도 두 PVE의 정상 quorum은 필요하다.

```bash
bash scripts/apply-production-sdn.sh --rollback --apply
```

이미 commit한 운영망은 초기 timer로 되돌리지 않는다. 수동 전환은 기존 gateway를
먼저 fence하고 단일 owner를 증명한 뒤 별도 유지보수로 수행한다. SSH가 살아 있으면
기존 owner service와 if-up 재기동을 mask하고 forwarding, gateway 주소와 SNAT를
내린다. 응답이 없으면 ping 실패만으로 fence됐다고 판단하지 않고 전원 차단 등 독립
근거가 필요하다. 정상 quorum에서 owner 기록을 원자적으로 바꾸고 대체 node의
runtime을 재시작한 뒤 gratuitous ARP, 외부 새 연결과 반환을 확인한다.
원래 node를 다시 켤 때는 owner 기록을 먼저 확인하고 standby로 복귀시킨다.

Source NAT 주소와 conntrack 상태가 바뀌므로 기존 세션은 보존하지 않는다. 이 절차는
core LXC/data를 복원하거나 공개 중계 endpoint를 자동 변경하지 않는다. pve-node 중계의
전환과 서비스 writer 복구는 해당 서비스의 별도 검증 절차다.
