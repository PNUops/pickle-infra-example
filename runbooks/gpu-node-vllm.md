# GPU 노드 vLLM 서빙 운영

교내 LLM API 뒤에서 자체 호스팅 모델을 서빙하는 절차다. 서빙 장비는 통합 메모리 구조의
aarch64 GPU 노드다(전용 VRAM이 없어 모델 크기 예산과 OS가 한 풀을 나눠 쓴다). 접근은 운영자 root 키를 쓴다(개발 머신 ssh config의 `Host gpu-node-root`, 키는 운영자
키 저장소). 이 문서의 어떤 것도 장비 자체의 crontab이나 체크아웃에서 돌지 않는다.

구성 요소와 각각의 원본 위치는 다음과 같다.

| 구성 요소 | 원본 | 적용 수단 |
|---|---|---|
| systemd 유닛 `pickle-vllm.service`(이미지 핀, 모델, 플래그) | `hosts/gpu-node/systemd/` | `scripts/apply-gpu-node-vllm.sh` |
| env `/etc/pickle/vllm.env`(서빙 API 키) | 운영자 볼트의 서빙 env | 같은 스크립트 |
| 모델 가중치와 컨테이너 이미지 | Hugging Face, Docker Hub. `/var/lib/pickle-vllm/hf-cache`와 docker 이미지 저장소에 캐시된다 | 최초 서비스 기동(또는 아래의 수동 사전 내려받기) |

서빙 엔드포인트는 `http://192.0.2.20:8000/v1`이다(캠퍼스 대역 전용, 공인 인바운드
없음). OpenAI 호환이고 env 파일의 `VLLM_API_KEY` bearer 값으로 인증한다. LLM 게이트웨이가
유일한 정상 클라이언트이고 셀프서브 카탈로그 모델을 여기로 보낸다. 재시작은 짧은 셀프서브
장애이며 게이트웨이가 업스트림 오류로 표면화하고 스스로 흡수한다. 다른 서비스는 영향을
받지 않는다.

## 기동과 정지

```bash
ssh gpu-node-root systemctl start pickle-vllm    # 콜드 스타트는 모델 적재로 수 분. TimeoutStartSec=1800
ssh gpu-node-root systemctl stop pickle-vllm
ssh gpu-node-root journalctl -u pickle-vllm -f   # 적재 관찰. "Application startup complete"가 준비 완료
```

기동 후 첫 요청은 JIT 워밍업 때문에 느리다. 미리 데운다.

```bash
curl -s -H "Authorization: Bearer $KEY" http://192.0.2.20:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<서빙 이름>","messages":[{"role":"user","content":"ping"}],"max_completion_tokens":8}'
```

유닛의 `ExecStartPre`는 매 기동 전에 page cache를 비운다. 의도한 것이며 vLLM의 통합 메모리
우회책이다(vLLM의 기동 시 메모리 검사가 회수 가능한 캐시를 사용 중으로 세는 문제,
vllm-project/vllm#35313). 재시작 후 장비의 파일 캐시가 차가운 것은 이 때문이다.

## 헬스 체크

```bash
KEY=$(ssh gpu-node-root "grep ^VLLM_API_KEY= /etc/pickle/vllm.env | cut -d= -f2")
curl -s -H "Authorization: Bearer $KEY" http://192.0.2.20:8000/v1/models
```

인증 없는 요청은 401을 받아야 한다. bearer 없이 `/v1/models`가 응답하면 env 파일이
프로세스에 닿지 않은 것이다. 서비스를 정지하고 다시 적용한다.

모델이나 양자화를 바꾼 뒤에는 엔드포인트를 믿기 전에 **첫 요청을 정답이 정해진 프롬프트로**
보낸다(예: "What is 2+2?"의 응답에 "4"가 있어야 한다). 이 GPU 계열에서 FP4 GEMM 백엔드를
잘못 고르면 실패하지 않고 그럴듯한 쓰레기를 내놓기 때문에, 정답 확인만이 이것을 잡는다.

## 모델과 플래그 변경, 롤백

유닛 파일이 설정의 전부다. 모델 id와 양자화, 메모리 비율, 컨텍스트 길이가 모두 거기 있다.
무엇이든 바꾸려면 다음을 따른다.

1. 이 레포의 `hosts/gpu-node/systemd/pickle-vllm.service`를 편집한다.
2. `scripts/apply-gpu-node-vllm.sh`를 실행한다(바뀐 것이 있을 때만 재시작한다).
3. 위의 헬스 체크로 확인한 뒤 유닛 변경을 커밋한다. 그 파일 하나의 git 이력이 곧 롤백
   경로다. 커밋을 되돌리고 다시 적용하면 된다.

새 모델은 첫 기동 때 영구 hf-cache로 내려받는다. 서비스 기동 안에서 내려받기 비용을 내지
않으려면 미리 받아 둔다.

```bash
ssh gpu-node-root docker run --rm --gpus all \
  -v /var/lib/pickle-vllm/hf-cache:/root/.cache/huggingface \
  --entrypoint hf vllm/vllm-openai:v0.27.1 download <org/model>
```

옛 가중치는 캐시에 남아 있어 롤백이 즉시 된다. 정리는 의식적으로 한다.

## 장애 복구

- **적재 중 OOM 또는 할당 실패**: 통합 풀을 OS와 나눠 쓰므로 `free -h`를 먼저 본다. 조절 손잡이는 유닛의 `--gpu-memory-utilization`(또는
  `--max-model-len`)이고, 위의 변경 절차로 바꾼다.
- **서비스 플래핑(`Restart=on-failure` 루프)**: `journalctl -u pickle-vllm --since -30min`.
  잘못된 플래그나 모델 id 오타는 매번 같은 방식으로 실패한다. 서비스를 정지하고 유닛을
  고친 뒤 다시 적용한다.
- **엔진 기동 시 "Unknown SF transformation"**: 이 GPU의 FP8 MoE 경로는 유닛이 담고 있는
  `VLLM_USE_DEEP_GEMM=0`을 필요로 한다. 이것이 보인다는 것은 그 env 줄이 빠졌다는 뜻이므로
  그 줄을 되돌린다.
- **프로세스는 살아 있는데 서빙이 멈춘 경우**: `systemctl restart pickle-vllm`이 맞다.
  게이트웨이가 업스트림을 재시도하고 자체 오류 봉투가 짧은 셀프서브 공백을 덮는다.

## 장비 재부팅

**BMC가 없는 GPU 노드는 원격 콘솔이 없다.** 재부팅 후 돌아오지 않으면 물리적 접근이
필요하다. 반드시 살아 있는 ssh 세션에서만 재부팅한다.

```bash
ssh gpu-node-root systemctl reboot
```

유닛이 `WantedBy=multi-user.target`이고 docker가 enable돼 있어 서빙은 스스로 돌아온다.
모델 적재 때문에 수 분이 걸리며, 그 뒤 헬스 체크를 돌린다.

**실측**. 의도적인 `systemctl reboot`이고 장비로서는 몇 주 만의 첫 재부팅이었다
(`journalctl --list-boots`로 확인).

| 지점 | 재부팅 명령으로부터 |
|---|---|
| ssh가 다시 응답 | **37초** |
| `pickle-vllm`이 `active` 보고 | 약 37초, 개입 없음 |
| 엔드포인트가 정답 프롬프트에 응답 | **약 6분**(360초) |

**`active`는 준비 완료가 아니다.** 유닛이 `Type=exec`이라 systemd는 `docker run`이 exec된
순간 기동됐다고 판단하고 모델을 기다리지 않는다. 유일한 준비 완료 신호는 엔드포인트의
실제 응답이다. `is-active`를 보고 노드가 복구됐다고 보고하면 게이트웨이는 그 뒤로도 5분간
502를 낸다.

**에스컬레이션 임계값**(BMC가 없는 노드는 부팅이 실패하면 사람이 현장에 가야 한다):

- **약 3분까지 ssh가 응답하지 않으면** 부팅 자체가 실패한 것이다. 저절로 진행되는 것은
  없으므로 위의 6분을 기다리지 말고 물리적 접근을 준비한다.
- **ssh는 올라왔는데 엔드포인트가 약 12분을 넘겨 조용하면** 조사한다
  (`journalctl -u pickle-vllm`, `docker logs pickle-vllm`). 그 전에는 유닛을 **재시작하지
  않는다.** 적재 도중 재시작은 진행된 적재를 버리고 시계를 되돌린다.

콜드 부팅은 같은 유닛의 웜 `systemctl restart`보다 느리다(360초 대 165초. 다만 세는 시작
지점이 달라 360초는 종료와 OS 부팅 전체를 포함하고 165초는 재시작 명령부터다). 추가된
시간은 펌웨어 POST와 커널 부팅, GPU 드라이버 초기화, docker 데몬 기동이지 가중치 읽기가
아니다. 유닛의 `ExecStartPre`가 **매** 기동마다 page cache를 비우므로 웜 재시작도 콜드와
똑같이 로컬 저장소에서 가중치 전체를 다시 읽는다. 그 줄은 통합 메모리 기동 검사 문제 때문에 있는
것이고, "재시작을 따뜻하게 하려고" 지우면 그 실패가 되살아나는 데다 빨라지지도 않는다.

측정은 정상 종료 재부팅 1회이므로 위 임계값은 계약이 아니라 지침으로 다룬다. 예기치 않은
정전 뒤의 부팅은 콜드 POST와 비정상 종료였을 때의 파일시스템 복구 때문에 더 느릴 수 있다.

**전원이 돌아왔을 때 장비가 스스로 켜지는지는 위 측정이 전혀 다루지 않는다.** 그것은
systemd가 아니라 펌웨어 동작이고, BMC가 없는 노드라 네트워크로 읽거나 바꿀 수 없다. 정전을
기다리지 않고 확인할 수는 있다. 물리 콘솔에서 펌웨어 설정을 읽거나, 사람이 지켜보는
상태에서 전원을 뽑아 본다. 둘 중 하나를 하기 전까지는 쓰는 모델에 그런 옵션이 있다고
가정하지 않고, 사람이 전원 버튼에 닿을 수 있다는 전제로 계획한다.

## 함께 고려할 것

- 게이트웨이는 기존 인프라 브리지 egress로 GPU 노드에 닿는다. 이 경로에 호스트 방화벽
  규칙은 관여하지 않으며 새로 추가해서도 안 된다.
- 서빙 API 키는 의도적으로 한 값이 두 곳에 있다. 여기의 볼트 원본과 게이트웨이 env의
  업스트림 블록이다. 회전은 볼트에 새 값을 만들고, 여기에 다시 적용한 뒤, 게이트웨이 env를
  갱신하는 순서로 한다. 이 순서에서 생기는 공백은 게이트웨이가 업스트림 오류로 흡수하는
  짧은 인증 실패다.
