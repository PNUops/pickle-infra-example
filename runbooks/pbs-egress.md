# PBS DB egress 설정 런북

이 런북은 pve2/pve3 production network의 active gateway에서 DB LXC가 PBS의 TCP 8007로
나가는 좁은 egress를 준비하는 절차입니다. 기존 설정에 backup_service가 없으면 생성되는
규칙 바이트가 유지됩니다. 이 문서는 설정 후보와 검증 범위만 설명하며 실제 PBS 연결이나
백업 성공을 주장하지 않습니다.

## 허용 범위

optional backup_service는 다음 세 값만 가집니다.

```json
{
  "source": "198.18.1.21",
  "destination": "203.0.113.40",
  "port": 8007
}
```

source는 pinfra 안의 DB 주소이고 destination은 guest CIDR와 양 node의 campus, mesh,
BMC 주소 밖에 있어야 합니다. port는 숫자 8007만 허용합니다. 실제 주소는 승인된
환경 inventory에서 넣으며 예시 주소를 운영 설정에 복사하지 않습니다.

active gateway owner에서만 아래 세 tuple을 생성합니다.

- pinfra → wt0: source에서 destination의 TCP 8007로 가는 새 연결을 RETURN합니다.
- wt0 → pinfra: PBS 출발지의 TCP source port 8007에서 DB가 만든 원래 client port로 돌아오는 ESTABLISHED/RELATED만 RETURN합니다.
- 같은 source/destination/port tuple을 wt0에서 MASQUERADE합니다.

standby node에는 새 연결 forward rule이나 NAT를 생성하지 않습니다. 기존 default DROP,
host-management guard, guest-to-pinfra guard, PVE filtering과 conntrack 상태는 유지합니다.
PVE firewall bypass, 전체 ACCEPT, conntrack flush, routing policy 확대를 수행하지 않습니다.

## 설정 변경과 재적용

production network는 입력 config, runtime library, state.json과 baseline hash를 하나의
소유 단위로 관리합니다. active state에서 config나 library만 바꾸고 reconcile하면 state의
config equality와 hash guard가 중단시킵니다. 실행 중인 파일을 직접 교체하거나 systemd
unit을 임의 reload하지 않습니다.

변경 순서는 다음과 같습니다.

1. 현재 owner, pending, rollback timer와 state.json을 읽고 이전 변경이 committed인지 확인합니다.
2. 새 config 후보를 dry-run과 unit test로 검사합니다. backup_service가 없던 config는
   기존 plan과 byte-equivalent인지 확인합니다.
3. 아직 prepare하지 않은 환경이면 기존 entrypoint로 runtime library와 config를 함께 설치합니다.

   ```bash
   bash scripts/apply-production-network.sh prepare --backup-dir /absolute/protected/backup --apply
   ```

4. 이미 committed guest가 있는 환경에는 config와 runtime library를 바꾸는 재적용 entrypoint가
   없습니다. 기존 gateway/VNet을 철거하거나 rollback해서 update를 흉내 내지 않습니다.
   별도 보호 update 절차와 maintenance window를 설계·승인한 뒤에만 변경합니다.
5. committed active state에서는 아래 기존 entrypoint만 사용합니다. startup readiness와
   state equality 검사가 먼저 실행되며, 다른 config면 중단합니다.

   ```bash
   bash scripts/apply-production-network.sh reconcile \
     --config /etc/pickle/production-network.json \
     --startup-wait-seconds 120 --apply
   ```

6. active owner, standby node, source/destination/port와 generated rule plan을 읽기 전용으로
   확인합니다. pve2/pve3가 관리경로와 quorum proof를 통과하기 전에는 운영 검증으로 표시하지
   않습니다.

## 검증과 실패 경계

- invalid source, guest 또는 host-management destination, non-8007 port, extra config key는
  validation에서 거부합니다.
- active plan은 정확한 forward RETURN 2개와 MASQUERADE 1개만 추가합니다. standby plan은
  이 tuple의 forward/NAT를 포함하지 않습니다.
- nft translator는 TCP sport와 dport를 정확히 출력해야 하며, 다른 protocol이나 option은
  거부합니다.
- 이 설정은 DB LXC에서 PBS port까지의 firewall/routing intent만 다룹니다. PBS 인증,
  datastore namespace, encrypted dump, 최신 dump 시점, restore와 RPO/RTO는 별도 절차와
  실측 증거가 필요합니다.

## 복구

잘못된 config나 reload 실패가 있으면 active owner 변경을 반복하지 않습니다. state.json,
 owner 기록, backup receipt와 rollback timer를 보존하고, 기존 gateway/VNet을 철거하지 않은
 상태에서 별도 보호 update 절차를 기다립니다.
standby는 새 forward/NAT가 없어야 하며, source/destination tuple이 다른 rule로 넓어졌다면
성공으로 판정하지 않고 중단합니다.
