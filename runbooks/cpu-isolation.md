# 호스트·플랫폼·학생 CPU 경계

`scripts/cpu-isolation.py`는 Proxmox VE 9.2.11, kernel `7.0.14-15-pve`,
systemd 257에서 확인한 cgroup v2 경로를 대상으로 합니다. 다른 버전과 불완전한
SMT topology는 거부합니다. 기본 작업은 새 후보 생성과 읽기 조회입니다.
원본의 `INSTALL_REVIEWED=False`, `APPLY_REVIEWED=False`는 유지하며, 설치와 적용은
독립 검토를 마친 실행 사본과 **실제 운영자가 승인한 시작·종료 시각**이 있어야 합니다.
이 문서나 예시의 숫자가 실행 승인을 대신하지 않습니다.

## CPU와 admission의 단위

같은 socket의 물리 core 0·1 전체 SMT pair는 host에, core 2–5 전체 pair는 플랫폼에,
나머지 core 전체 pair는 학생 영역에 둡니다. CPU 번호에 일정한 offset이 있다고
가정하지 않고 sysfs `core_id`, `physical_package_id`, `thread_siblings_list`를 대조합니다.
단일 socket·core당 online thread 2개·전체 core 목록이 정확히 일치해야 합니다.

host는 2물리 core/4thread, 플랫폼은 4물리 core/8thread입니다. 여러 플랫폼 CT가 같은
플랫폼 core를 공유할 수 있지만 각 CT의 명시적 mask는 온전한 SMT pair여야 합니다.
예시의 CT는 기존 `cores: 2` 또는 `cores: 4` 값을 유지합니다. 실제 배치는 CT별 config를
다시 읽어 정하고, 이미 정의된 사용자 cpuset은 자동 교체하지 않습니다.

노드 등록의 schema 2 수식은 그대로입니다.

`(physical.cpu_threads - reserved.cpu_threads) × allocation_ratio - committed_vcpu`

`reserve_cpu_threads=12`에는 host 4와 플랫폼 8을 합칩니다. 비율 2는 학생 영역에만
적용합니다. 이미 플랫폼 DB가 합산하는 학생 VM은 `committed_vcpu`에 넣지 않습니다.
보존할 외부 시험 VM만 별도로 commit합니다. 예를 들어 물리 24/32thread, 외부 5/3vCPU이면
학생 admission은 19/37vCPU입니다. 플랫폼 CT의 vCPU를 별도 commit에 다시 넣지 마세요.
RAM·디스크 예약량과 실제 `cpu_threads` 컬럼의 물리 총량은 유지합니다.

## 생성과 읽기 검증

`examples/cpu-isolation.json`을 환경별 보호 파일로 복사하고 실제 node, CT 분류와 설치된
PVE 라이브러리의 SHA-256을 넣으세요. 예시의 0 해시와 review 문구는 실제 증거가 아닙니다.
`clone_inheritance_review_ref`는 **그 SHA의 설치된** clone 코드가 hook을 상속하는지 확인한
독립 검토 기록입니다. 소스 조회만으로 clone의 실행 결과나 I/O 부하 검증을 주장하지 않습니다.
복구 CT는 `parked_ct_ids`에 명시하고 반드시 stopped/onboot 0이어야 합니다.
새로운 미분류 LXC가 나타나면 먼저 분류를 확인합니다.

PVE host root에서 실행합니다. 파일은 root 소유 보호 경로에 두고 기존 출력 파일은 보존합니다.

```bash
python3 scripts/cpu-isolation.py collect \
  --policy /root/cpu-isolation.json --output /root/cpu-isolation-before.json
python3 scripts/cpu-isolation.py render \
  --policy /root/cpu-isolation.json \
  --inventory /root/cpu-isolation-before.json \
  --inventory-sha256 '<실제 출력 SHA-256>' --output /root/cpu-isolation-candidate
```

`render`는 5분 이내의 inventory, 전체 topology·PVE 코드 pin·guest 분류를 확인하고
새 디렉터리에 후보만 만듭니다. `collect`는 조회 결과 파일 하나를 새로 쓰며 host 설정을
바꾸지 않습니다. 명령 stderr, 프로세스 env와 guest config 본문은 출력하지 않습니다.

적용 후 `check --output`은 전체 읽기 observation을 새 보호 파일에 기록하고,
그 canonical SHA와 thread inventory canonical SHA, 보호 authority SHA에 묶인
`pve-cpu-isolation-native-v1` 결과를 출력합니다. authority는 실행 승인 참조이며 native 사실
증거를 대신하지 않습니다.

```bash
python3 /usr/local/libexec/pickle-cpu-isolation.py check \
  --output /root/cpu-isolation-after.json
```

## 영속 후보와 시작 경계

- `system.slice`, `user.slice`, `init.scope`의 `AllowedCPUs`와 root의 `lxc.monitor`,
  `lxc.pivot` mask는 host thread 4개를 사용합니다.
- `/lxc`는 플랫폼 thread 8개의 **normal** `root` partition입니다.
  requested·exclusive·exclusive.effective·effective가 모두 그 집합과 일치해야 합니다.
  `isolated`나 `root invalid (...)`는 받지 않습니다. normal partition의 scheduler
  load balancing은 유지합니다.
- CT config에 `lxc.cgroup2.cpuset.cpus`를 추가합니다. 설치된 pvestatd가 이 명시적 설정을
  자동 재배치 대상에서 제외하는 코드도 SHA로 고정합니다. payload `/lxc/<ctid>/ns`와
  실제 thread mask를 별도로 조회합니다.
- `qemu.slice`의 `AllowedCPUs`와 cpuset 상속은 학생 집합을 모든 자식 scope의 상한으로
  제한합니다. 현재의 모든 QEMU·템플릿에 동일한 root 실행 hook을 연결합니다.
- `pickle-cpu-isolation.service`는 `pve-cluster` 뒤, `pve-guests`·`qemu.slice` 전에 부모 경계를
  준비합니다. LXC는 매 시작의 `ExecStartPre`, enrolled QEMU는 PVE의 매 `pre-start` hook에서
  읽기 검증을 다시 수행합니다. invalid partition·mask/topology drift·미분류 CT·빠진 hook은
  시작 전에 실패합니다. `RemainAfterExit=yes` 자체는 매 VM 검증이나 runtime 감시가 아닙니다.

PVE의 hook은 config에 연결된 guest만 검사합니다. **임의의 특권 관리자가 새 hook 없는
QEMU config를 직접 만드는 것까지 막는 글로벌 PVE hook은 없습니다.** 일반 API clone의
범위는 모든 카탈로그 템플릿에 hook이 있고, 정확한 clone 코드와 실제 owned clone에서
상속을 검증한 뒤에만 인정합니다. 새 템플릿은 enrollment 전 활성화하지 않습니다.
다른 node로 이동하기 전에도 같은 executable·정책·hook volume과 CPU 경계를 확인해야 합니다.

local storage는 기존 dir `/var/lib/vz`와 모든 기존 content를 보존한 채 `snippets` 한 종류를
CAS digest 아래 추가합니다. 새 실행 파일은
`/var/lib/vz/snippets/pickle-cpu-isolation-hook.py`이고 mode 0755/root입니다.
기존 hook·cpuset·control unit·enable link가 있으면 설치를 거부합니다.
기존 drop-in은 `pve-guests.service.d/example-production-network.conf` 한 파일만
SHA `4bd160888cfc1743de5f7fd89b59b9dbba37ddd10080506fd03b95a0868e587e`,
root:root·0600·단일 링크 조건으로 허용합니다. 필수 network dependency의 원래
바이트·inode·mtime/ctime·소유·mode를 읽기 inventory에 묶고, 설치 전 재조회 및
설치/enable/activation 뒤에 보존됐는지 확인합니다. 이 파일을 교체하거나
CPU manifest의 새 파일로 생성하지 않습니다. 다른 기존 override·내용·권한과
설치 중 추가된 override는 거부합니다. 기존 network 파일이 없으면 임의로
만들지 않습니다. 실패한 이전 시도·프로그램·before 자료는 보존하고, 보완한
프로그램에는 새 stage와 새 native inventory를 사용합니다.
PVE GET의 storage content CSV는 같은 capability 집합이어도 순서가 달라질 수
있습니다. 읽기 projection은 기존 네 종류 또는 그 집합에 `snippets`를 더한
경우에만 순서를 정렬합니다. 중복·누락·다른 capability·문자열 아닌 값은
거부하며 digest, raw `storage.cfg` SHA와 다른 설정 필드는 그대로 CAS 비교합니다.

## 승인 창의 설치 순서와 실패

독립 검토자는 manifest 전량, 실제 설치 코드 SHA와 unit ordering을 확인합니다.
실행 사본에서 **두 named guard만** 승인된 값으로 바꾸고 다시 해시를 고정합니다.
일괄 `False` 치환은 하지 않습니다. approval JSON은 정확한 node·canonical policy SHA·
실행 program SHA·보호 authority canonical SHA·실제 사용자 승인 기록 참조/해시·UTC 시작/종료·새 32hex nonce·영속 부팅
재적용 승인을 담습니다. 코드가 보호 파일을 받아들이는 것은 사람의 권한 확인을 대신하지 않습니다.

```bash
python3 /root/reviewed-cpu-isolation.py install \
  --policy /root/cpu-isolation.json \
  --inventory /root/cpu-isolation-before.json \
  --inventory-sha256 '<실제 새 inventory SHA-256>' \
  --admission /root/cpu-isolation-approval.json \
  --authority /root/cpu-isolation-authority.json
```

첫 실제 효과 전에 `/var/backups/pickle-cpu-isolation-<nonce>`를 0700으로 새로 만듭니다.
전체 guest config·storage config·before observation은 host 내부 0600 backup에 보존합니다.
새 정책은 `/etc/pickle/cpu-isolation/`, 프로그램은 `/usr/local/libexec/`에 O_EXCL로 설치하며
승인 기록과 전체 파일 manifest를 고정합니다. guest 수정은 PVE config lock과 전체 원본 SHA
CAS 아래 수행하고, 다른 필드의 canonical digest가 보존됐는지 조회합니다.

unit 문법 검사 뒤 부모 mask를 적용하고 실제 partition·모든 thread·hook·CT mask를 조회합니다.
이 검증이 통과한 뒤 daemon-reload와 영속 unit enable·첫 start를 수행하고 active 상태와
최종 조회를 기록합니다. 실제 효과 직전마다 승인 시각을 다시 확인하며, guest·storage
config lock 안에서도 deadline을 검사합니다. 창이 끝나면 남은 효과를 진행하지 않습니다.
게스트와 서비스를 정지·재시작하거나 VM 수·cores·onboot를 바꾸지 않습니다.
영속 boot 재적용은 완료된 첫 설치, 설치 manifest·실제 byte·승인 기록을 다시 확인하고
boot ID별 새 시도 기록을 남깁니다. 이전 incomplete 기록은 재실행하지 않습니다.

타임아웃·CAS 실패·unit 오류·부분 쓰기 뒤에는 소유 시도와 backup을 보존하고
`installation-incomplete.json`을 남깁니다. 실패한 install/boot를 자동 replay하거나
파일을 지워 다시 시도하지 않습니다. source를 아직 동결하지 않은 단계라면 source 운영을
그대로 두고 전환을 중단합니다.

원복은 독립 검토 후 정확한 자기 시도의 현재 after hash를 대조하여, 이번에 추가한
hook·CT cpuset·snippets content·unit 파일과 enable link만 회수하고 원래 cgroup mask를
보호 before 기록과 비교해 복구합니다. 다른 관리자가 변경한 파일·guest·storage는 과거 전체
config로 덮어쓰지 않습니다. `root` partition을 `member`로 바꾸면 하위 partition에 영향을
줄 수 있으므로 현재 descendants부터 확인합니다. 이 프로그램은 자동 원복을 제공하지 않습니다.

## 입증 범위와 남은 native 검증

출력의 `userland_masks_equal`은 requested/effective cpuset과 QEMU·vhost TID census의
실제 관계만 뜻합니다. `/qemu.slice` numeric scope·실제 QEMU 프로세스·running guest ID가
같고, 모든 TID가 고유하며 start tick이 안정적이고 학생 집합 안에 있어야 합니다.
host 밖 vhost helper와 누락·변경된 thread census는 거부합니다.

normal partition은 hardware IRQ·RPS/RFS·unbound workqueue를 자동 제외하지 않습니다.
`irq_and_kernel_workers_verified=false`와 `performance_or_full_protection_claim=false`를
유지합니다. 별도 I/O 관측과 owned CPU/I/O 부하·플랫폼 latency 검증에서 잔여 영향을 기록하고,
source 동결 전 host 준비 기준과 공개 후 실제 학생 clone 기준을 구분해 판정하세요.
부팅 인자·패키지·호스트 IP·파일시스템을 이 도구가 바꾸지 않습니다.

오프라인 테스트는 kernel이나 호스트의 cgroup에 접근하지 않습니다.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/tests/test_cpu_isolation.py
```

기본 모델 테스트 통과만으로 설치·재부팅·PVE hook/clone 상속·I/O 부하 검증이 끝났다고
기록하지 마세요. 기술 의미는 [Linux cgroup v2 문서](https://github.com/torvalds/linux/blob/master/Documentation/admin-guide/cgroup-v2.rst)와
[systemd 257의 AllowedCPUs 명세](https://github.com/systemd/systemd/blob/v257/man/systemd.resource-control.xml)를 따릅니다.
