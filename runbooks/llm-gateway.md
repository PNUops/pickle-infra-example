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
