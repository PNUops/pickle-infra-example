# 인증서 등록 후 PVE qdevice 활성화

두 노드 PVE 클러스터에서 공개 CA와 서명 인증서를 먼저 교환한 뒤 사용하는 절차다.
인증서 교환은 [백업 호스트 런북](backup-host.md)을 따른다. 이 스크립트는 qnetd 호스트에
SSH하지 않으며 NSS를 생성하거나 기존 인증서를 삭제하지 않는다. 두 PVE 사이에는 이미
검증된 클러스터 root SSH 신뢰를 사용한다.

## 사전 조건

- 두 PVE가 online이고 기존 정족수는 expected/total votes 2, quorum 2다.
- 각 PVE에 `corosync-qdevice`와 `libnss3-tools`가 설치돼 있다. 패키지 버전과 서명은
  실행 시 해당 PVE의 공식 APT에서 확인한다. 패키지 설치만으로 witness가 등록되지는 않는다.
- 두 PVE의 `/etc/corosync/qdevice/net/nssdb/`에 같은 qnetd CA와 같은 서명된 cluster
  certificate 및 private key가 등록돼 있다. `pwdfile.txt`와 `key4.db`는 root 소유 0600이다.
- dept-node의 qnetd는 확인한 NetBird 주소의 TCP 5403에서만 TLS와 client certificate를 요구한다.
  PVE 두 peer에서만 해당 서비스 접근을 허용하며 실제 peer 주소를 입력한다.
- 인증서 등록에 사용한 CA와 서명 인증서의 DER SHA-256을 알고 있다. 전송 파일의 형식이
  PEM이면 DER로 변환한 바이트를 해시한다. 패키지 도구의 기본 nickname은 `QNet CA`와
  `Cluster Cert`다. 다른 NSS를 이 형식에 맞춘다며 초기화하지 않는다.

Debian 패키지 도구는 NSS private 파일을 0660으로 생성할 수 있다. 인증서 import 직후
각 PVE에서 아래 service identity를 확인한다. `User`와 `Group`이 비어 있거나 root이고
`DynamicUser=no`인 새 구성이어야 한다. 다른 service identity면 권한을 바꾸지 않고
해당 설치 상태부터 검토한다. dept-node qnetd의 NSS 권한에는 이 명령을 적용하지 않는다.

```bash
systemctl show corosync-qdevice.service -p User -p Group -p DynamicUser
chown root:root /etc/corosync/qdevice/net/nssdb/key4.db /etc/corosync/qdevice/net/nssdb/pwdfile.txt
chmod 600 /etc/corosync/qdevice/net/nssdb/key4.db /etc/corosync/qdevice/net/nssdb/pwdfile.txt
```

공개 인증서 fingerprint는 다음처럼 산출한다. PEM 입력에는 `-inform PEM`을 사용한다.

```bash
openssl x509 -inform DER -in qnetd-cacert.crt -outform DER | sha256sum
openssl x509 -inform DER -in cluster-prod-cluster.crt -outform DER | sha256sum
```

Private key나 NSS password를 터미널에 출력하지 않는다. PKCS12는 PVE root끼리 전송하고
보호 디렉터리에만 보관한다. 인증서 검증과 private key 존재 검사는 각 노드 안에서 실행한다.

## 사전 검사와 적용

아래 값은 예시이며 실제로 확인한 노드 이름과 peer 주소, 공개 인증서 hash로 바꾼다.
스크립트는 실행 호스트 이름도 명시적으로 대조한다.

```bash
python3 scripts/activate-qdevice.py \
  --expected-host node-a --cluster prod-cluster --nodes node-a node-b \
  --witness-ip 100.64.0.30 \
  --ca-sha256 <ca-der-sha256> --certificate-sha256 <cluster-cert-der-sha256>
```

기본 실행은 읽기 전용이다. 기존 qdevice, quorum override, 서로 다른 인증서, offline
노드, 활성화된 기존 qdevice service가 있으면 멈춘다. 실제 disk/RAID와 PBS VM 재부팅을
이 검사로 확인했다고 판단하지 않는다.

적용할 때는 root 소유 0700인 **새 빈** 보호 디렉터리를 만든 뒤 같은 명령에
`--backup-dir /pickle/backup/qdevice-activation --apply`를 추가한다. 기존 폴더를 비워서
재사용하지 않는다. 설정 사본과 비밀 없는 전후 점검 결과가 그곳에 남는다.

1. 두 PVE에서 NSS client certificate의 CA·hash·cluster CN·유효성·private key와 witness
   TCP 연결, 기존 2 votes 상태를 확인한다.
2. 현재 설정의 hash를 고정하고 `corosync.conf` 클러스터 잠금 안에서 최신 설정과 노드,
   quorum을 다시 대조한다. `PVE::Corosync::atomic_write_conf`로 device 절만 추가한다.
   설정은 `net`, `ffsplit`, vote 1, `tls=required`다. TLS 미지원 서버로의 fallback은 없다.
3. 두 PVE의 qdevice를 enable/start하고 corosync 설정을 reload한다. 두 곳 모두 expected/
   total votes 3, quorum 2와 Qdevice flag, 연결 상태, TLS, service active/enabled를 확인한다.
4. 확인 성공은 `activation.json`의 `stage=verified`와 `verified=true`로 기록한다.

설정 쓰기나 서비스 시작 도중 실패하면 일부 변경이 남을 수 있다. `activation_attempted`는
쓰기를 시도했다는 뜻이며 결과가 불확실하므로 실제 설정을 조회한다. 스크립트가 종료 코드
0을 내지 않았으면 witness 검증 완료로 간주하지 않는다. 실패 직후 다른 PVE를 재부팅하지 않는다.

## 되돌리기와 후속 실측

두 PVE를 online/quorate 상태로 유지하고 현재 `corosync.conf`, `pvecm status`, 두 qdevice
로그와 dept-node listener/TLS 상태를 먼저 확인한다. 자동으로 expected votes를 낮추지 않는다.
현재 설정이 다른 작업으로 변경됐으면 과거 설정 파일을 통째로 덮어쓰지 않는다.

qdevice를 철회해야 하면 먼저 두 PVE의 NSS를 별도 root 소유 0700 폴더에 0600 파일로
백업한다. 두 PVE 정상 상태에서 지원 명령 `pvecm qdevice remove`를 실행한다. 이 명령은
qdevice NSS도 제거하므로 인증서 복구 사본 없이 실행하지 않는다. 이후 두 노드의 기존
2 votes/quorum 2와 관리 경로를 확인한다. 이것은 실패한 구성을 원복하는 절차이며,
시험을 통과시키기 위해 votes를 낮추는 명령은 사용하지 않는다.

후속 검증은 PBS VM 재부팅 중 witness 유지, 두 PVE 정상 중 qnetd 중단·복귀, 한 PVE씩
중단한 동안 생존 노드의 quorum을 각각 측정한다. qnetd가 없는 동안 추가 PVE를 멈추지 않는다.
이 결과는 자동 HA나 dept-node의 물리 전원 장애 복구를 입증하지 않는다.

## 검증 범위

설정 잠금 뒤 hash 변경, 잘못된 클러스터·노드·votes, 기존 qdevice와 quorum 상실은
offline fixture에서 쓰기 거부를 검사한다. 현재 PVE의 실제 parser/SSH 함수는 읽기 전용
preview로 확인한다. NSS 등록과 TLS handshake, 실제 votes 3은 배치 후 따로 확인한다.

[Corosync qdevice 설정](https://manpages.debian.org/trixie/corosync-qdevice/corosync-qdevice.8.en.html),
[Proxmox 클러스터 관리](https://pve.proxmox.com/pve-docs/pvecm.1.html)를 참고한다.
