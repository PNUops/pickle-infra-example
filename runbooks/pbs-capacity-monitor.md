# PBS datastore 용량 조회기

정기 용량 조회는 PBS API의 datastore 상태 endpoint 한 번에서 전체와 사용, 가용 바이트를 읽어요. 별도 `--check-read-denied` 시험은 존재하지 않는 합성 snapshot 경로에 download 요청을 보내므로 상태 조회와 분리해 수동 실행해요. 정기 검사는 pve-node-3에서 5분 간격으로 실행하고 상태가 바뀔 때 운영자에게 메일을 보내요. datastore 수정과 오래된 백업 정리는 하지 않아요.

## 읽기 전용 계정

기존 백업 계정의 권한을 넓히지 말고 용량 확인 전용 사용자와 privilege separation이 켜진 token을 만들어요. 사용자와 token 양쪽에 같은 역할을 지정 datastore 경로에서만 부여해요. 실효 권한은 두 ACL의 교집합이에요. 다음 명령의 이름은 모두 예시예요.

아래 user/token 발급과 secret 보관 절차를 마친 뒤 다음 ACL 명령을 실행해요.

```bash
set -euo pipefail
proxmox-backup-manager acl update /datastore/example-store DatastoreAudit \
  --auth-id 'capacity@pbs' --propagate false
proxmox-backup-manager acl update /datastore/example-store DatastoreAudit \
  --auth-id 'capacity@pbs!probe' --propagate false
proxmox-backup-manager user permissions 'capacity@pbs' --path /datastore/example-store
proxmox-backup-manager user permissions 'capacity@pbs!probe' --path /datastore/example-store
proxmox-backup-manager acl list
```

두 identity 모두 지정 datastore에서 감사 권한만 보여야 해요. 읽기나 쓰기, 백업, prune, verify 권한 또는 더 넓은 경로가 나오면 적용하지 않아요. 기존 writer/reader ACL이 바뀌지 않았는지도 대조해요.

조회 token으로 상태 endpoint가 성공하고 백업 파일 읽기는 권한 거부를 반환하는지 확인해요. 부정 검사는 존재하지 않는 합성 snapshot 식별자를 사용해요. 응답에 백업 파일 내용이 포함될 수 있으므로 본문을 출력하지 않아요. 정확한 권한 거부만 합격이며 다른 오류 코드를 통과로 보지 않아요.

### 전용 user·token 발급과 일회용 secret 보관

token을 발급하기 전에 `pve-node-3`의 최종 경로(`/etc/pickle-example-capacity/token`)가 비어 있는지 확인하고 PBS VM에서 옮길 보호된 단일 전달 경로를 정해요. 이 런북에서 두 호스트 사이의 전달 명령은 검증하지 않았어요. 비밀이 아닌 canary로 먼저 시험해 secret이 명령 인자와 터미널, shell history, 세션 기록, 일반 로그에 나타나지 않고 목적지에 root 소유 `0600` 파일로 저장되는지 확인해요. **검증된 전달 경로가 없으면 user나 token을 만들지 말고 중단해요.**

`pve-node-3`의 root shell에서 설정 상위 디렉터리가 없으면 만들고, 있으면 일반 디렉터리이며 root 소유 mode `0700`인지 확인해요. 종류나 권한이 다르면 수정하지 말고 중단해요. 준비한 뒤 최종 token 경로에 파일이나 심볼릭 링크가 없는지도 확인해요.

```bash
set -euo pipefail
token_dir=/etc/pickle-example-capacity
if test -e "$token_dir" || test -L "$token_dir"; then
  if test ! -d "$token_dir" || test -L "$token_dir"; then
    printf '%s\n' 'Protected token directory is not a regular directory' >&2
    exit 1
  fi
  if test "$(stat -c '%u:%a' "$token_dir")" != '0:700'; then
    printf '%s\n' 'Protected token directory owner or mode is invalid' >&2
    exit 1
  fi
else
  install -d -o root -g root -m 0700 "$token_dir"
fi
if test -e "$token_dir/token" || test -L "$token_dir/token"; then
  printf '%s\n' 'Token destination already exists; preserve it and stop' >&2
  exit 1
fi
```

PBS의 root shell 하나에서 이어 실행해요. 각 Bash block은 `set -euo pipefail`을 먼저 켜요. 어떤 검사든 실패하면 원인을 확인하기 전까지 다음 명령을 실행하지 않아요. ACL 예시는 user `capacity@pbs`, token 이름 `probe`를 사용해요. 다른 이름을 고르면 모든 단계에서 같은 이름을 써요. 다른 운영자가 같은 ID를 동시에 만들지 않도록 발급을 직렬화해요. 보호 디렉터리에 JSON user 목록을 저장하고 새 ID가 없는지 확인해요. 이미 있거나 출력 형식을 판정할 수 없으면 재사용하거나 덮어쓰지 말고 중단해요. 명령은 [PBS 공식 user 관리 문서](https://pbs.proxmox.com/docs/user-management.html)와 [CLI 명세](https://pbs.proxmox.com/docs/proxmox-backup-manager/man1.html)를 따르며 실행 전에 설치 버전에서 문법을 다시 확인해요.

```bash
set -euo pipefail
umask 077
protected_token_dir=$(mktemp -d /root/pbs-example-capacity.XXXXXXXX)
if test "$(stat -c '%u:%a' "$protected_token_dir")" != '0:700'; then
  printf '%s\n' 'Protected staging directory owner or mode is invalid' >&2
  exit 1
fi
users_file=$(mktemp "$protected_token_dir/users.XXXXXXXX")
proxmox-backup-manager user list --output-format json > "$users_file"
```

다음 검사는 JSON이 list인지, 모든 항목에 형식이 맞는 고유한 `userid`가 있는지, 새 ID가 없는지 확인해요. 원본 JSON과 ID 목록 대신 고정 문구만 출력해요. 형식이 달라지거나 해석할 수 없는 항목이 있으면 종료 코드 1로 중단해요.

```bash
set -euo pipefail
if ! python3 - "$users_file" 'capacity@pbs' <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    rows = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if not isinstance(rows, list):
        raise ValueError
    ids = [row['userid'] for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or any(not isinstance(value, str) or not
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*', value)
            for value in ids) or len(ids) != len(set(ids)):
        raise ValueError
    if sys.argv[2] in ids:
        raise ValueError
except (OSError, UnicodeError, ValueError, KeyError, TypeError):
    raise SystemExit('User preflight failed; preserve files and stop')
print('USER_ID_ABSENT')
PY
then
  exit 1
fi
```

ID가 없음을 확인한 뒤 PBS의 Debian `date`로 발급 시점부터 180일 뒤 Unix expiry를 한 번 계산해요. 같은 값을 user와 token 모두에 적용하고 UTC 만료 날짜와 token ID만 보호된 운영 기록에 남겨요. secret은 기록하지 않아요. 만료 14일 전에는 새 180일 expiry를 계산해 `user update capacity@pbs --expire "$token_expiry"`로 부모 user 만료도 연장해요. 충돌이 없는 새 이름으로 교체 token을 발급하고 ACL과 전달, 조회, 403을 검증한 다음 이전 token과 ACL을 폐기해요. 부모 user가 먼저 만료돼 새 token까지 무효가 되지 않도록 이 순서를 지켜요. 새 user의 token 목록이 비어 있는지도 확인해요.

```bash
set -euo pipefail
token_expiry=$(date -u -d '+180 days' +%s)
case "$token_expiry" in ''|*[!0-9]*) exit 1 ;; esac
token_expires_at=$(date -u -d "@$token_expiry" '+%Y-%m-%d %H:%M:%S UTC')
printf 'Token expiry: %s\n' "$token_expires_at"
proxmox-backup-manager user create capacity@pbs --expire "$token_expiry"
tokens_file=$(mktemp "$protected_token_dir/tokens.XXXXXXXX")
proxmox-backup-manager user list-tokens capacity@pbs --output-format json > "$tokens_file"
```

새 user의 token 목록도 검사해요. 각 항목에 `tokenid`가 있고 ID가 user와 일치하는 형식이며 중복이 없는지 확인해요. 신규 user의 목록은 비어 있어야 해요. secret이나 JSON은 출력하지 않아요.

```bash
set -euo pipefail
if ! python3 - "$tokens_file" 'capacity@pbs' <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    rows = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if not isinstance(rows, list):
        raise ValueError
    ids = [row['tokenid'] for row in rows if isinstance(row, dict)]
    pattern = re.compile(re.escape(sys.argv[2]) + r'![A-Za-z0-9][A-Za-z0-9._-]*')
    if len(ids) != len(rows) or any(not isinstance(value, str) or not pattern.fullmatch(value)
            for value in ids) or len(ids) != len(set(ids)) or ids:
        raise ValueError
except (OSError, UnicodeError, ValueError, KeyError, TypeError):
    raise SystemExit('Token preflight failed; preserve files and stop')
print('TOKEN_LIST_EMPTY')
PY
then
  exit 1
fi
```

`generate-token`은 Result secret을 한 번만 반환하며 `--output-format`을 지원하지 않아요. stdout은 root 소유 `0600` 파일로 바로 보내요. shell tracing과 `tee`, secret이 포함된 command substitution, `cat`, JSON을 화면에 표시하는 명령은 쓰지 않아요.

```bash
set -euo pipefail
token_result=$(mktemp "$protected_token_dir/result.XXXXXXXX")
token_payload="$protected_token_dir/token.payload"
proxmox-backup-manager user generate-token capacity@pbs probe \
  --expire "$token_expiry" > "$token_result"
if test ! -f "$token_result" || test -L "$token_result" || test ! -s "$token_result"; then
  printf '%s\n' 'Protected token Result file is invalid' >&2
  exit 1
fi
if test "$(stat -c '%u:%a' "$token_result")" != '0:600'; then
  printf '%s\n' 'Protected token Result owner or mode is invalid' >&2
  exit 1
fi
```

다음 Python 처리는 Result에서 `tokenid`와 `value`만 읽어요. 예상한 token ID와 지원하는 ASCII 형식인지 확인한 뒤 token byte를 그대로 담은 root 소유 `0600` 파일을 만들어요. **끝에 개행을 추가하지 않아요.** probe는 token 앞뒤 공백을 거부해요. argv에는 파일 경로와 비밀이 아닌 token ID만 전달하고 secret은 argv와 환경변수, 터미널, 로그에 넣지 않아요.

```bash
set -euo pipefail
if ! python3 - "$token_result" "$token_payload" \
  'capacity@pbs!probe' <<'PY'
import json
import os
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
expected_id = sys.argv[3]
with source.open('rb') as result_file:
    raw = result_file.read(4097)
if len(raw) > 4096 or not raw.startswith(b'Result: '):
    raise SystemExit('Unexpected token Result format; preserve files and stop')
try:
    result = json.loads(raw[len(b'Result: '):])
except (UnicodeError, ValueError):
    raise SystemExit('Invalid token Result JSON; preserve files and stop')
if not isinstance(result, dict) or set(result) != {'tokenid', 'value'}:
    raise SystemExit('Unexpected token Result fields; preserve files and stop')
secret = result['value']
if result['tokenid'] != expected_id or not isinstance(secret, str):
    raise SystemExit('Generated token identity does not match; preserve files and stop')
try:
    secret_bytes = secret.encode('ascii')
except UnicodeError:
    raise SystemExit('Generated secret is not ASCII; preserve files and stop')
if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', secret):
    raise SystemExit('Generated secret format is unsupported; preserve files and stop')
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as output:
    output.write(secret_bytes)
    output.flush()
    os.fsync(output.fileno())
PY
then
  exit 1
fi
if test ! -f "$token_payload" || test -L "$token_payload" || test ! -s "$token_payload"; then
  printf '%s\n' 'Protected token payload is invalid' >&2
  exit 1
fi
if test "$(stat -c '%u:%a' "$token_payload")" != '0:600'; then
  printf '%s\n' 'Protected token payload owner or mode is invalid' >&2
  exit 1
fi
rm -- "$token_result"
if test -e "$token_result" || test -L "$token_result"; then
  printf '%s\n' 'Protected token Result file removal failed' >&2
  exit 1
fi
```

payload를 쓴 뒤 일반 파일이며 심볼릭 링크가 아니고, 비어 있지 않으며 root 소유 mode `0600`인지 확인해요. 확인 뒤 Result JSON을 삭제하고 사라졌는지 확인해요. 파싱이나 ID 확인, 쓰기에 실패하면 token이 이미 만들어졌을 수 있으므로 보호 파일을 보존하고 재발급하지 않은 채 중단해요. 목적지 readback 뒤 원본 payload와 user/token 목록, 비어 있는 보호 디렉터리를 삭제하고 모두 사라졌는지 확인해요.

PBS VM에서 `pve-node-3`로 옮기는 검증된 명령은 없어요. 미리 정한 보호 전달 방식만 사용하고 `pve-node-3`에서 목적지 파일의 소유자와 권한, 비어 있지 않음을 확인해요. 확인 뒤 PBS의 원본 payload를 삭제해요. byte와 목적지 권한을 보존하지 못하면 검토되지 않은 명령이나 터미널 붙여넣기로 대신하지 말고 중단해요. 아래 로컬 설정에서 token 파일을 다시 확인하고 probe `--check`를 실행해요. token과 설정 파일은 root 소유 `0600`, 상위 디렉터리는 root 소유 `0700`이어야 해요.

`pve-node-3`를 잃었거나 일회용 secret을 다시 확보할 수 없으면 재출력하려 하지 않아요. `user list-tokens`에서 기존 token ID를 확인하고 `user delete-token`으로 폐기한 뒤 `acl update ... --delete true`로 token ACL을 제거해요. 새 180일 expiry를 계산하고 `user update capacity@pbs --expire "$token_expiry"`로 부모 user를 갱신한 뒤 충돌이 없는 새 이름으로 replacement를 한 번 발급해요. user와 새 token 양쪽에 datastore 범위 역할을 다시 부여하고 유효 권한과 상태 조회 성공, 백업 파일 접근의 HTTP 403을 재검증해요. 기존 user는 예상하지 못한 token이나 넓은 ACL이 없는지 확인한 뒤에만 재사용해요. 보호 전달 경로가 준비되기 전에는 replacement를 발급하지 않아요.

## 로컬 설정과 실행

서버 이름은 예약된 `example.invalid` 하위 도메인이고 token과 설정 파일은 root 소유 mode `0600`으로 둬요. TLS 인증서 지문은 PBS 관리 콘솔에서 별도 신뢰 경로로 대조해요. 예시 숫자를 실제 datastore 용량에 맞게 정하기 전에는 배포하지 않아요.

```bash
set -euo pipefail
if test -e /etc/pickle-example-capacity/config.json || test -L /etc/pickle-example-capacity/config.json; then
  printf '%s\n' 'Configuration destination already exists; preserve it and stop' >&2
  exit 1
fi
install -o root -g root -m 0600 config.json /etc/pickle-example-capacity/config.json
if test ! -f /etc/pickle-example-capacity/config.json || test -L /etc/pickle-example-capacity/config.json || test ! -s /etc/pickle-example-capacity/config.json; then
  printf '%s\n' 'Protected configuration file is invalid' >&2
  exit 1
fi
if test "$(stat -c '%u:%a' /etc/pickle-example-capacity/config.json)" != '0:600'; then
  printf '%s\n' 'Protected configuration owner or mode is invalid' >&2
  exit 1
fi
if test ! -f /etc/pickle-example-capacity/token || test -L /etc/pickle-example-capacity/token || test ! -s /etc/pickle-example-capacity/token; then
  printf '%s\n' 'Transferred token file is invalid' >&2
  exit 1
fi
if test "$(stat -c '%u:%a' /etc/pickle-example-capacity/token)" != '0:600'; then
  printf '%s\n' 'Transferred token owner or mode is invalid' >&2
  exit 1
fi
if ! python3 scripts/pbs-capacity-probe.py --check; then
  printf '%s\n' 'Local capacity probe check failed' >&2
  exit 1
fi
python3 scripts/pbs-capacity-probe.py
python3 scripts/pbs-capacity-probe.py --check-read-denied
```

다음 JSON의 host와 datastore, fingerprint, 임계치 숫자는 모두 예시예요. 실제 값을 독립적으로 확인하고 교체해요. `00` fingerprint는 실제 서버 pin으로 쓰지 않아요. token 값은 JSON이나 명령행 인자에 넣지 않아요.

```json
{
  "server": "pbs.example.invalid",
  "port": 8007,
  "datastore": "example-store",
  "auth_id": "capacity@pbs!probe",
  "token_file": "/etc/pickle-example-capacity/token",
  "fingerprint": "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00",
  "warning_bytes": 1000000000,
  "critical_bytes": 500000000
}
```

첫 번째 검사는 로컬 파일 권한과 형식, 두 번째는 실제 용량 상태, 세 번째는 백업 파일 읽기 거부를 확인해요. 조회 결과는 전체와 사용, 가용 바이트 및 `OK`, `WARNING`, `CRITICAL` 중 하나를 JSON으로 내보내요. status 응답의 선택적 `backend-type`은 `filesystem`만 허용하며 필드가 없던 응답도 허용해요. 다른 값이나 형식은 조회 오류로 처리하고 `ERROR`로 기록해요.

## pve-node-3 상태 전이 감시

monitor는 root만 읽을 수 있는 상태 receipt로 중복 실행을 잠그고 상태 변화를 기록해요. 상태 기록이 없는 첫 관측이 정상이면 조용한 baseline으로 남겨요. 이후 경고와 긴급, 조회 오류, 더 높은 심각도, 비정상에서 정상으로의 복귀를 알려요. 같은 상태가 계속되면 메일을 반복하지 않아요. 제출 전 실패나 서버가 명확히 거절한 경우만 재시도해요. SMTP 제출 뒤 수락 여부가 불명확하면 자동 재전송하지 않아요. receipt와 발송 기록을 확인해요.

메일은 기존 root 보호 SMTP 설정을 읽기만 하고 복사본을 만들지 않아요. 활성화 전에 수신 주소와 임계치를 확인해요. SMTP 성공은 메일 서버의 수락이며 받은 편지함 도착을 보증하지 않아요. 상태 파일을 지우면 알림 상태가 초기화되므로 삭제하지 않아요.

```bash
install -D -o root -g root -m 0755 scripts/pbs-capacity-probe.py \
  /usr/local/libexec/pickle-example/pbs-capacity-probe.py
install -D -o root -g root -m 0755 scripts/pbs-capacity-monitor.py \
  /usr/local/libexec/pickle-example/pbs-capacity-monitor.py
install -D -o root -g root -m 0644 hosts/pve-node-3/systemd/pickle-pbs-capacity-monitor.service \
  /etc/systemd/system/pickle-pbs-capacity-monitor.service
install -D -o root -g root -m 0644 hosts/pve-node-3/systemd/pickle-pbs-capacity-monitor.timer \
  /etc/systemd/system/pickle-pbs-capacity-monitor.timer
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/pickle-pbs-capacity-monitor.service \
  /etc/systemd/system/pickle-pbs-capacity-monitor.timer
systemctl start pickle-pbs-capacity-monitor.service
systemctl enable --now pickle-pbs-capacity-monitor.timer
systemctl list-timers pickle-pbs-capacity-monitor.timer
```

서비스 journal과 receipt의 상태와 이벤트, 전송 결과를 확인해요. 실패하면 timer를 중지하고 receipt를 보존해 원인을 확인해요. 수동 실행도 알림을 만들 수 있어요.

가용 바이트는 datastore 파일시스템의 보고값이에요. PBS나 TLS 조회 실패는 monitor와 메일 경로가 정상일 때 `ERROR`로 기록하고 알려요. pve-node-3나 SMTP 자체의 장애 감지는 이 monitor의 범위가 아니에요. 배열 건강이나 실제 쓰기 시험, 최신 백업의 복원 가능성은 별도 점검 대상이에요.

최종 갱신: 2026-11-04
