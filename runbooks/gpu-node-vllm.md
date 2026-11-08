# GPU 노드 vLLM 서빙 운영

자체 호스팅 모델을 vLLM으로 운영하는 절차예요. 공개 모델의 현재 연결 대상은 게이트웨이 설정에서 따로 확인해요. 서빙 장비는 통합 메모리 구조의
aarch64 GPU 노드예요(전용 VRAM이 없어 모델 크기 예산과 OS가 한 풀을 나눠 써요). 접근은 운영자 root 키를 사용해요(개발 머신 ssh config의 `Host gpu-node-root`, 키는 운영자
키 저장소). 이 문서의 어떤 것도 장비 자체의 crontab이나 체크아웃에서 돌지 않아요.

구성 요소와 각각의 원본 위치는 다음과 같아요.

| 구성 요소 | 원본 | 적용 수단 |
|---|---|---|
| systemd 유닛 `pickle-vllm.service`(이미지 핀, 모델, 플래그) | `hosts/gpu-node/systemd/` | `scripts/apply-gpu-node-vllm.sh` |
| env `/etc/pickle/vllm.env`(서빙 API 키) | 운영자 vault 의 서빙 env | 같은 스크립트 |
| 모델 가중치와 컨테이너 이미지 | Hugging Face, Docker Hub. `/var/lib/pickle-vllm/hf-cache`와 docker 이미지 저장소에 캐시돼요 | 최초 서비스 기동(또는 아래의 수동 사전 내려받기) |

서빙 엔드포인트는 `http://192.0.2.20:8000/v1`이에요(캠퍼스 대역 전용, 공인 인바운드
없음). OpenAI 호환이고 env 파일의 `VLLM_API_KEY` bearer 값으로 인증해요. LLM 게이트웨이가
이 엔드포인트를 대상으로 설정한 모델 요청을 보내요. 재시작하면 해당 자체 서빙 요청이 잠시 실패할 수 있어요.
게이트웨이의 업스트림 오류와 실제 응답 복구를 확인해요.

## 기동과 정지

```bash
ssh gpu-node-root systemctl start pickle-vllm    # 콜드 스타트는 모델 적재로 수 분. TimeoutStartSec=1800
ssh gpu-node-root systemctl stop pickle-vllm
ssh gpu-node-root journalctl -u pickle-vllm -f   # 적재 관찰. "Application startup complete"가 준비 완료
```

기동 후 첫 요청은 JIT 워밍업 때문에 느려요. 미리 워밍업해요.

```bash
curl -s -H "Authorization: Bearer $KEY" http://192.0.2.20:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<서빙 이름>","messages":[{"role":"user","content":"ping"}],"max_completion_tokens":8}'
```

유닛의 `ExecStartPre`는 매 기동 전에 page cache를 비워요. 의도한 동작이며 vLLM의 통합 메모리
우회책이에요(vLLM의 기동 시 메모리 검사가 회수 가능한 캐시를 사용 중으로 세는 문제,
vllm-project/vllm#35313). 재시작 후 장비의 파일 캐시가 차가운 것은 이 때문이에요.

## 헬스 체크

```bash
KEY=$(ssh gpu-node-root "grep ^VLLM_API_KEY= /etc/pickle/vllm.env | cut -d= -f2")
curl -s -H "Authorization: Bearer $KEY" http://192.0.2.20:8000/v1/models
```

인증 없는 요청은 401로 거절돼야 해요. bearer 없이 `/v1/models`에서 성공 응답을 받으면
서비스를 정지해요. 인증 설정이 실제로 적용됐는지 확인하고 설정을 다시 적용해요.

모델이나 양자화를 바꾼 뒤에는 엔드포인트를 믿기 전에 **첫 요청을 정답이 정해진 프롬프트로**
보내요(예: "What is 2+2?"의 응답에 "4"가 있어야 해요). 이 GPU 계열에서 FP4 GEMM 백엔드를
잘못 고르면 오류 없이 그럴듯한 오답을 낼 수 있으므로 알려진 정답으로 실제 출력을 확인해요.

## 모델과 플래그 변경, 롤백

모델과 서빙 플래그는 이 유닛에서 관리해요. 모델 id와 양자화, 메모리 비율, 컨텍스트 길이가 모두 거기 있어요.
무엇이든 바꾸려면 다음을 따라요.

1. 이 레포지토리의 `hosts/gpu-node/systemd/pickle-vllm.service`를 편집해요.
2. `scripts/apply-gpu-node-vllm.sh`를 실행해요(바뀐 것이 있을 때만 재시작해요).
3. 위의 헬스 체크로 확인한 뒤 유닛 변경을 커밋해요. 그 파일 하나의 git 이력이 곧 롤백
   경로예요. 커밋을 되돌리고 다시 적용하면 돼요.

새 모델은 첫 기동 때 영구 hf-cache로 내려받아요. 서비스 기동 안에서 내려받기 비용을 내지
않으려면 미리 받아 둬요.

```bash
ssh gpu-node-root docker run --rm --gpus all \
  -v /var/lib/pickle-vllm/hf-cache:/root/.cache/huggingface \
  --entrypoint hf vllm/vllm-openai:v0.27.1 download <org/model>
```

이전 가중치가 캐시에 남아 있으면 다시 내려받지 않고 롤백할 수 있어요. 정리는 명시적으로 진행해요.

## 장애 복구

- **적재 중 OOM 또는 할당 실패**: 통합 풀을 OS와 나눠 쓰므로 `free -h`를 먼저 확인해요. 조절 손잡이는 유닛의 `--gpu-memory-utilization`(또는
  `--max-model-len`)이고, 위의 변경 절차로 바꿔요.
- **서비스 플래핑(`Restart=on-failure` 루프)**: `journalctl -u pickle-vllm --since -30min`.
  잘못된 플래그나 모델 id 오타는 매번 같은 방식으로 실패해요. 서비스를 정지하고 유닛을
  고친 뒤 다시 적용해요.
- **엔진 기동 시 "Unknown SF transformation"**: 이 GPU의 FP8 MoE 경로는 유닛이 담고 있는
  `VLLM_USE_DEEP_GEMM=0` 설정이 필요해요. 관련 오류가 보이면 해당 env 줄과 실제 적용값을 확인하고
  누락된 값을 복구해요.
- **프로세스는 살아 있는데 서빙이 멈춘 경우**: `systemctl restart pickle-vllm`으로 재시작해요.
  해당 자체 서빙 요청이 잠시 실패할 수 있으므로 실제 응답이 복구됐는지 확인해요.

## 장비 재부팅

**BMC가 없는 GPU 노드는 원격 콘솔이 없어요.** 재부팅 후 돌아오지 않으면 물리적 접근이
필요해요. 반드시 살아 있는 ssh 세션에서만 재부팅해요.

```bash
ssh gpu-node-root systemctl reboot
```

유닛의 `WantedBy=multi-user.target`과 실제 enable 상태, docker의 enable 상태를 확인해요.
활성화한 서비스는 부팅 때 시작되며 모델 준비 완료는 실제 응답으로 확인해요.
모델 적재 때문에 수 분이 걸리며, 그 뒤 헬스 체크를 실행해요.

**실측**. 의도적인 `systemctl reboot`이고 장비로서는 몇 주 만의 첫 재부팅이었다
(`journalctl --list-boots`로 확인).

| 지점 | 재부팅 명령으로부터 |
|---|---|
| ssh가 다시 응답 | **37초** |
| `pickle-vllm`이 `active` 보고 | 약 37초, 개입 없음 |
| 엔드포인트가 정답 프롬프트에 응답 | **약 6분**(360초) |

**`active`는 준비 완료가 아니에요.** 유닛이 `Type=exec`이라 systemd는 `docker run`이 exec된
순간 기동됐다고 판단하고 모델을 기다리지 않아요. 유일한 준비 완료 신호는 엔드포인트의
실제 응답이에요. 위 실측에서 `is-active`만 보고 복구됐다고 판단하면 이후에도 약 5분간
502를 받을 수 있었어요.

**에스컬레이션 임계값**(BMC가 없는 노드는 부팅이 실패하면 사람이 현장에 가야 해요):

- **약 3분까지 ssh가 응답하지 않으면** 부팅과 연결 경로를 확인해요. 위의 6분을
  기다리는 대신 물리적 접근도 준비해요.
- **ssh는 올라왔는데 엔드포인트가 약 12분을 넘겨 조용하면** 조사해요
  (`journalctl -u pickle-vllm`, `docker logs pickle-vllm`). 그 전에는 유닛을 **재시작하지
  않아요.** 적재 도중 재시작하면 진행한 적재를 버리고 처음부터 다시 시작해요.

기록한 측정에서는 웜 `systemctl restart`도 콜드 부팅보다 빠르지 않았어요. **2026-10-23에 재시작 명령부터
`Application startup complete`까지 다섯 번 쟀고 6분 3초에서 6분 33초였다**(네 번은 계획한 재시작, 한 번은 엔진 오류 뒤
자동 재시작. 가중치 읽기 134~163초, 엔진 초기화 136~141초 중 컴파일 47~49초). 위 표의 360초는 재부팅 명령부터 센
값이라 펌웨어 POST와 커널 부팅, GPU 드라이버 초기화, docker 데몬 기동이 더 들어 있는데도
같은 6분대다. 이 문단의 이전 값 「웜 재시작 165초」는 가중치 읽기 시간과 비슷한 수치로,
엔진 초기화를 빼고 잰 것으로 보인다. 재시작도 6분으로 계획해요. 유닛의 `ExecStartPre`가
**매** 기동마다 page cache를 비우므로 웜 재시작도 콜드와 똑같이 로컬 저장소에서 가중치
전체를 다시 읽어요. 그 줄은 통합 메모리 기동 검사 문제 때문에 있는
것이고, "재시작을 따뜻하게 하려고" 지우면 그 실패가 되살아나는 데다 빨라지지도 않아요.

측정은 정상 종료 재부팅 1회이므로 위 임계값은 계약이 아니라 지침으로 다뤄요. 예기치 않은
정전 뒤의 부팅은 콜드 POST와 비정상 종료였을 때의 파일시스템 복구 때문에 더 느릴 수 있어요.

**전원이 돌아왔을 때 장비가 스스로 켜지는지는 위 측정이 전혀 다루지 않아요.** 그것은
systemd가 아니라 펌웨어 동작이고, BMC가 없는 노드라 네트워크로 읽거나 바꿀 수 없어요. 정전을
기다리지 않고 확인할 수는 있어요. 물리 콘솔에서 펌웨어 설정을 읽거나, 사람이 지켜보는
상태에서 전원을 분리해 확인해요. 둘 중 하나를 하기 전까지는 쓰는 모델에 그런 옵션이 있다고
가정하지 않고, 사람이 전원 버튼에 닿을 수 있다는 전제로 계획해요.

## 함께 고려할 것

- 이 예시의 게이트웨이는 기존 브리지 egress로 GPU 노드에 연결해요. 실제 배포 대상은
  선택한 업스트림의 연결 경로를 확인하고 불필요한 호스트 방화벽 규칙을 추가하지 않아요.
- 서빙 API 키는 의도적으로 한 값이 두 곳에 있어요. 여기의 vault 원본과 게이트웨이 env의
  업스트림 블록이에요. 회전은 vault 에 새 값을 만들고, 여기에 다시 적용한 뒤, 게이트웨이 env를
  갱신하는 순서로 진행해요. 게이트웨이의 환경 적용과 재시작 절차까지 마친 뒤 새 요청의
  정상 응답을 확인해요. 반영 사이에는 인증 실패가 생길 수 있어요.

최종 갱신: 2026-11-08
