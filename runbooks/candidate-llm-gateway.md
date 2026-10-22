# 후보 LLM 게이트웨이 준비

`scripts/bootstrap-candidate-llm.sh`는 격리된 후보 네트워크에 새 LLM 게이트웨이 LXC를
준비합니다. 기존 게이트웨이, proxy, API와 공개 진입 설정은 변경하지 않습니다. 기본 실행은
계획만 출력하며, `--apply`를 지정해야 게스트를 생성합니다.

## 입력과 계획 확인

`examples/candidate-llm.json`을 보호된 경로에 복사하고 대상 노드, 클러스터, CTID,
주소, bridge, 저장소와 artifact 경로를 실측값으로 채웁니다. 예시의 주소와 SHA-256은 실행
값이 아닙니다. Debian 13 amd64 PVE 템플릿, `llm-gateway`와 `llm-keygen` Linux amd64
바이너리, `llm-gateway.service` 유닛의 SHA-256을 각각 확인해 입력합니다. 스크립트는
다운로드나 빌드를 하지 않으며, 파일이 일반 파일인지와 입력 해시가 일치하는지 적용 전에
검사합니다. `state_dir`은 없어야 하고 상위 디렉터리는 root 소유 0700이어야 합니다.
`proxy_config_sha256`에는 대상 노드의 `/etc/pve/nodes/<node>/lxc/<proxy_ctid>.conf`
원본 바이트에 대한 SHA-256을 넣습니다. proxy 설정 파일은 root 소유 일반 파일이어야 합니다.

```bash
bash scripts/bootstrap-candidate-llm.sh --config /root/<protected>/candidate-llm.json
```

계획에서 새 게스트 하나, `onboot=false`, 서비스 `enabled=false`, `started=false`,
닫힌 권한 문서와 proxy 출발지의 TCP 8081만 허용하는 방화벽을 확인합니다. 실행 노드에서
검증한 설정으로만 아래 적용을 진행합니다.

```bash
bash scripts/bootstrap-candidate-llm.sh --config /root/<protected>/candidate-llm.json --apply
```

## 적용 경계와 실패 대응

적용은 root와 정확한 노드·클러스터·정족수, 모든 노드의 활성 PVE 작업과 HA 리소스 부재,
proxy 게스트의 설정 해시·소유권·실행 상태와 단일 `net0`의 bridge·주소, 새 CTID와
volume 부재, bridge·MTU·gateway, 저장소 여유와
IP 중복을 확인합니다. 생성 직전에도 클러스터와 활성 작업 및 HA 리소스를 다시 확인합니다.
새 게스트는 unprivileged LXC이며 `onboot=0`입니다. 소유권 description과 보호된
`manifest.json`을 남기고, 기존 게스트나 파일을 덮어쓰지 않습니다.

첫 `pct start`부터 nftables 설치 전까지는 **게스트 방화벽이 아직 없습니다.** 이 구간은
기존 격리 bridge의 경계에 의존하며, APT 단계마다 실행 제한 시간이 있습니다. 이때
gateway 데몬과 upstream 자격증명은 아직 없습니다. 게스트 OS를 확인하자마자 `ssh.socket`과
`ssh.service`, `postfix.service`, `postfix-resolvconf.path`를 중지·mask합니다.
그 직후 `eth0` MTU hook `/etc/network/if-pre-up.d/candidate-llm-mtu`를 설치하고 MTU를
즉시 적용한 뒤 `ip -j link` readback이 입력 MTU와 같을 때만 APT로 진행합니다.
APT 전후와 완료 시 각 unit의 inactive/masked 상태, hook 존재와 실제 MTU, 설정한
nameserver를 확인하고, TCP listener가 없음을 확인합니다. `/etc/resolv.conf`의 nameserver는 입력값과 일치해야 합니다.
첫 부팅과 이 명령 사이의 짧은
구간까지 닫혔다고 간주하지 않습니다. APT 네트워크 확인과 설치가 끝나면 proxy 주소에서
TCP 8081만 받는 게스트 방화벽을 활성화하고 부팅 시 networking보다 먼저 실행되도록
연결합니다.

실행 디렉터리와 바이너리는 root 소유이며 서비스 계정은 실행 파일을 교체할 수 없습니다.
바이너리와 유닛을 해시와 함께 설치한 뒤 `serviceEnabled=false`, 빈 모델·키 목록의
권한 문서를 둡니다. 환경 파일에는 listen·경로만 들어가고 upstream credential은 없습니다.
완료 시 LXC의 `onboot=0`, `llm-gateway.service`의 disabled/inactive 상태를 확인합니다.
공개 라우팅과 API 설정은 이 단계에서 바뀌지 않습니다.

생성 뒤 어느 단계에서든 실패하면 CT의 정확한 소유권을 다시 확인하고 제한 시간 안에
정지를 시도합니다. `manifest.json`의 `failure_stop`에 정지 성공, 이미 정지됨 또는
확인 실패를 기록하며 게스트와 volume은 삭제하지 않습니다. 소유권이나 정지 확인이 실패한
경우에는 CT가 실행 중일 수 있으므로 상태를 직접 확인해야 합니다. 같은 설정을 기존
CTID에 재적용하지 않습니다. Upstream
자격증명 복원, API 동기화, 서비스 활성화, 트래픽 전환과 전환 실패 시 롤백은 각각 검증한
절차에서 별도로 수행합니다.

## guest MTU 확인

PVE는 `net0`의 `mtu=`를 CT를 기동할 때 만드는 veth 양 끝에만 적용하고, guest의
`/etc/network/interfaces`에는 `mtu` 줄을 쓰지 않습니다. hook이 없으면 guest 안에서 networking이
다시 올라올 때 `eth0`이 1500으로 돌아가고, 1370 경로에서 큰 패킷이 오류 없이 버려집니다.
연결은 맺어지지만 TLS 핸드셰이크와 큰 응답이 재전송 끝에 늦거나 실패하므로 서비스
health만으로는 드러나지 않습니다.

이 단계가 들어가기 전에 이 도구로 만든 게스트, 손으로 만든 게스트, 그리고 그런 게스트의
아카이브에서 복원한 게스트에는 hook이 없습니다. 적용 직후, 복원 직후, 그리고 의심될 때
아래로 config·veth·guest 세 값과 hook을 확인합니다. 정기 확인은 `production-health.py`의
컨테이너별 `<CTID>:mtu` 점검이 맡습니다.

```
CT=<대상 CTID>
pct config "$CT" | sed -n 's/^net0: //p' | tr ',' '\n' | grep '^mtu='   # 없으면 bridge MTU를 물려받습니다
cat "/sys/class/net/veth${CT}i0/mtu"
pct exec "$CT" -- cat /sys/class/net/eth0/mtu
pct exec "$CT" -- ls -l /etc/network/if-pre-up.d/
```

세 숫자가 같아야 합니다. hook이 필요한 것은 `net0`에 `mtu=`가 있고 그 값이 1500보다 작을 때뿐입니다.
`mtu=`가 없는 LXC는 hook 없이 세 값이 같으면 정상이고, 같지 않으면
그 bridge에 맞는 `mtu=`를 `net0`에 먼저 정합니다. `mtu=`가 있는데 guest 값만 다르거나 hook이 없으면
진행하지 않습니다. hook은 생성 도구가 쓰는 것과 같은 내용의 root 소유 0755 파일이고, 아래의 1370 자리에는
`net0`의 `mtu=` 값이 들어갑니다. 이름은 원래 생성 경로의 것(`isolated-core-mtu`,
`isolated-services-mtu`, `candidate-llm-mtu`)을 따르고, 이미 다른 이름의 MTU hook이 있으면
하나를 더 만들지 않습니다.

```
#!/bin/sh
set -eu
[ "${IFACE:-}" = "eth0" ] || exit 0
exec /usr/sbin/ip link set dev eth0 mtu 1370
```

hook은 `pct push "$CT" <파일> /etc/network/if-pre-up.d/<이름> --perms 0755`로 두고 소유자가
root인지 확인합니다. 그 뒤 `pct exec "$CT" -- /usr/sbin/ip link set dev eth0 mtu <net0의 mtu>`로 즉시 적용하고
위 네 줄을 다시 읽습니다. 운영 게스트에 쓰는 일이므로 운영 변경 승인 아래에서 합니다. guest가
ifupdown2라면 `/etc/network/ifupdown2/ifupdown2.conf`의 `addon_scripts_support=1`도 확인합니다.
이 값이 0이면 hook이 실행되지 않습니다.
