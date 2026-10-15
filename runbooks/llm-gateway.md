# LLM gateway 환경 배포 예시

현재 배포는 [명시 환경 artifact 절차](deploy-environments.md)를 사용합니다.
`deploy-llm-gateway.sh`는 check/apply, dev/prod, 실제 commit·archive·SHA를 받는
진입점입니다. 기본 호출과 CTID·RP·체크아웃 override는 빌드·대상 조회 전에 거부합니다.
예시 manifest의 주소·CT 번호·commit 자리표시자는 실제 검증한 배치 값으로 교체합니다.

## Artifact 준비와 검사

별도 build job에서 같은 commit의 daemon·keygen 바이너리를 함께 검증·빌드합니다.
Archive에는 정확히 `llm-gateway`와 `llm-keygen` 두 regular file만 담습니다.
환경 배포 도구가 root 보호 staging artifact와 SHA, 선택한 CT·unit·env·실행 경로를 검사합니다.

```bash
bash scripts/deploy-llm-gateway.sh check dev <commit> <staged-archive> <SHA>
```

대상 CT는 root 보호 환경 manifest가 선택합니다. 보존 source CT는 개발 target으로
선택할 수 없습니다. 진입점은 소스 빌드, unit 동기화, 기존 release 삭제를 수행하지 않습니다.

## 적용 순서와 복구

API·console 성공 receipt와 현재 Chat 호환 검증이 gateway 적용보다 먼저 필요합니다.
검증은 의도적으로 Chat 없는 정상 키도 유지하고 실제 실행 API artifact·DB snapshot을
대조합니다. 성공한 check만으로 적용 순서나 운영 승인 창을 대신할 수 없습니다.

`apply`는 환경 배포 도구의 같은 조건을 사용합니다. 운영 unit 전환과 restart는
별도 승인 창, 실제 독립 backup/recovery 검증이 필요합니다. Health 또는 실제 실행
artifact 검증이 실패하면 호환성이 확인된 daemon·keygen의 이전 symlink를 복구합니다.
Unit·env를 자동 교체하지 않으며 기존 release도 삭제하지 않습니다.

## 메모리 증설과 큰 응답 상한

게이트웨이 기본값(채팅 큰 응답 상한 128 MiB에 큰 본문 슬롯 3개, Key별 몫 없음, 패스스루 응답 상한
128 MiB에 큰 본문 슬롯 2개)은 8 GiB 컨테이너와 유닛의 `MemoryMax=7680M`, `MemoryHigh=6656M`,
`GOMEMLIMIT=5120MiB`를 전제로 합니다. 슬롯이 다 차도 `GOMEMLIMIT` 아래에 슬롯 밖 트래픽 몫 약
3.2 GiB가 남습니다(채팅 3 × 128 MiB × 3.2, 패스스루 2 × (25 MiB × 3 + 128 MiB × 1.75)를 뺀 값).
패스스루도 채팅처럼 2단으로 받아, 신고 길이 2 MiB 이하 요청과 8 MiB 이하 응답은 슬롯 없이
동시 요청 상한만 받고 그보다 큰 것만 패스스루 큰 본문 슬롯을 잡습니다.
`LLMGW_PASSTHROUGH_MAX_IN_FLIGHT`는 이름은 그대로이고 그 슬롯 수입니다. 측정 배수로 계산한 값이고, 16 GiB로 키울 때의 값은 게이트웨이 README의 표에
있습니다. 큰 응답은 클라이언트에 다 쓸 때까지 슬롯을 쥐고, 8 MiB 이하 응답과 2 MiB 이하 요청은
동시 요청 상한만 묶습니다(하나가 최대 약 40 MiB). 새 바이너리가 옛 사다리 위에서 돌지 않도록
컨테이너, 유닛, 바이너리 순으로 올립니다.

1. 노드 배치 예산의 메모리 예약(`reserve_memory_mb`)을 컨테이너 증가분만큼 올리고 관리형 VM
   예산을 같은 양만큼 줄여 재등록합니다. 복구용 코어 세트를 예약한 노드는 조정하지 않고, 그
   노드로 복구할 때 게이트웨이를 환경 파일과 유닛 값으로 낮춰 예약 안에 맞춥니다.
2. 지금 값을 root 전용 디렉터리에 남깁니다: `pct config <CTID>`, `systemctl cat <UNIT>`,
   `<ENV_FILE>`의 `LLMGW_` 상한 줄(비밀값 제외), 관리 주소 `/metrics`의 `maxInFlight`,
   `passthroughMaxInFlight`, `chatLargeBodySlots`.
3. `maxInFlight`가 1024이면 유지하고 진행합니다. 다르면 그 값을 기록하고 진행할지 판단합니다.
   1024 × 40 MiB는 어떤 컨테이너에도 들어가지 않는 최악이고, 사다리에 들어가도록 낮추면 증명되는
   값은 약 82 이하라 수업 한 반을 503으로 잃습니다. 그 최악은 공급자 과금 없이는 만들 수 없어 Key
   금액 한도가 상한입니다. 실측으로 보통 요청 1,024개가 248 MB였고 수업 피크는 약 3 GiB로
   추정합니다. 징후는 `MemoryHigh` 조절 로그와 `/metrics`의 `inFlight`입니다.
   `passthroughMaxInFlight`는 이제 패스스루 큰 본문 슬롯 수라 작은 임베딩 요청과 무관합니다. 이
   수는 2 MiB 초과 요청과 8 MiB 초과 응답(4K 그림, 큰 임베딩 배치)의 동시 수를 정하고, 기본 2에서
   하나 올릴 때마다 슬롯 밖 몫(약 3.2 GiB)에서 약 299 MiB가 빠집니다. 2보다 크면 그 이유와
   차감한 몫을 기록할 때만 두고, 이유 없이 남은 값이면 줄을 지웁니다. 채팅 슬롯 수나 두 응답
   상한을 기본값보다 올린 줄이 있으면 기본값으로 맞춘 뒤에 진행합니다.
4. 이 바이너리가 처음 올라가는 배포가 개발 환경이어도 순서는 같습니다. 개발 컨테이너를 다른
   서비스와 나눠 쓰면 그 몫을 더해 키우고, 아래 컨테이너와 유닛 drop-in을 먼저 적용한 뒤에
   배포합니다.
5. 컨테이너, 유닛 drop-in, 바이너리 순으로 올립니다.

```bash
pct set <CTID> --memory 8192                         # 즉시 적용
pct exec <CTID> -- install -d -m 0755 /etc/systemd/system/<UNIT>.d
pct exec <CTID> -- sh -c 'printf "%s\n" "[Service]" "MemoryMax=7680M" "MemoryHigh=6656M" "Environment=GOMEMLIMIT=5120MiB" > /etc/systemd/system/<UNIT>.d/50-memory.conf'
pct exec <CTID> -- systemctl daemon-reload           # 다음 재시작(배포)에서 적용
pct exec <CTID> -- systemctl show <UNIT> -p MemoryMax -p MemoryHigh -p Environment
```

되돌리기는 반대 순서입니다. 바이너리를 이전 release로 되돌리거나 환경 파일에 옛 바이너리의
기본값(`LLMGW_CHAT_LARGE_BODY_SLOTS=8`, 두 응답 상한 `33554432`,
`LLMGW_PASSTHROUGH_MAX_IN_FLIGHT=16`)을 적은 뒤 drop-in을 지우고 `daemon-reload`와 재시작을 하고, 컨테이너는 마지막에 줄입니다. 먼저 줄이면 이미 쓰는 메모리
아래로 한도가 내려가 OOM 킬이 날 수 있습니다. Key별 몫은 env로 되살릴 수 없으므로 필요하면
바이너리를 되돌립니다. 1에서 올린 예약도 같은 작업 단위에서 되돌립니다.
