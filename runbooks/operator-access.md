# 전용 운영자 SSH 계정

backup-node의 Ubuntu 22.04에 새 `pickle` 계정과 전용 Ed25519 공개키를 등록하는 절차다.
이 계정은 `pickle ALL=(ALL:ALL) NOPASSWD: ALL`로 **비밀번호 없는 전체 root 권한**을
받는다. 명령 제한 계정이 아니며, 해당 개인키를 가진 사람과 자동화는 호스트 전체를
관리할 수 있다. 개인키는 운영자가 별도 보호 위치에서 보관하고 이 스크립트로 전달하지 않는다.

기존 사용자와 sudo 권한, 전역 SSH 설정을 유지한다. 계정 비밀번호는 잠그고 추가 그룹에
가입시키지 않는다. `PermitRootLogin no`와 기존 PAM 기반 공개키 인증을 확인한다.
기존 사용자의 `PasswordAuthentication yes`는 유지할 수 있다. 새 계정의 잠긴 비밀번호와
단일 공개키가 접근 경계이며, 키에는 agent/port/X11 forwarding 금지 옵션을 붙인다.
PTY는 사용할 수 있다. 전체 root 권한이 있으므로 키 옵션을 권한 격리로 간주하면 안 된다.

## 준비와 사전 검사

`scripts/provision-operator-access.py`, `scripts/revoke-operator-access.py`와
`scripts/lib/operator_access.py`를 함께 준비하고 SHA256을 확인한다. 사용자 경로에서
검토한 파일은 실행 전에 root 소유의 보호 디렉터리에 스냅샷으로 복사한다. 이때 `scripts/`
아래의 상대 배치를 유지한다. 실행 파일과 모듈의 SHA256을 다시 대조한 뒤 `python3 -I`로
실행한다. 비밀번호는 기존 관리자의 SSH 터미널에서 sudo가 요청할 때만 입력한다.

공개키 파일은 옵션이 붙지 않은 `ssh-ed25519` 한 줄만 받는다. 키 파일의 정확한 SHA256을
인자로 전달한다. 계정·동명 그룹·`/home/pickle`·`/etc/sudoers.d/90-pickle` 중 하나라도
이미 있으면 등록을 거부한다. 기존 계정을 가져오거나 덮어쓰지 않는다.

상태 디렉터리의 부모는 root 소유 0700이어야 한다. 아래 예시의 부모가 없다면 먼저
`sudo mkdir -m 0700 /root/pickle-backup`으로 만든다. 이미 있다면 소유권과 권한을 확인한다.
실행마다 새 상태 디렉터리를 지정한다. 기존 상태 디렉터리는 재사용하지 않는다.

```bash
sudo --preserve-env=SSH_CONNECTION python3 -I scripts/provision-operator-access.py \
  --expected-host backup-node \
  --public-key-file /absolute/path/to/operator.pub \
  --public-key-sha256 <reviewed-sha256> \
  --backup-dir /root/pickle-backup/2026-09-17-operator-access
```

기본 실행은 사전 검사다. root 전용 잠금 파일만 `/run/lock/`에 만들며 계정과 설정은
변경하지 않는다. 현재 연결의 `SSH_CONNECTION`으로 원격 출발지와 호스트 목적지, SSH
포트를 확인한다. 환경 변수가 없다면 `--ssh-source-address`, `--ssh-local-address`,
`--ssh-port`를 실제 연결 값으로 지정한다. `sshd -T -C`의 `host`는 원격 출발지 주소다.

`UseDNS no`, `UsePAM yes`, 공개키 허용과 표준 사용자 홈의 authorized_keys 경로를 확인한다.
Allow/DenyUsers·Groups, 강제 명령, 외부 키 조회나 CA 인증, 다른 인증 체인이 있으면 별도
검토를 요구하고 종료한다. 스크립트가 기존 SSH 정책을 완화하거나 서비스를 재시작하지 않는다.

## 등록과 접속 확인

사전 검사와 같은 명령에 `--apply`를 추가한다. 기존 관리자의 연결을 유지한다.
스크립트는 다음 순서로 진행한다.

1. root 전용 상태 디렉터리에 계정 DB와 sudoers 사본을 0600으로 보존한다.
2. 후보 sudoers 파일과 기존 설정에 후보를 포함한 전체 구성을 `visudo`로 검증한다.
3. 빈 root 전용 skeleton에서 새 계정과 홈을 만든다. 기존 `/etc/skel`의 키는 복사하지 않는다.
4. 비밀번호 잠금과 SSH 정책을 재확인하고, 소유 sudoers 파일을 root:root 0440으로 설치한다.
5. 전체 `visudo` 검사 후 실제 `pickle` UID에서 `sudo -k -n id -u`의 결과가 `0`인지 확인한다.
   호출 환경에 기존 `SUDO_USER`와 `SUDO_UID`를 넘기지 않는다.
6. 새 사용자 소유 `.ssh` 0700과 `authorized_keys` 0600에 검토한 공개키를 등록한다.

실제 SSH 접속은 별도로 확인해야 한다. 기존 호스트키 검증을 유지하면서 새 키로 접속하고
`id`, `sudo -n id`, `sudo -n true`를 확인한다. 스크립트의 `ssh_login_verified=false`는 이
외부 검증이 아직 없다는 뜻이다. 확인 전에는 기존 관리자 연결을 닫지 않는다.

오류가 나면 기록된 새 계정을 잠그고 만료 처리하며 shell을 `nologin`으로 바꾸려고 시도한다.
계정 차단 성공 여부와 별개로 원래 내용과 소유권이 일치하는 등록 키와 sudoers도 각각 회수한다.
차단·회수·상태 기록 중 실패한 항목은 stderr에 모두 표시한다. 자동 차단도 실패할 수 있으므로
성공으로 간주하지 말고 출력과 `state.json`을 확인한다.
`uid`가 기록되기 전에 useradd가 실패했다면 소유권을 추정해 지우지 않는다. root가 계정·그룹과
생성 시점을 확인해야 한다. 전체 passwd/shadow 사본을 되돌리면 다른 변경도 사라지므로 자동
복원하지 않는다.

## 철회와 부분 설치 복구

기존 관리자 또는 별도 root 터미널에서 진행한다. `pickle` 자신의 sudo 연결은 사용할 수 없다.
먼저 새 계정으로 시작한 작업을 중지하고 `loginctl terminate-user pickle`로 로그인 세션을
종료한다. root로 승격해 분리한 작업과 서비스도 확인해 정리한다. UID 프로세스 검사는
이미 분리된 root 작업의 종료까지 증명하지 못한다.

```bash
sudo python3 -I scripts/revoke-operator-access.py \
  --expected-host backup-node \
  --state-dir /root/pickle-backup/2026-09-17-operator-access
```

사전 검사 결과와 세션 종료를 확인한 뒤 같은 명령에 `--sessions-quiesced --apply`를 추가한다.
이 명령은 부분 설치의 복구 명령으로도 사용한다. 새 계정 UID의 프로세스가 남으면 거부한다.

철회는 상태 파일의 UID/GID/홈이 현재 계정과 일치할 때만 진행한다. 계정을 잠금·만료·nologin
처리하고, 원래 내용이 그대로인 소유 sudoers 파일과 등록했던 공개키 한 줄만 제거한다.
다른 키와 홈 파일, 계정 및 그룹은 보존한다. sudoers 내용이 바뀌었으면 임의로 삭제하지 않는다.
변경된 파일과 root 권한으로 새로 만든 접근 경로는 운영자가 별도로 검토해야 한다.

상태 디렉터리에는 shadow 사본 등 민감한 자료가 있으므로 root 0700/파일 0600으로 보호하고
Git에 넣지 않는다. 회수 확인 후 운영자의 보호 자료 보존 정책에 따라 정리한다.
