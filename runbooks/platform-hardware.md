# 플랫폼 하드웨어 자동 관측

Linux 장비의 CPU와 RAM, GPU를 자동으로 읽어 성과 화면에 제공해요.
수집 호스트의 독립 oneshot이 로컬 장비와 고정된 원격 장비를 읽고 최신 스냅샷을 API에 보내요.
API는 SSH에 접속하지 않아요.

## 수집 값과 고정 명령

- CPU 모델은 `lscpu -J`, 코어와 스레드는 sysfs의 present CPU와 thread sibling 그룹에서 읽어요.
- RAM은 `/proc/meminfo`의 `MemTotal`을 MiB로 변환해요.
- GPU는 PCI display/3D function을 열거하고 Matrox와 ASPEED 관리 화면 장치를 제외해요.
  모델은 NVML 또는 `lspci`에서 읽어요. 읽을 수 없는 GPU 메모리는 null로 남겨요.
- 모델별 사양 표나 이전 고정 용량으로 측정 실패를 채우지 않아요.

기존 reader 실행 파일은 두 고정 명령을 구분해요.

| SSH 명령 | 동작 |
| --- | --- |
| `read-health-v1` | 기존 health 결과만 읽어요. 하드웨어 probe를 실행하지 않아요 |
| `read-hardware-v1` | `allow_hardware: true`인 고정 profile에서만 장비 사양을 읽어요 |

기존 `reports`, health importer와 `expected_sources`, 생성 점검과 통지 상태는 바꾸지 않아요.
하드웨어 전용 `hardware-host` role은 새 명령만 처리해요.

수집 호스트는 원격 장비를 최대 네 개씩 병렬 조회하고 로컬 probe도 함께 실행해요.
각 원격 읽기는 12초, HTTP worker는 10초로 제한해요.
설정한 모든 장비가 15분 안에 관측됐을 때만 전체 스냅샷을 보내요.
대표 시각은 가장 오래된 실제 관측 시각이에요. 일부 관측이 없으면 전송하지 않아요.

## 설정

| 파일 | 역할 |
| --- | --- |
| `hosts/llm-monitor/hardware-reader.example.json` | 하드웨어 전용 고정 reader 설정 |
| `hosts/llm-monitor/hardware-reporter.example.json` | 수집 호스트, 고정 SSH 대상과 장비 ID 목록, HTTPS 주소 |
| `hosts/llm-monitor/systemd/pickle-platform-hardware.service` | 별도 25초 oneshot과 전용 credential |
| `hosts/llm-monitor/systemd/pickle-service-health-monitor.service.d/40-platform-hardware.conf` | 기존 중앙 수집 시작 때 별도 하드웨어 oneshot도 요청하는 선택 drop-in |

reader 예시는 `allow_hardware: false`, reporter 예시는 `enabled: false`예요.
장비 목록에는 ID와 연결 정보만 있고 용량은 없어요. 로컬 항목은 정확히 하나예요.
장비 ID는 API에 설정한 허용 목록과 같아야 해요.
원격 연결은 고정 private/VPN 주소와 전용 키, 호스트키 pin을 사용해요.
SSH agent와 forwarding, PTY, 임의 명령, proxy와 jump 설정을 사용하지 않아요.

## 적용 준비

이 디렉터리는 배포 소스와 예시를 제공해요. 실제 파일과 키, API 경로를 적용하기 전에
대상 장비와 변경 파일, 되돌릴 파일을 확인하고 운영 적용을 승인받아요.

1. 현재 unit과 reader 설정, 스크립트 해시를 보존해요. 설치 버전이 같다고 가정하지 않아요.
   기존 health timer와 importer, 상태 디렉터리는 유지해요.
2. 장비에 `hardware_observation.py` library를 먼저 설치한 뒤 reader 실행 파일을 갱신해요.
   library 없이 새 reader가 실행되는 중간 상태를 만들지 않아요.
   기존 모델과 gateway role은 유지하고 `allow_hardware`는 아직 켜지 않아요.
3. 기존 제한 키는 고정 reader 실행 파일과 profile이 같은지 대조해서 사용해요.
   새 장비에는 별도 제한 키와 고정 known-hosts 파일을 준비해요.
   키는 수집 호스트에만 보관하고, 대상의 해당 키에는 `restrict`, 정확한 발신 주소와
   고정 reader 명령을 설정해요. 일반 관리 키를 복사하지 않아요.
4. 하드웨어 전용 장비에는 `hardware-host` profile을 준비해요.
   `expected_hostname`을 실제 hostname과 대조하고 `allow_hardware: true`로 바꿔요.
   기존 모델과 gateway profile도 같은 옵션만 추가해요. health importer 설정은 바꾸지 않아요.
5. 수집 호스트에서 pin과 고정 경로로 `read-hardware-v1`이 되는지 확인해요.
   정상 종료 여부와 함께 `status: OBSERVED`와 실제 자산 필드를 확인해요.
   library 누락 등으로 `UNAVAILABLE`이면 기존 health 명령은 유지하고 API에는 보내지 않아요.
   임의 명령과 forwarding, PTY가 거절되는지도 확인해요.
   기존 `read-health-v1`의 결과와 시간 제한은 유지되는지 대조해요.
   연결 실패를 해결하려고 SSH 인증이나 방화벽을 완화하지 않아요.

실행 파일과 library는 root 소유 0755와 0644로 설치해요.
수집 호스트의 설정과 키는 root 소유 0600, 디렉터리는 0700으로 둬요.
장비 reader 설정은 **SSH 실행 계정 소유 0600**과 그 계정이 접근하는 0700 디렉터리에 둬요.
비루트 계정은 예를 들어 `~/.config/pickle-llm-health/reader.json`을 사용하고 고정 명령에
그 절대 경로를 지정해요. 관측 명령에 sudo를 추가하지 않아요.

## API 전송

전송 주소는 **HTTPS `/internal/platform-hardware-observations`**예요.
API의 전용 bearer, source ID와 실제 TCP peer 제한을 준비해요.
기존 LLM 원장이나 사용자 키를 재사용하지 않아요. 프록시를 사용하면 발신 제한과 API의
실제 마지막 hop을 검증해요. 임의의 forwarded header를 신뢰하지 않아요.

본문은 64KiB 이하이며 장비는 최대 32대, 장비별 GPU 항목은 최대 16개예요.

```json
{
  "schemaVersion": 1,
  "sourceId": "00000000-0000-4000-8000-000000000201",
  "observedAt": "2026-01-02T03:04:05.000Z",
  "assets": [{
    "id": "compute-a",
    "displayModel": "Example compute server",
    "cpuModel": "Example CPU",
    "cores": 4,
    "threads": 8,
    "memoryMiB": 16384,
    "gpus": [{
      "model": "Example accelerator",
      "count": 1,
      "dedicatedMemoryMiB": null,
      "unifiedMemoryMiB": null
    }]
  }]
}
```

정상 저장은 `200 {"sourceId": "…", "observedAt": "…"}`로 확인해요.
이미 같거나 새로운 스냅샷이 있으면 409를 보내며 reporter는 정상 건너뛰기로 처리해요.
다음 실행은 새 관측을 다시 읽어요. 전송 이력이나 재시도 원장은 만들지 않아요.

1. API의 최신 스냅샷 스키마와 전용 수신 설정을 배포해요. 환경 행을 migration으로 넣지 않아요.
2. 수집 호스트에 reporter와 library, 전용 credential, root0600 `reporter.json`을 준비해요.
   credential 경로는 `/run/credentials/pickle-platform-hardware.service/api-token`을 사용해요.
3. `pickle-platform-hardware.service`와 선택 drop-in을 설치하고 unit 검증 후 daemon-reload해요.
   기존 health unit의 120초 제한과 timer는 바꾸지 않아요. 별도 oneshot은 25초 제한과 자체 상태 경로를 사용해요.
4. `enabled: true`로 바꾼 뒤 다음 자연 실행을 확인해요.
   최신 결과는 `/var/lib/pickle-platform-hardware/result.json`에 남아요.
   API 저장 결과와 관리자 화면을 함께 읽고 자동 측정값이 반영되는지 확인해요.

`Wants`는 하드웨어 unit을 함께 시작하지만 health unit의 성공 조건으로 삼지 않아요.
하드웨어 실패와 전송 지연은 기존 생성 점검과 통지의 시간 제한을 늘리지 않아요.

## 운영 수신 경로 예시

수집 호스트는 고정 장비를 읽고 기존 `pickle-prod.example.com` HTTPS에 전송해요.
별도 망의 장비에 SSH로 연결하려면 수집 호스트 한 대에서 해당 장비의 SSH 포트로
가는 한 방향 정책을 사용해요. 정책 그룹의 구성원이 각각 장비 한 대인지 확인해요.

엣지 프록시는 기존 PROXY 경로에서 복원한 수집 호스트의 POST만
`/internal/platform-hardware-observations`에서 받아요. 원래 요청 헤더 전달은 끄고
Host, Authorization, Content-Type을 지정해 API로 넘겨요. API 방화벽에서는
실제 TCP peer인 엣지 프록시만 새로 API 포트에 허용해요.
전용 경로를 바꿀 때 기존 공개 화면의 HTTPS 200과 다른 발신지의 403을 확인해요.
수집 호스트에서 bearer 없이 보내면 배포와 수신 설정이 끝난 API의 401로 경계를 확인할 수 있어요.

같은 HTTPS 이름이 내부 주소와 공개 주소로 해석되면 reporter 프로세스에만 내부 주소를 고정할 수 있어요.
수집 호스트의 `hosts` 조회가 DNS 전에 파일을 읽는지 확인해요. 호스트의 hosts 파일을 root 소유
사본에 복사하고 `192.0.2.10 pickle-prod.example.com`처럼 내부 HTTPS 이름이 한 주소에만
매핑되도록 준비해요. reporter unit의 drop-in에서
`BindReadOnlyPaths=/etc/pickle-platform-hardware/report-hosts:/etc/hosts`로 사본을 읽기 전용으로 연결해요.
호스트 전체의 이름 해석과 HTTPS hostname, CA 검증을 유지해요. 다음 자연 실행에서 내부 발신과 API 200을 확인해요.

수신 경로를 되돌릴 때는 수집 전송부터 끄고 엣지 프록시의 exact location만 제거해요.
`nginx -t`를 통과한 뒤 graceful reload하고 기존 화면을 확인해요.
API 방화벽의 영구 파일과 실행 규칙에서 해당 소스·포트·표식의 규칙만 제거해요.
별도 망에서는 이름과 대상을 대조한 전용 정책만 제거해요.

## 되돌리기

선택 drop-in을 제거하고 daemon-reload한 뒤 하드웨어 oneshot만 중지해요.
기존 health timer와 상태, 통지 이력은 유지해요.
내부 주소 바인딩도 거두려면 하드웨어 수집을 끄고 해당 drop-in과 hosts 사본을 설치 기록에 대조해요.
drop-in을 제거하고 daemon-reload한 뒤 그 사본을 제거해요. 호스트의 `/etc/hosts`는 유지해요.
reader의 `allow_hardware`를 끄면 새 명령이 거절되고 기존 health 명령은 유지돼요.
새 연결을 정리할 때는 이번에 추가한 설정과 정확한 제한 키 행만 제거해요.
기존 health 키나 `authorized_keys`, 상태 디렉터리를 일괄 삭제하지 않아요.
