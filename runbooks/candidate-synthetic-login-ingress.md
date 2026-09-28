# 합성 계정 로그인 공개 시험

이 절차는 pve-node-2 CT1200에 이미 설치된 `staging.example.com`의 mTLS 외부 계층과
health-only 내부 TLS vhost를 유지하면서, 합성 DB를 사용하는 CT1201의 로그인
화면과 필요한 API만 잠시 노출한다. pve-node의 공개 SNI 라우터, 기존 서비스,
CT1100, host 방화벽, CT1201 설정과 DB는 바꾸지 않는다. 적용 대상은 CT1200의
`/etc/nginx/conf.d/pickle-interim-tls.conf` 한 파일이다.

## 적용 전 확인

운영자는 pve-node과 CT1200의 현재 health, 인증서 이름, 허용·거부 출발지 결과를
먼저 기록한다. CT1200에서 `sha256sum /etc/nginx/conf.d/pickle-interim-tls.conf`가
`4a457f2d58fe16f8d11af4b6be6f3a4c8e8c73ac52b8d9010eb8152964c3346c`인지
확인한다. 이 값은 정제된 health-only 기준 설정의 예시 해시다. 설정 파일의
소유자와 모드는 root:root 0644여야 한다. 기존 CT1200 TLS 적용 도구의 `recover`는
이 시험 설정 해시를 인정하지 않으므로 **시험 동안 실행하지 않는다.**

CT1200 내부 보관본과 독립적으로, **pve-node-2 host**에서 적용 전에 원본 파일을 보호된
위치로 한 번 더 가져온다. 기존 보호 사본이 있으면 소유권·모드·크기·해시를
대조해 재사용하며, 차이가 있으면 덮어쓰지 않고 중단한다.

```bash
# pve-node-2 host root 셸. 한 번에 실행하고 어느 대조라도 실패하면 즉시 중단한다.
bash -euo pipefail <<'SH'
BACKUP_DIR=/root/pickle-synthetic-login-host-backup-20260928
SOURCE=/etc/nginx/conf.d/pickle-interim-tls.conf
BACKUP="$BACKUP_DIR/ct1200-inner-health.conf"
BASE_SHA=4a457f2d58fe16f8d11af4b6be6f3a4c8e8c73ac52b8d9010eb8152964c3346c
test ! -L "$BACKUP_DIR"
test "$(pct exec 1200 -- stat -c '%u:%g %a' "$SOURCE")" = '0:0 644'
test "$(pct exec 1200 -- sha256sum "$SOURCE" | awk '{print $1}')" = "$BASE_SHA"
EXPECTED_SIZE="$(pct exec 1200 -- stat -c %s "$SOURCE")"
if test -e "$BACKUP_DIR"; then
    test -d "$BACKUP_DIR"
    test -f "$BACKUP"
    test ! -L "$BACKUP"
else
    mkdir -m 0700 -- "$BACKUP_DIR"
    test ! -e "$BACKUP"
    test ! -L "$BACKUP"
    pct pull 1200 "$SOURCE" "$BACKUP"
    chown root:root "$BACKUP"
    chmod 0600 "$BACKUP"
fi
test "$(stat -c '%u:%g %a' "$BACKUP_DIR")" = '0:0 700'
test "$(stat -c '%u:%g %a' "$BACKUP")" = '0:0 600'
test "$(stat -c %h "$BACKUP")" = 1
test "$(stat -c %s "$BACKUP")" = "$EXPECTED_SIZE"
test "$(sha256sum "$BACKUP" | awk '{print $1}')" = "$BASE_SHA"
printf 'Protected CT1200 source verified: %s bytes, SHA-256 %s\n' "$EXPECTED_SIZE" "$BASE_SHA"
SH
```

SHA-256은 위의 health-only 값과 같아야 한다. 크기는 CT1200 원본의 `stat -c %s`
결과와 같아야 하며 pve-node-2 사본은 root:root 0600, 디렉터리는 root:root 0700이어야
한다. 차이가 있으면 적용하지 않는다. 이 host 사본은 CT1200 장애에서 원본 bytes를
확인할 근거이고 guest 내부 `recover`의 자동 입력은 아니다. 보존 기간이 끝나기
전에 필요한 검증 기록을 별도로 남긴다.

CT1201의 합성 DB·합성 계정·격리된 작업 실행 주체와 `198.18.1.20:80` 앱 경로를
먼저 확인한다. 실제 계정·원본 DB, 실제 LLM key, 외부 vendor 자격증명 또는 VM
작업이 연결돼 있으면 적용을 중단한다. `/index.html`과 `/api/v1/meta/status`가
CT1200에서 CT1201로 도달해야 하며 앱의 응답이 준비되기 전에는 공개 시험을 열지
않는다. 이 시험은 로그인 UI를 열 뿐이며 합성 계정 발급이나 데이터 준비를 하지 않는다.

CT1200에 root 소유 0700인 `/root/pickle-synthetic-login-ingress`를 만들고,
`config/candidate-synthetic-login.conf`를 그 안의
`candidate-synthetic-login.conf`로 root:root 0600으로,
`scripts/apply-candidate-synthetic-login.py`를 같은 디렉터리로 root:root 0700으로
전송한다. 전송 전에 소스 SHA-256을 검토하고 전송 후 다시 대조한다. 후보 설정의
SHA-256은 `81c3205d89587ed8538243a6996f32cbd2631bb446cbf81d15a4cec65d68b185`다.
도구는 정확한 guest hostname, 파일의 소유권·모드·해시, nginx 상태와 기존
health/차단 응답을 먼저 확인한다.

```bash
# CT1200 내부 root 셸. 활성 연결과 변경 창을 먼저 확인한다.
ss -Htan state established '( sport = :8443 or sport = :24443 )'
python3 -B /root/pickle-synthetic-login-ingress/apply-candidate-synthetic-login.py apply
```

도구는 원본을 root:0600으로 보관하고 후보를 원자적으로 교체한 후 `nginx -t`,
graceful reload, health·로그인·API status·차단 경로 probe와 설치 해시를 확인한다.
reload 직후 이전 worker가 새 연결을 수락하면 정상 후보 파일이 설치돼도 첫 `/login`
probe가 404일 수 있다. 적용 중 `/login` GET/HEAD와 `/api/v1/meta/status` GET의
404만 0.25초 간격으로 최대 10초 재시도한다. root·관리·VM·LLM 경로가 열리거나
출발지/Host 거부가 깨진 경우와 upstream 503 등 다른 불일치는 즉시 원복한다.
각 probe는 새 TLS 연결을 사용한다. 복구 때는 이전 trial worker의 `/login` 200만
재시도한다. 허용된 불일치가 deadline을 넘으면 마지막 경로·방법·기대/실제 코드와
당시 설치 파일 SHA-256을 포함한 timeout으로 실패한다. TLS 연결, 인증서, 소켓 등
probe 오류는 timeout으로 바꾸지 않고 즉시 실패한다.
적용 중 예외가 나면 원본으로 되돌려 재검증한다. 프로세스가 강제 종료되면
`apply`를 반복하지 말고 `recover`로 현재 해시를 확인하며 복구한다. 정상 복구
후 다시 시험할 때 `apply`는 보존된 guest 원본이 root:0600이고 원본 해시가
일치하는 경우에만 재사용한다. `recover`도
알 수 없는 파일이나 임시 파일을 임의로 제거하지 않는다. 그 경우 파일 상태를
수동으로 조사하고 기존 서비스를 보존한다.

## 시험 범위와 확인

기존 `/__ingress_probe`는 허용 출발지에서 200이며 root는 404다. 정확한
`/login`은 SPA index를, `/assets/`와 `/pnu-logo.png`는 정적 파일을 GET/HEAD로만
전달한다. `/api/v1/auth/login`, `/api/v1/auth/mfa`, `/api/v1/auth/refresh`, `/api/v1/auth/logout`는
POST만, `/api/v1/me`와 `/api/v1/meta/status`는 GET/HEAD만 전달한다. 다른 `/api/`,
`/admin`, `/llm-keys`, `/vms` 경로는 404다. 기존에 허용되지 않은 출발지는
403, 다른 Host는 421이다. 원본 IP는 인증된 PROXY 경로의 `$remote_addr`를
사용하고, `X-Real-IP`와 `X-Forwarded-For`를 그 값으로 **덮어쓴다**. 사용자
제공 `X-Forwarded-For`는 이어붙이지 않는다. `Forwarded`와
`X-Forwarded-Host`는 제거하고 Host는 `staging.example.com`, Proto는 `https`로
고정한다. CT1201의 현재 nginx는 `X-Real-IP`를 API에 전달하고
전달받은 `X-Forwarded-For` 뒤에 CT1200 hop을 추가한다.

적용 성공 후 허용된 **새 외부 연결**에서 실제 로그인·MFA·refresh·logout과
`/me`를 합성 계정으로 확인한다. 차단 출발지와 잘못된 Host, 다른 API·관리·VM
경로도 외부에서 확인한다. 도구의 loopback probe는 공개 SNI, 클라이언트의
실제 출발지와 CT1201의 합성 데이터 경계를 대신 증명하지 않는다. 기존 TLS
연결은 graceful reload 후에도 오래 남을 수 있으므로 새 연결 기준으로 판정한다.

## 복구

시험을 마치거나 응답이 예상과 다르면 CT1200에서 다음 명령을 실행한다.

```bash
python3 -B /root/pickle-synthetic-login-ingress/apply-candidate-synthetic-login.py recover
sha256sum /etc/nginx/conf.d/pickle-interim-tls.conf
```

복구 도구는 원본 백업 SHA와 현재 파일이 health-only 또는 이 시험 설정의 정확한
SHA인지 확인한 후 원본을 원자적으로 복원하고 `nginx -t`, graceful reload,
health 200·root 404·로그인 404와 접근 거부가 새 연결에서 최대 10초 안에
수렴하는지 재검증한다. 불일치가 계속되면 원본 파일 SHA를 유지한 채 마지막
실제 응답과 설치 SHA를 보고한다. 수렴한 뒤에는 외부 새 연결에서도 별도로
health-only 결과를 확인한다. 이 결과와 도구의 JSON은 **새 연결 기준**이다.
기존에 수락된 TLS/keepalive 세션은 nginx의 `worker_shutdown_timeout 3600s`에
따라 최대 1시간 이전 login 경로를 유지할 수 있다. 세션이나 conntrack을 강제로
종료하지 않는다. 복구용 원본은 원인 조사와 재검증이 끝날 때까지
보존한다. CT1200의 더 넓은 TLS/HTTP 경로를 해제할 필요가 생긴 경우, 이 시험
설정을 먼저 복구한 뒤 기존 경로의 복구 순서를 따른다.
