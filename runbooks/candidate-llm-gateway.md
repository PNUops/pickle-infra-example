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
`ssh.service`를 중지·mask하고 상태를 읽어 확인하지만, 첫 부팅과 이 명령 사이의 짧은
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
