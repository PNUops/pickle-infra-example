# 개발·운영 배포 경로 예시

이 문서에 날짜와 함께 보존한 운영 반영과 되돌림, 준비 결과는 당시 기록이에요.
현재 도구의 실행 조건과 구분해 읽어요.

예시 CT 번호와 RFC 2544 주소, example.com 이름과 commit 자리표시자는 실제 환경 값으로 교체해야 해요.

배포는 사전에 빌드한 서비스 commit의 artifact를 받아요. `develop`은
example-dev-host의 새 개발 CT901, 릴리스된 `main`은 example-prod-host의 운영 CT911/203을 대상으로 해요.
기존 example-dev-host CT904/103 원본 서비스와 DB hold는 해제하지 않아요. 공개 CT900 중계와
CT903 SSH transit도 이 절차에서 바꾸지 않아요.

## 대상 준비

두 호스트의 root 소유 `/etc/pickle/deploy-targets/{dev,prod}.json`은 이 레포의
`config/deploy-target-{dev,prod}.json`에서 각각 설치해요. 실제 CTID와 hostname,
systemd unit의 effective `ExecStart`와 `EnvironmentFiles`, API profile과 DB URL,
nginx root를 읽어 설정 파일과 일치하는지 확인해요. 자격증명 값은 출력하지 않아요.
`allowed_commits` 세 SHA는 함께 검증한 API와 console, gateway 조합이에요. 조합이
바뀌면 세 값을 함께 재검토한 뒤 host 설정을 갱신해요. 지금 개발 gateway 값은
Chat 권한 집행 전판이므로 권한 집행판 검증 후 다시 고정해야 해요. 이 값만으로
최종 호환 검증이 완료됐다고 판단하지 않아요.

개발 API unit의 effective `ExecStart`는
`/usr/local/libexec/pickle-api-start.py /opt/pickle/api/current-dev.jar dev`예요.
이 레포의 `scripts/pickle-api-start.py`를 root 소유 0755로 설치하고 설정의
`startup_wrapper_sha256`와 대조해요. Wrapper는 release JAR 자체와 active link의 상위 디렉터리 전체가 root 소유이고,
다른 사용자 쓰기를 허용하지 않으며 symlink가 아닌지 확인해요. JAR를 FD9로 열어 유지하고 `java -jar /proc/self/fd/9`로 실행해요.
Spring 설정은 JAR의 `classpath:/application.yml`만 읽고, 검토된 `PICKLE_*` 변수만
전달하므로 외부 config와 Spring/JVM, loader override가 실행 설정을 바꾸지 못해요.
개발은 `-Xmx1g`와 `--server.address=127.0.0.1`, 운영은 기존 `-Xmx2g`와
`--server.address=0.0.0.0`과 JobRunr server true/dashboard false, network-policy true,
vm-firewall true, DNS google의 명시 CLI 선택을 보존해요. `PICKLE_DEV_*` inventory와 JobRunr, 실연동
opt-in, 기대 DB identity 변수도 whitelist에 포함해요. 원격 개발 DB는
`PICKLE_DEV_DB_HOST=198.18.0.60`, `NAME=pickle_dev`, `USER=pickle_dev_api`,
`PORT=5432`이며 실제 `PICKLE_DB_USER`도 `pickle_dev_api`예요. 초기 개발 VMID
start는 `PICKLE_DEV_VMID_START=900000`이에요. 초기 mock의 root domain은
`.test`/`.invalid`를 사용하며 공개 개발 console URL과 구분해요.
새 서비스 설정 변수를 쓰려면 wrapper allowlist와 hash도 같은 배포 조합에서 갱신해요.
Gateway unit은
`/opt/pickle/llm-gateway/bin/current-dev`를 실행해야 해요. 대응 `llm-keygen-dev`도
관리형 symlink예요. 개발 DB는 별도 CT105의 `pickle_dev`이고 개발 API의 DB URL은
`198.18.0.60`의 전용 DB를 `verify-full`과 개발 CA로 가리켜야 해요. 개발 nginx root는
`/var/www/pickle-console-dev`예요. 개발 환경의 mock 메일과 외부 연동,
Proxmox/IPAM/LLM 사용량 격리는 별도로 검증한 뒤 배포를 열어야 해요. 개발
`check`는 단일 env 파일, DNS noop, 비어 있는 Proxmox token, loopback mock
proxy/terminal/OpenRouter와 gateway control/upstream 주소를 비밀값 출력 없이 검사해요.
개발 unit의 `Environment`, `PassEnvironment`, `UnsetEnvironment`가 비어 있지 않거나
manager가 application 변수를 주입하면 거부해요. 선택 env 파일은 root 소유이며
다른 사용자 쓰기와 symlink를 허용하지 않아요. 개발 API env 이름은 명시 allowlist로
제한하고 Spring/JVM/loader generic 변수는 거부해요. Loopback URL은 prefix만
비교하지 않고 scheme과 실제 hostname, port, userinfo 부재까지 검사해요.
Console artifact에 운영 주소가 포함되면 배포를 거부해요. `pickle-dev.example.com`의
공개 TLS와 CT900 경로는 별도 승인 전까지 확인된 접속 경로로 취급하지 않아요.

현재 운영 API unit은 고정 `production-example/pickle-api.jar`를, gateway unit은 drop-in
`40-release-example.conf`로 고정 `llm-gateway-example`을 실행합니다(2026-10-29 API·console·게이트웨이 수동
배포, 게이트웨이 env에 `LLMGW_UPSTREAM_OPENROUTER_REASONING=effort` 한 줄 추가). console은 2026-10-30에 네 번 더
배포했고 2026-11-11에 자체 서빙 가이드 hotfix를 배포했습니다. 게이트웨이는 2026-11-09에 새 고정 바이너리로 바꿨고, drop-in
파일 이름은 그대로 두고 `ExecStart`와 주석 한 줄만 바꿨습니다. 2026-11-12에 아래 「2026-11-12 릴리스」로 세 서비스를 다시
배포했습니다.

### 2026-11-12 릴리스

배치(batches)와 동영상 생성(videos) 기능 권한, 사용자 알림 설정을 담은 릴리스입니다. 순서는 게이트웨이 → API → console입니다.
console은 새 API 경로(`/api/v1/me/notification-preferences`)를 부르고, 이전 API는 batches와 videos 권한 승인을 거부하기 때문입니다.
공개 차단(fence)은 걸지 않았습니다.

- 게이트웨이: `main` 병합 commit을 go1.27.3 linux/amd64 `CGO_ENABLED=0 -trimpath`(ldflags 없음)로 깨끗한 체크아웃에서
  빌드했습니다(`vcs.modified=false`). 진행 중 요청 0에서 재시작했고 실행 파일 SHA, `/healthz`, 카탈로그 generation, env SHA 불변을
  확인했습니다. env는 바꾸지 않았습니다.
- API: API 명세 0.115.0, Flyway V146(스키마만). JobRunr 진행 작업, 웹 터미널과 SSH 연결, 게이트웨이 진행 요청이 모두 0인 순간에
  멈추고, 수동 백업이 VERIFIED된 뒤 unit의 JAR 경로만 바꿔 기동했습니다(정지 약 30초). 사용량 알림 발송은
  `PICKLE_LLM_USAGE_ALERTS_ENABLED`가 env에 없으므로 꺼져 있습니다.
- console: 기본 빌드를 직전 웹루트를 `cp -a`로 보존한 뒤 그 위에 복사했습니다. `._*` 파일이 없는지 확인했습니다.
- 실호출 검수(시험 키 둘): 유료 Chat과 자체 서빙 Chat, Anthropic 메시지 배치 제출, 가장 싼 영상 모델의 짧은 영상 생성과 내려받기,
  권한 없는 기능의 403 `endpoint_not_allowed`, 금액 한도 소진 뒤 429 `credit_exhausted`, 폐기 뒤 401 `api_key_revoked`를
  확인했습니다. 비동기 객체 기록은 접수 시 OWNED, 업스트림 거절 시 RELEASED였습니다.
- 현장 사본: API unit, console 웹 루트, 게이트웨이 drop-in, 백업 reader 설정의 `.before-<날짜>`.

### 2026-11-09 게이트웨이 릴리스

같은 날 공개 자체 서빙 모델의 카탈로그 행을 외부 API에서 자체 서빙 업스트림으로 되돌린 뒤, 게이트웨이 컨테이너를 한 번
재시작해 새 바이너리와 env를 함께 올렸습니다(진행 중 요청 0).

- 바이너리: llm-gateway `main` 병합 commit을 go1.27.3 linux/amd64 `CGO_ENABLED=0 -trimpath -ldflags='-s -w'`로 깨끗한
  체크아웃에서 빌드했습니다(`vcs.modified=false`). 새 고정 경로에 root 0755로 두고 실행 파일 SHA를 빌드 SHA와 대조했습니다.
- env 차이는 정확히 -1/+5입니다. `LLMGW_UPSTREAM_OPENROUTER_API_KEY` 줄(사업 계정 런타임 키)을 지웠고, OpenRouter의
  `_BASE_URL`과 `_REASONING=effort`는 유료 경로가 쓰므로 남겼습니다. 더한 줄은 `LLMGW_UPSTREAM_DGX_MESSAGES=open`,
  `_MESSAGES_MAX_INPUT_TOKENS=65536`, `_MESSAGES_MAX_CONCURRENT=4`, `_MESSAGES_QUEUE_WAIT=60s`,
  `_MESSAGES_CHAT_INPUT_TOKENS=32768`입니다. `LLMGW_UPSTREAM_DGX_REASONING` 줄은 없으므로 자체 서빙 추론은 닫힌 상태이고,
  바이너리에 들어 있는 예산 모드는 켜지 않았습니다. env 정본은 운영자 볼트이며 같은 날 같은 SHA로 갱신했습니다.
- 재시작 뒤 확인: 실행 파일 SHA, 프로세스 environ의 OpenRouter API 키 0개와 `DGX_MESSAGES=open` 1개, `/healthz` generation,
  `/metrics`의 Messages 슬롯 한도 4, journal ERROR 0, API의 마지막 연락과 감시 키의 자체 서빙 응답.
- 현장 사본: env와 drop-in의 `.before-<날짜>`. 카탈로그 행 사본과 적용 스크립트 사본은 운영 호스트의 백업 폴더에 있습니다.
- 게이트웨이 되돌림(2026-11-12 게이트웨이 되돌림 뒤에만): 이전 바이너리는 새 env로 기동하지 못합니다. 바이너리만 바꾸지 말고 두 `.before-<날짜>`를 원래 이름으로
  함께 복사한 뒤 `systemctl daemon-reload`, restart 합니다. 실행 파일과 env SHA가 배포 전 기록과 같은지 확인하고 볼트 env도
  이전 판으로 맞춥니다.
- 카탈로그를 외부 API로 되돌릴 때: 지금 env에는 OpenRouter API 키 줄이 없으므로 외부 API 행을 먼저 적용하면 해당 모델 요청이
  모두 업스트림 인증 실패가 됩니다. env에 `LLMGW_UPSTREAM_OPENROUTER_API_KEY` 줄을 복원하고 게이트웨이를 재시작한 **뒤에**
  외부 API 행을 다시 적용합니다. 그 런타임 키가 벤더에서 꺼졌다면 새 키가 먼저 필요합니다.
- 실행하면서 확인한 명령 차이: 자체 서빙 vLLM은 localhost가 아니라 서빙 장비의 캠퍼스 주소에 바인드하며 `/health`는 키 없이
  200, `/v1/models`는 서빙 API 키가 필요합니다. 운영 게이트웨이 컨테이너에는 `curl`이 없어 관리 주소를 컨테이너 안에서
  `wget -qO-`로 읽습니다. 사용량 이벤트를 시간으로 거를 때 `llm_usage_events`에는 `created_at`이 없으므로 `requested_at`을 씁니다.

CLI 연결 안내 후속은 2026-10-30 09:45 KST에 태그 `2026.10.30c`로 운영 반영했습니다. 빌드 artifact는 83파일이고
이미 열린 탭의 지연 로딩을 위한 기존 assets 네 개를 별도 SHA 검산 뒤 보존했으므로 당시 웹루트 manifest는 87파일이었습니다.
새 빌드 목록과 이전 판에서 보존한 목록의 출처를 구분하고 소스와 릴리스 CI 및 두 목록의 전체 검산 뒤 정적 웹루트를
원자 교체했습니다. 교체 후 파일 검산도 통과했고 공개 87파일 SHA와 영상 Range 206, 자막 `text/vtt` 및 가이드 화면도
검수했습니다.
API, DB, 게이트웨이와 nginx 설정은 이 후속에서 바꾸지 않았습니다.

영상 안내 정리 후속은 2026-10-30 11:25 KST에 태그 `2026.10.30d`로 운영 반영했습니다. 당시 빌드 artifact는 83파일이며,
직전 웹루트의 `assets/` 중 새 빌드에 없는 11파일(9,437,568바이트)을 보존하므로 당시 웹루트 manifest는 94파일이었습니다.
두 판에 공통인 assets의 SHA가 같고 보존 목록은 기존 assets와 새 빌드 경로의 차집합과 일치하는지 검산했습니다. 이전 영상과
자막도 열린 탭이 참조할 수 있으므로 보존했습니다. 새 빌드 83파일과 이전 판에서 보존한 11파일의 출처를 구분하고 각 파일의
경로, 크기와 SHA를 검산한 뒤 정적 웹루트를 원자 교체했습니다. 교체 후 94파일 검산도 통과했습니다.
공개 94파일의 SHA와 이전 assets 11개의 HTTP 200, 영상 Range 206과 자막 `text/vtt`, 실제 브라우저 플레이어의
새 영상과 포스터 및 한국어 자막 반영도 검수했습니다. 기존 촬영 원본을 바꾸지
않고 오프라인으로 재편집한 정적 자료 갱신이며, 새 키 발급이나 LLM 추론 요청, 메일 발송은 하지 않았습니다.
API, DB, 게이트웨이와 nginx 설정도 바꾸지 않았습니다.

자체 서빙 가이드 hotfix는 2026-11-11에 태그 `2026.11.11`로 운영 반영했습니다. hotfix 브랜치를 `main`에 바로 병합한 commit에서
빌드했고 `develop` 역병합은 아직 하지 않았습니다. 가이드의 컨텍스트 길이, 자체 서빙 모델의 Messages와 Claude Code 연결
(`CLAUDE_CODE_MAX_CONTEXT_TOKENS=65536`, `ENABLE_TOOL_SEARCH=false`), 자체 서빙 제약, `reasoning_effort`가 유료 모델 전용이라는
설명을 실제 게이트웨이 설정에 맞췄습니다. 빌드는 VITE 변수 없는 기본 `npm ci && npm run build`입니다. 직전 운영 소스를 같은
방식으로 빌드하면 운영 83파일이 바이트 단위로 같았으므로 기본 빌드가 곧 운영 빌드입니다. 새 빌드는 83파일 중 8파일이 달라졌습니다.
이번에는 원자 교체가 아니라 직전 웹루트를 `cp -a`로 보존한 뒤 새 빌드를 그 위에 복사했고, 기존 assets를 지우지 않아 `assets/`는
96파일, 웹루트 전체는 약 102파일입니다. 공개 index SHA와 새 chunk의 200, 직전 index chunk의 200을 확인했습니다. API, DB, 게이트웨이와 nginx 설정은 바꾸지
않았습니다.

추론 예산 안내 hotfix는 2026-11-12에 태그 `2026.11.12`로 운영 반영했습니다. 같은 기본 빌드로 만들었고 83파일 중 8파일이
달라졌습니다. 게이트웨이는 같은 날부터 자체 서빙 `reasoning_effort`를 생각 토큰 예산(low 1,024, medium 4,096, high 8,192)으로
받습니다. 직전 웹루트는 `<웹 루트>.before-<날짜>-reasoning-guide`에 보존했습니다. 11-11 반영 때 macOS `tar`가 붙인 메타데이터
파일(`._*`)이 웹루트에 함께 들어갔다가 지워졌습니다. 개발 머신에서 archive를 만들 때는 `COPYFILE_DISABLE=1 tar --no-xattrs`를
쓰고, 풀어 놓은 트리에 `._*`가 없는지 확인한 뒤 복사합니다. 같은 날 그 절의 문장을 해요체로 바꾼 후속 hotfix(태그
`2026.11.12b`)를 같은 방식으로 반영했고 직전 웹루트는 `<웹 루트>.before-<날짜>-reasoning-guide-tone`에 보존했습니다.

이전 판의 고정 경로도 남아 있어요. 2026-10-29에 Flyway V143을 적용했으므로 이전 API JAR 단독 재기동은 금지이며,
데이터까지 되돌리려면 V142 전 복구점에서 새 DB를 복원하는 별도 운영자 판단이 필요해요. 운영 API의 준비 대상 명령은
`/usr/local/libexec/pickle-api-start.py /opt/pickle/api/production-current.jar prod`이며,
gateway는 관리 symlink `current-production`과 기존 명령 경로 `llm-keygen`을
선택해야 해요. 운영 `apply`는 고정 artifact unit을 허용하지 않아요. 관리형 unit 준비 외에도
artifact에 결합된 root 보호 승인 창과 실제 PBS 독립 파일 복원 검증을 모두 요구해요.
운영 unit은 관리형 경로로 전환하지 않은 고정 경로 상태예요. 운영 호스트에는 아직 이 도구, `/etc/pickle/deploy-targets/prod.json`,
승인과 호환 기록 경로가 설치되지 않았습니다(2026-10-30 확인). 설치하면 `check`는 현재 고정 ExecStart와 기록된
artifact SHA를 읽기 전용으로 대조할 수 있어요. 서비스 재시작이나 공개 중계 변경이 필요하면 대상과 영향, 복구 방법, 작업 창을 정해 별도로 승인받아요.

### 2026-10-29 릴리스 되돌림 자료

수동 배포가 남긴 원본 사본과 되돌리는 순서입니다. 파일은 기록이 끝날 때까지 지우지 않습니다.

- 정본 사본: 운영 호스트 `/pickle/backup/release-<날짜>/`(root 0700, 파일 0600). 앱 컨테이너 nginx 설정과
  `pickle-api.service`, 게이트웨이 컨테이너 env와 drop-in 네 개의 배포 전 원본이며, 파일마다 SHA-256을 대조했습니다.
  env의 정본은 비밀 볼트의 게이트웨이 env 사본입니다.
- 현장 사본: 앱 컨테이너 nginx 설정과 console 웹 루트, 게이트웨이 컨테이너 env와 drop-in의 `.before-<날짜>` 파일.
- 공개 차단(fence): 앱 컨테이너 `/etc/nginx/pickle-release-fence.conf.staged`. 루프백이 아닌 요청을 503으로 돌리는
  설정입니다. 다시 걸기 전에 지금 활성 파일을 `.before-<날짜>`로 남기고, staged 파일을 nginx 설정에 복사한 뒤
  `nginx -t`와 reload를 합니다. 풀 때는 그때 남긴 `.before-<날짜>`를 복사하고 SHA가 걸기 전과 같은지 확인합니다.
  평시 파일은 생성기 `scripts/lib/isolated_core.py`의 출력이며, 자막 MIME location이 없는 이전 판은 fence 복귀 기준으로
  쓰지 않습니다.
- 자막 MIME: 앱 컨테이너 nginx에 `/assets/` 아래 `.vtt`만 `text/vtt; charset=utf-8`로 내보내는 location을 더했습니다.
  되돌릴 때는 `.before-<날짜>-vtt`를 복사하고 `nginx -t`, reload 뒤 SHA를 대조합니다.
- 게이트웨이 되돌림: 2026-11-09 릴리스가 적용된 상태라면 그 되돌림을 먼저 하고 볼트 env도 그 판에 맞춘 뒤에만 진행합니다.
  두 `.before-<날짜>`를 원래 이름으로 복사하고 `systemctl daemon-reload`, restart. 실행 바이너리가
  이전 판이고 env에 `_REASONING` 줄이 없는지 확인합니다. 이전 게이트웨이는 추론 강도(`reasoning_effort`)를 업스트림에
  맞춰 바꾸지 않으므로, 되돌리면 사용 가이드의 추론 강도 설명이 사실과 달라집니다. 가이드 문구 수정이나 공지를 함께 정합니다.
- API 되돌림: V143을 적용했으므로 이전 JAR 단독 기동은 금지입니다. 되돌리려면 V142 전 복구점에서 새 DB를 복원해
  전환하는 별도 운영자 판단이 필요하고, 그때만 이전 JAR와 이전 console을 함께 되돌립니다.
- 2026-11-12 릴리스 되돌림은 배포의 역순(console → API → 게이트웨이)입니다. API는 fix-forward가 원칙입니다.
  - console: `<웹 루트>.before-<날짜>-release`와 현재 index를 배포 기록의 SHA와 대조하고 `mv --exchange -T`로 맞바꿉니다.
  - API: V146은 테이블 둘을 더하고 메일 발송 기록의 결과 CHECK에 SKIPPED를 더할 뿐이라 이전 JAR도 기동합니다. 그래도 먼저
    고쳐서 다시 배포하는 쪽을 택하고, 꼭 되돌려야 하면 console을 먼저 되돌린 뒤 `pickle-api.service.before-<날짜>`를 원래 이름으로
    복사하고 `systemctl daemon-reload`, restart 합니다. 이미 승인된 batches와 videos 권한은 DB에 남습니다. V146은 DB에 남으므로
    백업 reader의 `expected_schema_version` 146도 그대로 둡니다.
  - 게이트웨이: drop-in의 `.before-<날짜>`를 원래 이름으로 복사하고 `systemctl daemon-reload`, restart 합니다. 실행 파일 SHA가
    배포 전 기록과 같은지 확인합니다. env는 그대로입니다. 되돌린 게이트웨이는 batches와 videos 경로를 열지 않으므로 그 권한을
    받은 키의 호출이 거절됩니다.
- console 되돌림(추론 안내 문체 hotfix, 위 2026-11-12 console 되돌림 뒤에만): `<웹 루트>.before-<날짜>-reasoning-guide-tone`과 현재 index를 배포 기록의 SHA와
  대조하고 `mv --exchange -T`로 맞바꿉니다. 문구 문체만 다르므로 게이트웨이 설정은 그대로 둡니다.
- console 되돌림(추론 예산 안내 hotfix, 위 문체 hotfix 되돌림 뒤에만): `<웹 루트>.before-<날짜>-reasoning-guide`와 현재 index를 배포 기록의 SHA와 대조하고
  `mv --exchange -T`로 맞바꿉니다. 되돌린 판은 자체 서빙 추론이 닫혀 있다고 설명하므로 게이트웨이 추론 설정도 함께 볼지 정합니다.
- console 되돌림(자체 서빙 가이드 hotfix, 위 추론 예산 안내 되돌림 뒤에만): `<웹 루트>.before-<날짜>-selfhost-guide`에 직전 운영판 94파일을 보존했습니다.
  되돌리기 전 현재와 보존본 index가 배포 기록의 SHA와 같은지, 같은 파일시스템의 실제 디렉터리인지 확인합니다.
  `mv --exchange -T <웹 루트>.before-<날짜>-selfhost-guide <웹 루트>`로 맞바꾸고 두 index가 반대로 놓였는지 대조합니다.
  교환 뒤 보존 경로에 남는 것은 깨끗한 hotfix 빌드가 아니라 직전 판 위에 새 빌드를 덮어쓴 트리이므로, 다시 hotfix 판으로
  돌아갈 때도 index SHA와 함께 파일 수를 대조합니다.
  되돌린 가이드는 지금 게이트웨이 설정과 다른 설명을 담고 있으므로 가이드 문구를 어떻게 할지 함께 정합니다. API나 DB,
  게이트웨이를 재시작하지 않습니다.
- console 되돌림(영상 안내 정리 후속): 자체 서빙 가이드 hotfix가 적용됐다면 위 절차로 먼저 되돌린 뒤에만 진행합니다.
  `<웹 루트>.before-<날짜>-video-clarity`에 직전 운영판 87파일을 보존했습니다.
  기존 복구 경로를 덮어쓰거나 symlink를 따라가지 않습니다. 되돌리기 전 현재와 보존본 index가 배포 기록의 교환 전 SHA와 같은지,
  같은 파일시스템의 실제 디렉터리인지 확인합니다. `mv --exchange -T <웹 루트>.before-<날짜>-video-clarity <웹 루트>`로 맞바꾸고 두 index가
  반대로 놓였는지 대조합니다. 되돌린 웹루트는 87파일이고 보존 경로에는 94파일이 남습니다. 다시 후속 판으로 전환할 때도
  SHA 위치를 먼저 확인한 뒤 같은 교환을 합니다. 배포 helper는 index와 전체 파일 검산 실패에 역교환하고,
  직전 판과 기존 복구 사본을 덮어쓰지 않아야 합니다. 공개 200, JS/CSS MIME, 영상 Range 206과 자막 `text/vtt`를
  확인하고 nginx의 자막 설정은 유지합니다. API나 DB, 게이트웨이를 재시작하지 않습니다.
  운영 호스트와 앱 컨테이너의 보호된 릴리스 자료 폴더(0700)에 배포 자료 세 개(0600)를 남겼습니다.
  이 자료와 직전 웹루트는 다른 릴리스 복구 사본과 함께 2026-11-12까지 보존하며 이후 정리는 별도로 판단합니다.
- console 되돌림(CLI 안내 후속): 현재 웹루트가 CLI 안내 후속 판인지 먼저 확인합니다. 영상 안내 정리 후속이
  적용됐다면 위 절차로 먼저 되돌립니다. 직전 웹루트는 `.before-<날짜>-cli-guides`에 보존했습니다. 그 경로가 이미 있거나
  symlink이면 덮어쓰지 않습니다. 되돌리기 전에 현재와 보존본 index가 배포 기록의 교환 전 SHA와 같은지, 같은 파일시스템의
  실제 디렉터리인지 먼저 확인합니다. `mv --exchange -T <웹 루트>.before-<날짜>-cli-guides <웹 루트>`로 맞바꾸고
  두 index가 반대로 놓였는지 대조합니다. 되돌린 웹루트는 83파일이고 보존 경로에는 87파일이 남습니다. 다시 후속 판으로
  전환할 때도 SHA 위치를 먼저 확인한 뒤 같은 교환을 합니다.
  공개 200, JS/CSS MIME, 영상 Range 206과 자막 `text/vtt`를 확인하고 nginx의 자막 설정은 유지합니다. API나 DB,
  게이트웨이는 재시작하지 않습니다.
  운영 호스트와 앱 컨테이너의 보호된 릴리스 자료 폴더(0700)에 artifact와 교체 helper(0600)를 남기고, 직전 웹루트와
  함께 배포 기록의 보존 기한까지 유지합니다. 이후 정리는 별도로 판단합니다.
- console 되돌림(그 이전 판): 대상의 직전 판인지 먼저 확인하고, CLI 안내 후속이 적용됐다면 먼저 되돌립니다. 같은 날 여러 번 배포했다면 판마다
  웹 루트의 `.before-<날짜>-<이름>`이 남습니다. 가장 최근 것부터
  하나씩 맞바꿉니다. 예를 들어 그 전 판은 `.before-<날짜>-followup`입니다.
  `mv --exchange -T <웹 루트>.before-<날짜>-followup <웹 루트>`로 두 디렉터리를 한 번에 맞바꾸고(coreutils 9.5 이상),
  두 `index.html`의 SHA를 배포 기록과 대조합니다. 같은 명령을 다시 실행하면 원래대로 돌아갑니다. 이전 릴리스 console은
  새 API와 짝이 맞지 않으므로 되돌림 대상이 아닙니다.
- 백업 reader 스키마: 백업 reader 호스트에 설치한 `deploy-backup-reader.json`의 `expected_schema_version`을
  레포 판과 함께 145로 바꿨고, 원본은 같은 폴더의 `.before-<날짜>`(root 0600)입니다. 되돌릴 때는
  `install -o root -g root -m 0600`으로 원본을 다시 설치하고 SHA를 대조하지만, 운영 DB가 V145이므로 이전 값은
  검증을 거부하게 됩니다. 이전 값으로 되돌리는 것은 정상 상태 복구가 아니라 검증 기준을 과거 스키마로 바꾸는 일이므로,
  운영 DB가 새 스키마인 동안은 새 값을 유지하고 DB 복원 같은 환경 판단과 함께 고릅니다.

## 제한된 실연동 개발 후보

초기 mock `config/deploy-target-dev.json`은 보존해요. 별도
`config/deploy-target-dev-real.json`이 승인된 실연동 후보이며 선택 host의 `dev.json`을
바꾸기 전에 기존 파일 bytes/소유/권한/hash와 되돌릴 경로를 별도 보호 폴더에 보존해요.
CT901 방화벽, template9101, `example-dev@pve!dev-api`의 정확한 ACL과 DB 범위가 실제로
준비된 뒤에만 이 manifest를 선택해요. Token 값은 파일에 담지 않으며 env의 값이
비어 있지 않은지만 확인해요.

실연동 후보는 전용 DB `198.18.0.60:5432/pickle_dev`와 `pickle_dev_api`, verify-full CA,
`PICKLE_DEV_REAL_INTEGRATIONS_ENABLED=true`, `PICKLE_DEV_JOBRUNR_ENABLED=true`,
PVE URL `https://example-dev-host:8006`과 `/etc/pickle/example-dev-proxmox-ca.crt`, node example-dev-host와 vmbr3,
local-lvm, template9101을 고정해요. 새 IPAM은 `198.18.10.0/24`, gateway `.1`,
예약 `.0–.99`와 `.200–.255`이며 `PICKLE_DEV_IP_POOL_RESERVED_RANGES`는
`from`/`to` 두 객체의 single-quoted JSON이에요. 실제 DB의 JSON `dev_environment_marker=true`, node의 pool 연결, node/pool/image metadata와
`vmid_seq MINVALUE 900000 MAXVALUE 900009 NO CYCLE`을 읽어 manifest와 비교해요.
실연동에도 `PICKLE_DEV_ROOT_DOMAIN=dev.pickle.test`, `PICKLE_DEV_ENABLE_GATEWAYS=false`,
DNS noop, SSH host `ssh.example.com`와 port22를 고정해요. 새 platform public key는
비어 있지 않아야 하며 private key 값은 manifest에 담지 않아요.
운영 DNS와 메일, OpenRouter 자동 polling은 여전히 열지 않아요. 공개 중계는 이 후보가
변경하지 않아요. 현재 개발 pin은 검증했던 0.99 snapshot을 보존해요. 승인된 0.100/V140 guard 재통합과 matching console의 검증과 packaging 이후 최종 source pin을 갱신해요. 이후의 별개 기능을 이 cutoff에 자동 포함하지 않아요.

## 운영 DB 사본을 사용하는 개발 환경

`config/deploy-target-dev-copy.json`은 `development_mode=production-copy`와
API의 `PICKLE_DEV_DATA_MODE=production-copy`가 함께 있어야 선택돼요. 배포 대상은
계속 dev이며 CT901/902와 `pickle_dev`, 전용 DB 로그인과 verify-full TLS를 사용해요.
API profile은 `isolated`로 고정하고 관리자 MFA를 CLI로 켜요. 이 profile은 개발 시더를
실행하지 않으며 메일 발송과 spool 기록을 거부해요. 운영 계정과 비밀번호 hash, MFA,
기관 권한, 멤버십, 이력, 설정, node/VM/relay endpoint를 사본 안에 그대로 보존해요.
운영 API보다 새 스키마의 기존 migration이 추가 필드와 Chat 권한을 변환할 수 있으므로
원본 dump의 row 검증과 migration 후 검증을 구분해요.

운영 writer는 계속 실행하고 PostgreSQL의 일관된 snapshot dump를 별도 개발 DB에
복원해요. 개인정보와 ciphertext, 암호화 key는 서버의 보호된 경로와 메모리에서만
처리하고 Mac과 Git, 로그에 저장하지 않아요. 기존 합성 DB는
`pickle_dev_synthetic_20261013`으로 보존하고 incoming DB를 검증하고 migration한 뒤
개발 API만 정지한 창에서 `pickle_dev`를 선택해요. 원본 운영 DB와 기존 개발 VM은
삭제하지 않아요. 일반 배포 명령은 이 DB 복제나 이름 변경을 수행하지 않아요.

원래 비밀번호는 DB의 hash로 유지해요. 같은 MFA와 암호화한 VM 자격을 읽으려면
보호 전송한 `PICKLE_CREDENTIALS_KEY`가 필요해요. LLM 본문과 OpenRouter 관리 자격은
각각 `PICKLE_LLM_BODY_READ_KEYS`와 `PICKLE_OPENROUTER_CREDENTIAL_READ_KEYS`가
관련 row를 실제로 열 수 있는지 내용 출력 없이 확인해요. 개발 JWT는 독립 key를
유지하므로 개발 주소에서 새로 로그인해요. Google identity 연결 row는 보존하지만
개발 Google OAuth client는 별도 승인과 구성 전까지 비활성이에요. 비밀번호 없는 Google
전용 계정의 로그인 시험은 그 구성을 마련한 뒤 진행해요.

JobRunr worker와 dashboard, direct LLM rollup/quota, OpenRouter polling, VM 제어와
외부 control bearer는 CLI에서 비활성으로 고정해요. DNS는 `none`, proxy와 terminal,
OpenRouter client는 loopback으로 고정하고 production-copy의 gateway 배포는 거부해요.
CT901의 `inet pickle_dev_app`는 input CT900 HTTP만, output 전용 CT902 DB5432만
허용하며 IPv4/IPv6 기본 정책은 DROP이에요. `check`/`apply`는 이 실제 ruleset,
선택 process의 isolated/MFA/worker/control 인자, V140/failed0, fresh worker0,
보존 합성 DB 존재를 대조해요. 복제한 JobRunr heartbeat는 2분 동안 과거 worker가
살아 있는 것처럼 보일 수 있으므로 row를 지우지 않고 시간이 지난 뒤 다시 확인해요.

복구할 때는 개발 API를 정지하고 복제 DB를 별도 이름으로 보존한 뒤 기존 합성 DB를
`pickle_dev`로 다시 선택해요. 보호 보관한 이전 개발 env와 wrapper, manifest, nft bytes를
같은 조합으로 되돌리고 DB TLS와 스키마, 실행 artifact, readiness를 확인한 후 개발 API만
시작해요. 복제 이후 시험으로 생긴 데이터도 보존하며 기존 백업과 key는 삭제하지 않아요.
운영 프로세스와 공개 중계는 이 절차에서 변경하지 않아요.

## 기존 파일명의 명시 환경 진입점

`deploy-api.sh`, `deploy-console.sh`, `deploy-llm-gateway.sh`는 현재 같은 보호 target과 artifact 도구를 호출해요.
인자가 없는 예전 호출이나 `CTID`, `EXPECTED_CT_HOSTNAME`, `API_DIR`, `CONSOLE_DIR`,
배포 Vite flag로 대상을 선택하는 호출은 빌드와 대상 조회, 재시작 전에 거부해요.
개발 manifest의 `forbidden_ctids`는 보존 source를 선택하지 못하도록 고정해요.
API target health는 local actuator readiness만 허용하며 aggregate health로 바꾸면 거부해요.

```bash
bash scripts/deploy-api.sh check dev <commit> <staged-JAR> <SHA>
bash scripts/deploy-console.sh check dev <commit> <staged-console-archive> <SHA>
bash scripts/deploy-llm-gateway.sh check dev <commit> <staged-daemon-keygen-archive> <SHA>
```

`apply`도 같은 인자 형식을 사용하며 운영 승인과 실제 백업, 관리형 unit 조건을 우회하지 않아요.
호스트 체크아웃의 빌드나 기존 release 삭제는 세 진입점에서 수행하지 않아요.

## 고정 artifact와 실행

수동 GitHub `deploy` workflow에 환경, 서비스, 40자리 commit SHA를 입력해요.
workflow는 서비스 checkout의 SHA와 `origin/develop` 또는 `origin/main` 포함 관계를
확인하고 해당 트리의 `scripts/verify.sh`를 통과한 후 깨끗이 빌드해요. API는
`clean package`로 오래된 migration 리소스를 제거해요. artifact SHA를 기록하고
별도 runner의 배포 job에 artifact만 전달해요. 배포 job은 선택한 GitHub
`dev` 또는 `prod` environment를 사용하고 배포 tool은 infra `main`에서 checkout해요.
개발 job에는 example-prod-host 운영 키와 주소, 신뢰 핀을 주입하지 않아요. 서비스 소스의 빌드와 시험은 root SSH
키를 받는 runner에서 실행되지 않아요. 전달된 SHA를 다시 확인하고 선택 호스트의
root 전용 `/root/pickle-deploy-staging/<commit>/<run-id>-<attempt>/`에 전송해요.
2026-10-12 확인한 공식 GitHub Actions 릴리스 기준으로 Java setup은
[`actions/setup-java` v6](https://github.com/actions/setup-java/releases/tag/v6.0.1)을
선택했습니다. checkout·Node·Go setup은 기존 검증 workflow와 같은 v7 계열이고
job 사이 산출물에는 [upload-artifact v7](https://github.com/actions/upload-artifact/releases/tag/v7.0.2)과
[download-artifact v8](https://github.com/marketplace/actions/download-a-build-artifact)을 씁니다.
호스트에서 `deploy-environment.py check`가 CT와 unit, env, profile, DB, artifact를
대조해요. 개발 workflow는 API와 console만 `apply`까지 실행해요. Gateway는
활성 키 권한과 snapshot generation을 확인한 뒤 별도 적용하며, 운영 workflow는
모든 대상을 stage/check에서 멈춰요. 운영 SSH는 기존 example-dev-host 신뢰 핀을 통한
ProxyJump와 example-prod-host 자체 신뢰 핀과 키를 사용해요. 이 경로의 도달성과 GitHub 비밀 설정은
실제 stage/check 전에 별도 확인해야 해요. 공개 중계 설정은 workflow가 바꾸지 않아요.

## CI VPN 배포 경로

이 절은 배포 소스에서 선택할 수 있는 기능이에요. CI Setup Key 발급, Cloud 정책과 GitHub 설정,
실제 연결과 배포는 별도 실행 승인 후 진행해요. SSH private key, 고정 receiver와
운영 stage/check 의미는 유지해요. `DEPLOY_TRANSPORT`와 키가 없으면 기존 native
경로를 사용해요. 키가 있거나 모드가 `netbird`이면 VPN 준비에 실패한 job은 중단하며
자동 native fallback을 하지 않아요. `DEPLOY_TRANSPORT=netbird`를 유지하면 키 누락도
실패해요. `native`는 운영자가 명시적으로 선택하는 원복 모드예요.

| GitHub 항목 | 종류 | 용도 |
|---|---|---|
|`NETBIRD_SETUP_KEY`|Secret|전용 CI peer 등록, build job에는 전달하지 않음|
|`EXAMPLE_DEV_VPN_HOST`|Variable|example-dev의 overlay IPv4, TCP 포트는 기존 `EXAMPLE_DEV_PORT` 유지|
|`EXAMPLE_PROD_VPN_HOST`|Variable|운영 ProxyJump 목적 example-prod의 overlay IPv4|
|`DEPLOY_TRANSPORT`|선택 Variable|`netbird`로 VPN 필수, `native`로 명시적 원복; 미설정은 키 존재 여부로 선택|

기존 개발 Secret은 `EXAMPLE_DEV_HOST`, `EXAMPLE_DEV_PORT`, `EXAMPLE_DEV_KNOWN_HOSTS`,
`DEV_DEPLOY_SSH_KEY`예요. 운영에는 같은 example-dev host/port/pin과 `EXAMPLE_HOP_SSH_KEY`,
`EXAMPLE_PROD_HOST`, `EXAMPLE_PROD_PORT`, `EXAMPLE_PROD_KNOWN_HOSTS`, `EXAMPLE_PROD_SSH_KEY`를 사용해요.
VPN 모드에서도 이 값을 덮어쓰지 않아요. SSH HostName만 VPN 변수로 바꾸고,
HostKeyAlias는 원래 호스트와 포트에서 계산하여 이미 신뢰한 공개키를 대조해요.
포트22는 bare host, 다른 포트는 `[host]:port`이며 새 키를 자동 수집하지 않아요.

CI용 Setup Key는 사용자 전달 키와 분리한 reusable 키로 발급하며 사용 횟수와 만료 기간을
제한하지 않아요. ephemeral=true, extra DNS labels=false를 사용해요. GitHub-hosted
runner는 job마다 새 peer를 등록하므로 키가 만료되면 이후 job이 등록하지 못해요.
[NetBird Setup Key 안내](https://docs.netbird.io/manage/peers/register-machines-using-setup-keys)에
따라 키 만료와 회수 후에도 이미 등록된 peer는 연결을 유지해요.
자동 배정은 전용 `ci-deploy-runners` 그룹 하나이며
`operators`나 사용자 그룹을 추가하지 않아요. Cloud 정책은 이 그룹에서 example-dev의
정확한 한 peer에 TCP22를 시작하는 단방향 규칙만 허용해요. CI peer끼리 통신과
서버발 역방향 새 연결을 허용하지 않아요. 현재 운영 ProxyJump 구조를 유지하므로
example-dev에서 example-prod의 정확한 VPN 주소에 TCP22로 연결하는 별도 정책도 먼저 필요해요.
이 정책에 CI나 사용자 peer를 섞지 않아요. 두 VPN IP 변수는 `100.64.0.0/10` 안의
정확한 IPv4만 받아요. Runner는 목적 example-dev 외의 peer가 배포되면 준비를 거부해요.

키는 GitHub Actions Secret에 보관하고 외부 전달이나 서버 등록에 재사용하지 않아요.
키가 노출되거나 CI 등록 용도가 끝나면 회수하여 새 등록을 차단해요. 이미 등록된 peer와
관련 정책은 별도로 확인해 필요한 대상을 제거해요. 무기한 키의 등록 권한을 전용 그룹과
한 SSH 대상과 포트로 제한하고, 키 회수와 등록된 peer 제거를 함께 관리해요.

`scripts/ci-netbird.py start`는 disposable GitHub-hosted Linux X64 runner만 지원하며
기존 NetBird 설치나 소유 불명의 임시 상태를 재사용하지 않아요. 공식 amd64 deb를
SHA256으로 확인한 뒤 설치하고 `NB_DISABLE_SSH_CONFIG=true` daemon 환경을 적용해요.
등록 키는 runner 임시 디렉터리의 0700/0600 파일로 전달하고 성공과 실패 모두 제거해요.
DNS, client/server routes, 내장 SSH와 remote jobs를 꺼요. Control-plane 연결과 대상 peer를
확인한 뒤 기존 SSH 포트에 최대 2초 TCP 연결로 lazy peer 연결을 유도해요. 인증 데이터를
전송하지 않는 이 준비는 전체 45초 대기 안에 끝나야 하며 SSH host-key와 사용자 인증은
이후 기존 전송 단계에서 검증해요. 항상 실행하는 stop 단계는 자신의 run/attempt/job과 일치하는
연결만 중단해요. 호스트 VM 폐기 후 Cloud의 ephemeral peer 정리는 10분 offline 기준이며
GitHub 종료 순간의 즉시 peer 삭제를 주장하지 않아요. 원문 상태와 인증값을 로그에 출력하지 않아요.

버전은2026-10-28 확인한 [공식 최신 안정 릴리스0.81.5](https://github.com/netbirdio/netbird/releases/tag/v0.81.5)입니다.
[공식 APT amd64 인덱스](https://pkgs.netbird.io/debian/dists/stable/main/binary-amd64/Packages.gz)의
`netbird_0.81.5_linux_amd64.deb` SHA256
`d4cdd97734055f810ff4b5af8eacac3b19a2a50e3ff594fb4353151725374800`을 핀했습니다.
[공식 Linux 안내](https://docs.netbird.io/get-started/install/linux)의 CLI 지원 범위를 사용하며
GUI와 다른 패키지는 설치하지 않습니다. 이 선택은 runner용이며 기존 노드 버전을 바꾸지 않습니다.

전환은 Cloud 그룹과 제한 정책, 전용 키를 승인해 준비한 뒤 GitHub의 선택 환경에 Secret과
VPN 변수를 추가하고 `DEPLOY_TRANSPORT=netbird`로 고정해요. 먼저 승인된 비배포 연결
점검에서 기존 SSH host-key와 계정의 인증을 확인해요. 운영 workflow는 기존처럼 stage/check에서
멈춰요. 성공을 확인한 뒤 native SSH 차단을 별도 적용해요. 원복은 진행 중인 job을 정리하고
검증된 native 경로를 먼저 복구한 다음 `DEPLOY_TRANSPORT=native`로 명시해요. Cloud 키 회수와
이미 등록된 peer 제거는 서로 다른 작업이며 별도로 확인해요.

### 수동 SSH 인증 점검

실제 배포 workflow로 VPN 경로를 시험하지 않아요. 별도 `.github/check-ssh.example.yml`은
`workflow_dispatch`로만 실행하며 build나 artifact 전송 없이 기존 `prod` 환경의 키와 pin을
사용해요. NetBird 연결을 필수로 선택하고 example-dev 및 기존 ProxyJump를 거친 example-prod에
원격 command, shell, subsystem과 PTY를 요청하지 않는 SSH 연결만 인증해요.

각 대상은 runner의 0700 임시 디렉터리 안에 고유 control socket을 만들고
`ssh -M -S <소유 socket> -fNT`로 인증한 뒤 `-O check`를 확인해요. 성공과 실패 모두
finally에서 같은 socket에 `-O exit`를 요청해요. [OpenSSH의 `-N`과 control 명령](https://github.com/openssh/openssh-portable/blob/master/ssh.1)은
원격 session을 열지 않으므로 서버 키에 설정된 forced command를 실행 요청으로 호출하지 않아요.
원문 SSH 출력과 IP, 인증값은 로그에 출력하지 않아요. 서버 파일 upload, 배포 receiver 호출,
서비스 restart, 생성 요청, DB와 VM 작업을 수행하지 않아요.

이 점검은 고정 pin을 가진 대상과 설정한 `root` 계정의 SSH 인증만 확인해요.
원격 hostname, 실제 Unix UID와 명령 실행 권한, 개발 receiver 권한이나 배포 성공은
검증하지 않아요. SSH 서버의 일반적인 인증 로그가 남을 수 있어요.

승인할 실행 단위는 전용 CI 키 발급, GitHub Secret과 VPN 변수 설정, 이 인증 점검 1회예요.
job timeout은 3분이며 runner 배정과 environment 승인 대기 시간은 포함하지 않아요.
항상 실행하는 종료 단계에서 해당 job이 등록한 NetBird 연결만 중단해요. 현재 소스 준비 단계이며
키 발급, GitHub 설정과 실제 workflow 실행 결과는 별도로 확인해야 해요.

`config/deploy-target-prod.json`은 실제 운영 실행본의 source SHA를 사용합니다. 2026-11-12부터
0.115/V146 판이고(API, console, 게이트웨이 모두 2026-11-12 릴리스), 산출물은
고정 artifact 수동 배포로 올렸습니다. pin은 빌드한 commit이자 릴리스 태그가 가리키는 commit이며, 게이트웨이의
`legacy_exec_path`와 `legacy_sha256`은 drop-in이 실행하는 고정 바이너리 경로와 그 SHA-256입니다. 운영 호스트에는
이 manifest를 읽는 배포 도구가 없어, console pin 갱신은 실행 중인 source를 기록할 뿐입니다. 같은 파일과 `config/deploy-backup-reader.json`의
`expected_schema_version`은 2026-11-12에 백업 reader 호스트의 설정과 함께 146으로 맞췄습니다(그 전 145는 2026-10-30). 별도
`config/deploy-target-prod-release-candidate.json`은 그 전의 미활성 0.100/V140 미집행 후보이며
이번 배포에 쓰지 않았습니다. 파일에 남은 0.99 API·console SHA는 과거 검증한 snapshot이며 최종 0.100 source로 확인된 값이 아닙니다. API와 console 새 source, 검증된 Chat 집행 gateway source와 세 artifact
SHA를 함께 검증하기 전에는 `release_candidate_ready=false`이며 apply를 거부해요.
Gateway placeholder와 미확인 digest를 제거하지 않고 READY로 바꿔도 거부해요.
운영 workflow는 `check`까지만 실행해요. `<SHA>`는 artifact SHA-256,
`<commit>`은 실제 서비스 commit이에요. `--backup-point` 문자열이나
`PICKLE_PRODUCTION_RELEASE_APPROVED`는 운영 적용을 허용하지 않아요.

`config/deploy-backup-reader.json`은 example-backup-host 독립 DB reader의 현재 선택 profile,
배치된 기존 native library SHA, 실제 DB instance/system ID와 현재 스키마 버전(2026-11-12부터 V146)을 고정한 후보예요.
백업 reader 호스트에 설치한 사본도 레포 판과 같아야 하므로, 이 파일이 바뀌면 호스트 사본을 같은 소유와 권한(root 0600)으로 다시 설치해요.
이 파일과 `verify-production-backup.py`를 별도 root 보호 경로에 stage하고, example-prod-host→example-backup-host
reader 및 example-backup-host→example-prod-host의 native PostgreSQL 읽기 actor를 각각 기존 신뢰 핀과 키의 새 전용
SSH 설정으로 연결해요. 설정의 `postgres_restore_command`는 기존 CT204의
`pg_restore --list`와 dump의 Flyway 데이터 출력만 사용해요. SQL을 실행하거나
DB를 만들지 않아요. `/root/pickle-release-backup-readback`은 root0700으로 준비해요.
현재 배치된 monitor/profile/library는 수정하지 않아요.

독립 verifier는 실제 PBS의 최신 complete data/proof pair를 조회하고 암호화 manifest와
receipt를 대조한 뒤 같은 point를 새 임시 폴더로 복원해요. `database.dump`와 `roles.sql`,
`manifest.json`의 실제 SHA, native PostgreSQL TOC와 dump에 들어 있는 Flyway COPY를
검사해요. 임시 plaintext는 성공과 실패 모두 제거해요. 최신 point는 600초 이내여야 하고,
root 임의 JSON이나 과거의 SQL 복원 기록만으로 이 확인을 대신하지 않아요. 호출자는
매번 새 challenge와 verifier SHA를 대조하고 실제 CT204의 system ID와 현재 스키마 버전, failed0도 읽어요.
이 경로는 독립 파일 복원과 TOC, Flyway 데이터 검증이며 전체 SQL clone/service RTO 시험이
아니에요. 기존 완료한 full restore는 보존해요. 운영 manifest의 `historical_full_sql_restore`는
이미 완료된 native SQL producer 경로와 SHA를 연결해요. 이 파일은 root0600으로
존재하는 것을 읽기 전용 확인했으며 이번에 SQL 복원을 반복하지 않았습니다. 과거 drill이
현재 point의 full SQL 복원을 의미하지 않으므로 다음 승인된 릴리스 실행안에서 필요한
전체 SQL 복원 범위를 따로 정해요. 실제 연결과 선택적 readback은 별도 실행 범위예요.

```bash
python3 scripts/deploy-environment.py check prod api <commit> \
  /root/pickle-deploy-staging/<commit>/<run-id>-<attempt>/artifact.jar <SHA>
# Independent native PBS file-recovery path, after its protected actor is wired.
python3 scripts/deploy-environment.py check prod api <commit> \
  /root/pickle-deploy-staging/<commit>/<run-id>-<attempt>/artifact.jar <SHA> --backup-check
```

향후 운영 `apply`에는 `/etc/pickle/deploy-approvals/prod.json`이 필요해요. operator가
별도 승인한 실제 작업 창을 `authorization_reference`로 연결하고 `environment=prod`,
`host`, `target`, `commit`, `artifact_sha256`, `approved_by=operator`, Unix timestamp
`approved_at`와 `expires_at`, 명시적으로 수용한 `accepted_recovery_scope`를 기록해요.
창은 최대 15분이고 현재 artifact와 복구 범위에 정확히 일치해야 해요. 파일 자체가 인간 승인을
대신하지는 않으며, 이 준비 작업은 그 파일을 작성하지 않아요. apply는 lock 안에서
fresh 실제 backup verifier를 다시 호출하고, 검증 후에도 승인 창이 유효한지 확인해요.
현재 fixed production unit은 이 조건과 별개로 apply를 거부해요.

이 릴리스의 V139는 이전 API가 신규 키 신청을 저장하지 못할 수 있으므로 개발과 운영
설정 모두 `api_rollback_policy`를 `fix-forward`로 고정해요. API 적용에서 정책이
없거나 CLI 지정이 manifest와 다르면 거부해요.

Gateway는 `artifact.tar.gz`에 `llm-gateway`와 `llm-keygen` 두 바이너리를 함께
넣어요. Console은 `index.html`을 포함한 `dist/` tarball을 받아요. 해당 대상의
명령에서도 `check`를 먼저 실행해요. API와 gateway는 versioned release를 남기고
관리형 symlink를 바꾸며 console은 이전 webroot를 별도 이름으로 보존해요.
API는 MainPID의 열린 JAR FD9, gateway는 `/proc/<MainPID>/exe`의 inode와 SHA를
선택 artifact와 대조하며 PID start time도 앞뒤로 확인해요. Readiness만 응답하는
구판 프로세스는 성공 receipt를 받지 못해요. Console archive의 uid/gid와 mode를
그대로 적용하지 않고 root 소유, directory 0755, file 0644로 정규화해요.
특수 mode와 중복 경로, link entry는 archive 검증에서 거부해요.
API readiness와 gateway health는 대상 내부,
운영 gateway는 CT200에서 198.19.0.40:8081로 검사해요. 실패 시 호환성이 확인된
API와 gateway는 이전 symlink로 복구하고 console은 이전 webroot로 복구해요.
`fix-forward`인 개발과 운영 API는 이전 JAR로 자동 복구하지 않고 작업 창의 수동 복구안을
따라요. 기존 백업과 원본 release는 지우지 않아요. 외부 공개 응답과 실제 기능은
별도 확인해요.

Chat 권한이 포함된 릴리스는 API와 console을 먼저 적용하고 snapshot 호환성을
검증한 다음 gateway를 적용해요. 성공 receipt의 manifest SHA와 실제 실행 API
artifact, console index SHA, V139 success 및 applied generation을 대조해요.
모든 ACTIVE 키에 `chat`을 요구하지 않아요. 신규 API에서 의도적으로 Chat 없이
발급한 정상 키도 유지해야 하기 때문이에요.

독립 검증 후 호스트 root 소유이고 다른 사용자는 쓸 수 없는
`/var/lib/pickle-deploy/<env>/chat-compatibility.json`을 작성해요. 이 결과는
migration 전 기존 키 baseline의 grant 보존, 현재 gateway snapshot과 DB grant
동일성, 의도적으로 Chat 없는 신규 키의 결과를 실제로 확인한 뒤에만 작성해요.
파일 자체가 실제 확인을 대신하지는 않아요. 필드는 `environment`, manifest 세 SHA의
`commits` 객체, receipt의 `api_sha256`와 `console_index_sha256`, 확인한 `generation`,
Unix timestamp `checked_at`, 실제 확인 후 true인 `existing_key_grants_preserved`와
`snapshot_grants_match_database`, `chat_free_keys_verified`예요. 결과는 15분 이내,
현재 commit과 artifact, generation이 모두 일치해야 적용을 허용해요. 파일이 없거나 결과가 미확인 또는 stale이면
거부해요. 현재 gateway manifest는 권한 집행 전판이므로, 집행판 commit을
검증하고 고정한 뒤 이 순서를 실행해요.

V139 이후 예전 API는 `chat`을 snapshot에서 빠뜨릴 수 있으므로 호환되는 복구가
필요할 때 gateway를 먼저 구판으로 돌려요. API migration은 jar rollback으로
되돌아가지 않아요.

`sshgw`, `proxy-agent`, `relay`는 이 workflow의 대상이 아니에요. 각자의 기존
수동 절차와 별도 영향 검토를 따라요.

## 개발 CI 키와 frozen receiver 준비

개발 호스트에 고정 receiver·tool·digest pin과 신규 제한 key row를 설치하고 호스트 내부 실제 check 및 명령·digest 거부를 검증했습니다. Key의 추가 custody와 GitHub secret 등록은 미완료이며 외부 CI 연결을 검증했다고 표현하지 않습니다.
`dev-deploy-receiver.py`는 root 보호 고정 설정과 설치한 `deploy-environment.py`의 SHA를
검사합니다. 새 key row는 `restrict,command="/usr/bin/python3 -I /opt/pickle/dev-deploy/receiver.py"`
만 허용하며 기존 운영 key와 authorized row는 그대로 보존합니다. PTY·port/agent/X11 forwarding·
user rc는 key의 restrict 조건으로 차단합니다. Shell·SCP·SFTP·임의 도구 upload는 받지 않습니다.

SSH 원격 명령은 정확히 `pickle-dev-deploy-v1`이에요. stdin 첫 줄의 strict JSON은
version1과 action check/apply, target, commit, sha256, size만 받아요. 중복/추가 key나 prod 환경,
잘못된 commit이나 size, trailing payload는 거부해요. 고정 CT와 DB, source 제외 목록이 선택
manifest와 같아야 해요. 수신은 120초, 압축 크기는 target별 상한, console/gateway의
expanded size와 entry 수도 제한해요. 같은 commit과 SHA 경로의 기존 artifact는 덮어쓰지
않고 digest를 대조해요. 전용 lock과 소유한 incoming temp만 정리해요. 설치된 trusted
도구만 최대 300초 실행하며 timeout에 전체 process group을 종료해요. Gateway Chat 순서
검증은 이 도구에 그대로 남아요.

준비 순서는 frozen code/config와 root0700 staging/lock 경로 확인 → 새 host-only key 생성 →
기존 authorized rows의 byte/hash/소유/권한 보존과 새 restricted row 단독 append → 제한 호출
negative test → GitHub `dev` environment 준비와 새 secret 등록이에요. 실제 적용은 독립
검토 후 수행해요. `prod` environment는 main deployment branch와 operator review를
요구하도록 설정해야 해요.

보존한 준비 기록:

> 이번 코드 준비는 기존 environment·secrets를 수정하지 않았습니다.

`prepare-dev-deploy-key.py generate`는 새 `/root/pickle-dev-deploy-key`가 이미 있으면
거부해요. Private key 원문은 example-dev-host에만 남기고 Mac과 Git, 로그로 가져오지 않아요.
`install-row`는 읽어 확인한 authorized-row SHA와 receiver SHA를 요구하며 기존 bytes를
`/pickle/backup/dev-deploy-key/`의 별도 폴더에 보존하고 새 row만 append해요. 이 backup은
보존 기한이 있는 복구 사본이며 private key의 유일한 보관 위치가 아니에요.

GitHub의 [environment public-key/secret API](https://docs.github.com/en/rest/actions/secrets#create-or-update-an-environment-secret)
metadata를 Mac에서 읽어 root 보호 public-key JSON으로만 example-dev-host에 전달해요. example-dev-host에서는
libsodium sealed box로
private key를 암호화하고 `key_id`와 `encrypted_value` JSON만 Mac으로 가져와요. Vendor PAT는
호스트로 복사하지 않아요. Mac은 ciphertext JSON을 API body로 등록하며 plaintext를 읽지 않아요.

현재 repository admin metadata는 true이나 environments 목록은 비어 있고 `dev` public-key는
404였습니다. 이는 신규 환경 준비 전 상태이며 secret write 성공을 뜻하지 않습니다. example-dev-host의
기존 libsodium도 없으므로 global package 설치 대신 signed Debian stable package를 protected
stage에 download/extract하고 library SHA를 고정하는 후보입니다.
[Debian trixie의 libsodium23](https://packages.debian.org/trixie/libsodium23)은 확인 시
1.0.18-1+deb13u1이며 실제 실행 직전 APT candidate·signed Release/package hash를 다시
확인합니다. `seal --libsodium-library <protected-so> --libsodium-sha256 <SHA>`는 이 pin과
root 보호 파일을 검증합니다. Dependency·환경 권한이 준비되지 않으면 키를 외부로 복사하여
우회하지 않고 등록 단계의 구체적 남은 결정을 operator에게 전달합니다.

## 운영 release·rollback 실행안

운영 baseline 파일을 별도 root 보호 staging으로 복사해 check한 결과는 운영 변경 승인이 아니에요. Console snapshot과 gateway archive의 SHA는 설치 파일 복사본의 SHA이며,
commit과의 연결은 보존된 baseline metadata예요. 독립 rebuild provenance로 표현하지 않아요.

API와 gateway의 현재 unit은 고정 artifact를 직접 실행해요. `check`는 manifest의 고정 경로와
SHA를 대조하여 허용하지만 `apply`는 managed artifact 경로를 선택하지 않는 unit을 거부해요.
Startup wrapper 설치와 unit 전환, restart는 운영 창에서 별도 승인한 실행안의 작업이에요.
현재 운영의 JVM 옵션과 환경 파일, gateway 설정을 이 전환에서 보존하고, 실제 process artifact를
API의 열린 JAR FD/inode와 gateway의 `/proc/PID/exe`로 다시 확인해야 해요.

실행 전에는 새 release source와 artifact digest를 확정하고, API/console과 호환되는 Chat
강제 gateway 구현을 먼저 병합하고 검증해요. 비활성 future manifest의 placeholder와 readiness
false를 임의로 해제하지 않아요. 승인 파일은 정확한 source/artifact와 작업 scope, 짧은 승인
창에 결합해요. 기존 고정 unit의 전환과 실제 apply는 이 승인에 명시해야 해요.

Backup reader의 보호된 SSH config와 private temporary root를 준비한 후 fresh paired point에서
실제 payload 3개를 selective restore하고 SHA, native TOC, 복구된 Flyway history를 검증해요.
이 검증은 full SQL clone restore가 아니에요. 과거 SQL drill receipt의 파일과 SHA를 재확인하고,
현재 point의 SQL clone gate가 필요한 release에서는 승인 실행안에 따로 넣어요. 현재 경로에
과거 receipt가 없으면 drill 통과라고 주장하지 않고 자료 위치를 확인할 때까지 gate를 유지해요.

API rollback은 manifest의 fix-forward 정책을 따라요. Migration을 적용한 DB에 이전 JAR를
자동 재활성화하지 않아요. 실패 시 approved rollback 계획의 DB 복구 point와 single-writer
중지, 호환 artifact 조합을 확인한 뒤 실행해요. Console/gateway도 호환성을 확인한 이전
artifact만 선택하며 Chat enforcement의 후퇴를 자동 허용하지 않아요. 실행 전후 PID와
artifact/env hash, schema, compatibility proof, readiness를 기록해요.

### Dev artifact pin 갱신

Dev manifest의 `approved_artifacts[target][source_commit]`는 승인된 artifact SHA-256을
고정해요. Receiver는 header의 digest와 optional size를 이 pin과 대조하고 deploy tool도
check/apply 진입 시 digest pin을 요구해요. 같은 commit label을 붙인 다른 파일은 거부해요.
Gateway의 검토된 archive pin이 없으면 check/apply 모두 거부해요.

Pin 갱신은 실제 배포할 artifact의 hash와 source provenance, 필요한 검증 결과를 검토한 뒤
호스트 root 보호 manifest를 수동 갱신하는 절차예요. CI rebuild가 다른 digest를 만들면
자동 허용하지 않아요. 실제 CI artifact를 확보해 검증하고 그 digest를 명시적으로 승인해야
해요. Commit label만으로 재현 가능한 build provenance를 증명했다고 표현하지 않아요.
Tool/receiver 코드가 변경되면 새 SHA의 독립 검토와 설치 config pin을 대조하며, 기존 SSH
authorized row bytes를 보존한 CAS 절차로 필요한 변경만 적용해요.

최종 갱신: 2026-11-08
