# 중지 VM 템플릿을 다른 노드에 복제

이 절차는 같은 Proxmox cluster의 한 노드에 있는 중지 VM template을 `vzdump` archive로
보관하고, 다른 노드의 local storage에 새 VMID로 복원합니다. 원본 template과 그 volume은
유지합니다. 대상에는 새 template과 그 소유 volume만 생기며 physical disk format, host
주소와 network 설정은 바꾸지 않습니다.

예시에서는 source `pve-a`의 VMID 1101을 target `pve-b`의 VMID 1102로 복제합니다. 이름과
번호는 실제 인벤토리로 바꾸되, 한 실행 안에서는 바꾸지 않습니다.

```bash
set -euo pipefail
SOURCE_NODE=pve-a
TARGET_NODE=pve-b
SOURCE_VMID=1101
TARGET_VMID=1102
SOURCE_ROOT=/root/template-replication-source
TARGET_ROOT=/root/template-replication-target
```

## 시작 조건

두 노드는 같은 quorate cluster의 정확한 online member여야 합니다. source에서는 아래 상태를
모두 확인합니다.

- VMID 1101은 중지되고 lock이 없는 QEMU template입니다.
- `onboot`는 없거나 `0`입니다. Proxmox는 기본값 `0`을 raw config에서 생략할 수 있습니다.
- root disk와 cloud-init drive가 source node의 기대 storage에 있습니다.
- net0의 model, bridge, MTU와 `firewall=1`이 기대값과 같습니다.
- pending 변경, snapshot, tag, custom QEMU argument, 추가 NIC와 passthrough device가 없습니다.
- source config 파일의 SHA-256과 read-only root disk의 크기·SHA-256을 보호 기록에 남겼습니다.
- root disk를 read-only로 열어 SSH host key가 없고 machine-id가 `uninitialized`이며 cloud-init
  instance state가 비어 있음을 확인했습니다.

target에서는 VMID 1102가 cluster inventory와 모든 node config에 없어야 합니다. target
storage에도 `vm-1102-*`와 `base-1102-*` volume이 없어야 하며 archive, raw VMA와 복원 disk를
수용할 여유가 있어야 합니다. target VMID를 쓰는 HA resource와 실행 중인 task도 없어야 합니다.
`pveum acl list`에 `/vms/1102` ACL이 없고 `/etc/pve/firewall/1102.fw`도 없어야 합니다. 이 두
객체는 `qm destroy`가 `--purge` 없이도 지울 수 있으므로 시작 전 부재가 cleanup 소유권의 일부입니다.

각 작업 디렉터리는 root 소유 mode 0700으로 새로 만들고, 입력·log·receipt는 mode 0600으로
보관합니다. 기존 디렉터리나 파일을 덮어쓰지 않습니다.

## Source archive 만들기

source config SHA-256과 cluster 상태를 직전에 다시 확인합니다. archive 작업은 아래 한 VMID만
대상으로 합니다.

```bash
set -euo pipefail
umask 077
test ! -e "$SOURCE_ROOT"
mkdir -m 0700 -- "$SOURCE_ROOT"
vzdump "$SOURCE_VMID" --mode stop --compress zstd \
  --dumpdir "$SOURCE_ROOT" --remove 0

mapfile -t ARCHIVES < <(find "$SOURCE_ROOT" -maxdepth 1 -type f \
  -name "vzdump-qemu-${SOURCE_VMID}-*.vma.zst" -print)
test "${#ARCHIVES[@]}" -eq 1
ARCHIVE=${ARCHIVES[0]}
ARCHIVE_NAME=$(basename -- "$ARCHIVE")
```

중지 template도 backup 중에는 `vzdump`가 backup lock을 잡고 일시적인 paused QEMU를 실행할
수 있습니다. 실행 전·후 status를 각각 기록하고, 완료 뒤 source가 다시 stopped이고 config
SHA-256이 처음과 같은지 확인합니다. `--remove 0`은 이 실행이 이전 archive를 정리하지 않게
합니다.

생성된 basename을 그대로 사용합니다.

```text
vzdump-qemu-1101-YYYY_MM_DD-HH_MM_SS.vma.zst
```

`qmrestore`는 basename으로 archive 종류와 압축 형식을 판정합니다. `source-template.vma.zst`
같은 일반 이름은 지원되는 VMA여도 archive 정보 판정에서 거부됩니다. 전송 과정에서 `.part`를
붙일 수는 있지만 restore 전에는 원래 `vzdump-qemu-…vma.zst` basename으로 되돌립니다.

archive가 regular file인지 확인하고 mode 0600으로 고친 뒤 byte 수와 SHA-256을 기록합니다.
압축 stream과 VMA를 둘 다 검사합니다. 설치된 `vma`는 raw VMA filename을 받으므로, 여유
공간을 먼저 확인하고 같은 보호 디렉터리에 임시 raw VMA를 만듭니다.

```bash
set -euo pipefail
zstd --test --quiet "$ARCHIVE"
test ! -e "$SOURCE_ROOT/verified-source.vma"
zstd --decompress --stdout "$ARCHIVE" > "$SOURCE_ROOT/verified-source.vma"
chmod 0600 "$SOURCE_ROOT/verified-source.vma"
vma verify -v "$SOURCE_ROOT/verified-source.vma"
test ! -e "$SOURCE_ROOT/archive-config.txt"
vma config "$SOURCE_ROOT/verified-source.vma" > "$SOURCE_ROOT/archive-config.txt"
chmod 0600 "$SOURCE_ROOT/archive-config.txt"
```

embedded config에서 template, 이름, disk, NIC를 source config와 비교합니다. `onboot:` line은
없거나 exact `onboot: 0`이어야 합니다. substring 검색 대신 field별 line을 한 번씩 parse해
중복과 다른 값을 거부합니다.

## Archive 전송

이미 검증한 관리 경로로 archive를 target의 mode-0700 디렉터리에 전송합니다. 전송 중 파일은
원본과 구분되는 `.part` suffix를 사용합니다.

Source node에서 target 경로가 없는 것을 먼저 확인하고 target node에 디렉터리를 만듭니다.

```bash
set -euo pipefail
ssh "root@$TARGET_NODE" -- test ! -e "$TARGET_ROOT"
ssh "root@$TARGET_NODE" -- mkdir -m 0700 -- "$TARGET_ROOT"
TARGET_PART="$TARGET_ROOT/$ARCHIVE_NAME.part"
TARGET_ARCHIVE="$TARGET_ROOT/$ARCHIVE_NAME"
ssh "root@$TARGET_NODE" -- test ! -e "$TARGET_PART"
ssh "root@$TARGET_NODE" -- test ! -e "$TARGET_ARCHIVE"
scp -p -- "$ARCHIVE" "root@$TARGET_NODE:$TARGET_PART"
```

target에서 `.part`가 root 소유 regular file이고 mode 0600인지 확인합니다. source와 target의
byte 수와 SHA-256이 같고 `zstd --test`가 통과한 뒤에만, 같은 디렉터리 안에서 원래 basename으로
atomic rename합니다.

```bash
set -euo pipefail
ssh "root@$TARGET_NODE" -- mv -- "$TARGET_PART" "$TARGET_ARCHIVE"
```

Rename 뒤 SHA-256을 다시 확인합니다. Timestamp를 다시 추정하거나 glob의 첫 파일을 고르지
않습니다. 아래 restore는 source의 관리 shell에서 exact target 경로를 SSH로 전달해 target에서
실행하는 형태입니다.

## Target VMID로 복원

복원 직전에 quorum, exact online member set, VMID 1102 부재, target storage의 orphan volume
부재와 여유 공간을 다시 확인합니다. 이름이 보존된 archive만 사용합니다.

```bash
set -euo pipefail
ssh "root@$TARGET_NODE" -- qmrestore "$TARGET_ARCHIVE" "$TARGET_VMID" \
  --storage local-lvm --unique 1 --start 0 --ha-managed 0
```

`--force`와 `--live-restore`는 사용하지 않습니다. `--unique 1`은 target NIC MAC과 SMBIOS UUID를
새로 만들며, 유효한 VM generation ID도 restore 과정에서 새 값으로 바뀝니다. 기존 MAC이나
guest identity를 두 node에 함께 두면 안 됩니다. source net0에 Proxmox IPAM address field가
없음을 먼저 확인하고, restore 뒤 예상하지 않은 SDN IPAM allocation도 없는지 확인합니다.

복원 결과는 아래 기준을 모두 통과해야 합니다.

- target은 stopped template이며 `onboot`는 없거나 `0`입니다. HA resource가 아닙니다.
- name, CPU, core, memory, agent, boot, OS type, SCSI controller, serial console와 VGA가 source와
  같습니다.
- target MAC, SMBIOS UUID와 VM generation ID는 source와 다르고 형식이 유효합니다.
- net0의 MAC 이외 model, bridge, MTU와 firewall 값은 source와 같습니다.
- root disk는 target storage의 `base-1102-*`, cloud-init drive는 새 `vm-1102-cloudinit*`이고
  target VMID가 소유합니다.
- lock, pending 변경, snapshot, tag, custom argument, 추가 NIC, passthrough와 unused volume이
  없습니다.

Config digest, creation metadata, volume ID와 세 identity 값은 의도적으로 달라집니다. 이들을
source config와 byte-for-byte 비교하지 않습니다.

## Root image와 cloud-init 확인

target root LV는 read-only이며 보통 inactive입니다. 처음 상태를 기록하고, inactive일 때만
read-only activation을 수행합니다. `-K`를 사용해 template LV의 activation-skip marker를
존중한 채 이번 명령에서만 활성화합니다. 아래 두 명령 블록은 **target node의 root bash
shell에서만** 실행합니다. Source node에서 동명 LV를 대상으로 실행하지 않습니다.

아래처럼 별도 child bash에 cleanup trap을 먼저 설치합니다. 검사 명령이 실패해도 target이 stopped인
것을 확인한 뒤 이 절차가 활성화한 LV를 deactivate합니다. Status를 확인할 수 없으면 자동으로
끄지 않고 실패를 유지합니다.

```bash
bash <<'INSPECT'
set -euo pipefail
SOURCE_VMID=1101
TARGET_VMID=1102
LV=vg/base-1102-disk-0
DISK=/dev/vg/base-1102-disk-0
SOURCE_MANIFEST_SHA256=REVIEWED_SOURCE_MANIFEST_SHA256
BEFORE_ACTIVE=$(lvs --reportformat json -o lv_active "$LV" | jq -r '.report[0].lv[0].lv_active // ""')
ACTIVATED=0
cleanup() {
  rc=$?
  trap - EXIT INT TERM
  if [ "$ACTIVATED" -eq 1 ]; then
    if qm status "$TARGET_VMID" | grep -qx 'status: stopped'; then
      lvchange -an "$LV" || rc=1
    else
      echo 'target status is not proven stopped; leaving LV active for inspection' >&2
      rc=1
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM
if [ -z "$BEFORE_ACTIVE" ]; then
  lvchange -ay -K "$LV"
  ACTIVATED=1
fi
IMAGE_JSON=$(env LIBGUESTFS_BACKEND=direct guestfish --ro -a "$DISK" -i \
  cat /etc/pickle/image.json)
printf '%s\n' "$IMAGE_JSON" | python3 -c \
  'import json,sys; value=json.load(sys.stdin); expected=int(sys.argv[1]); assert value["templateVmid"]==expected; assert value["recipeRevision"] and value["imageChecksum"]' \
  "$SOURCE_VMID"
IMAGE_SHA256=$(printf '%s\n' "$IMAGE_JSON" | sha256sum | cut -d' ' -f1)
test "$IMAGE_SHA256" = "$SOURCE_MANIFEST_SHA256"
CLEAN=$(printf '%s\n' \
  'cat /etc/machine-id' \
  'exists /var/lib/cloud/instance' \
  'exists /var/lib/cloud/instances' \
  'exists /var/lib/cloud/data' \
  'glob-expand /etc/ssh/ssh_host_*' | \
  env LIBGUESTFS_BACKEND=direct guestfish --ro -a "$DISK" -i)
CLEAN_NONEMPTY=$(printf '%s\n' "$CLEAN" | sed '/^$/d')
EXPECTED_CLEAN=$(printf '%s\n' uninitialized false false false)
test "$CLEAN_NONEMPTY" = "$EXPECTED_CLEAN"
blockdev --getsize64 "$DISK"
sha256sum "$DISK"
INSPECT
```

`guestfish --ro`에서 SSH host key 부재, uninitialized machine-id, 빈 cloud-init instance state와
image provenance 파일을 확인합니다. full virtual disk 크기와 SHA-256은 source read-only disk와
같아야 합니다. 검사가 끝나면 target이 stopped인지 다시 확인하고, 이 절차가 활성화한 LV만
deactivate합니다. 처음 active였던 LV는 끄지 않습니다. Child bash가 끝난 뒤 LV 상태를 다시 읽어
처음 active/read-only 상태와 같은지 확인합니다.

Cloud-init data volume은 restore가 target VMID용으로 새로 만든 것입니다. source volume ID나
instance-specific user, password, SSH key, IP 설정을 그대로 가졌다고 가정하지 않고 config와
volume ownership을 별도로 확인합니다.

## Build provenance와 catalog 등록

Source image manifest의 `templateVmid`는 빌드가 실제 일어난 VMID 1101을 가리킵니다. 복제본을
1102로 복원해도 이 값을 바꾸지 않습니다. 값을 1102로 고치면 restore replica를 새 build처럼
기록하게 됩니다. Guest root image 안의 같은 manifest도 byte-identical provenance의 일부로
그대로 둡니다.

Source node의 image registration에는 원래 build manifest를 사용합니다. Target replica는
`build_manifest_file`을 `null`로 두고, source manifest SHA-256, VMA archive SHA-256, 전송 후
SHA-256, source·target full virtual disk SHA-256과 target config를 보호 receipt로 연결합니다.

두 replica는 같은 logical revision이므로 name, version, OS family/release, SSH username과
minimum disk를 같게 등록합니다. 표시명과 notes도 같게 유지합니다. 각 node별 VMID만 다릅니다.
`register-image.py`가 만드는 두 행은 모두 `DISABLED`이고 node는 `MAINTENANCE`로 남아야 합니다.
복원, read-only image 확인과 실제 guest 검증이 끝나기 전에는 image나 node를 활성화하지 않습니다.

## 실패와 정리

`qmrestore` CLI가 timeout되거나 연결이 끊겨도 PVE worker가 계속 실행될 수 있습니다. Log에서
UPID를 확인하고 task가 terminal state가 될 때까지 재실행, unlock, stop, destroy를 하지 않습니다.
실패 직후 조회는 중간 상태일 수 있으므로 완료 결과로 기록하지 않습니다.

Archive 이름 판정처럼 data write 전 실패해도 빈 target config가 생길 수 있습니다. 아래를 모두
증명할 때만 그 exact 빈 config 파일을 지웁니다.

- restore task와 관련 process가 종료되었습니다.
- target config가 이 실행에서 생긴 exact 경로이고 크기 0, SHA-256이 빈 파일 값입니다.
- 시작 전 target VMID가 없었다는 receipt가 있고, cluster inventory는 해당 entry가 없거나 이 실행의
  target node에 생긴 QEMU placeholder 한 건뿐입니다. 다른 node/type의 entry나 중복은 없습니다.
- target VMID의 volume, HA resource와 SDN IPAM allocation이 없습니다. 시작 전 부재를 기록한
  VM별 ACL과 firewall 파일도 새로 생기지 않았습니다.

Config가 비어 있지 않거나 target volume이 하나라도 있으면 빈 실패로 취급하지 않습니다. Task 종료
뒤 config의 name, VMID, target node와 각 volume의 owner가 이 실행의 target과 정확히 일치하는지
검토합니다. 시작 전 `/vms/<target-vmid>` ACL과 target firewall 파일이 없었다는 기록을 다시
대조하고, 현재 생긴 항목이 있다면 이 restore가 만든 exact 내용인지 별도로 증명합니다. 이 조건까지
포함해 소유권이 증명된 새 target만 `qm destroy <target-vmid>`로 정리할 수 있습니다.
`--purge`는 backup·replication 같은 인접 객체까지 넓게 지우므로 이 절차에서는 사용하지 않습니다.
Config가 없고 orphan volume만 남았으면 각 volume ID와 owner를 따로 증명한 뒤 exact volume만
회수합니다. Prefix나 VMID 범위로 일괄 삭제하지 않습니다.

Source template, source volume과 검증된 archive는 실패 정리 대상이 아닙니다. 자동 unlock이나
강제 정리는 하지 않습니다. 실패 원인이 해소되고 cluster, task, config와 volume 부재를 처음부터
다시 확인하기 전에는 같은 target VMID로 반복하지 않습니다.

이 절차의 완료는 성공한 `qmrestore` 한 줄이 아닙니다. Target config, identity, volume ownership,
full disk SHA-256, read-only guest image와 source 보존 receipt가 모두 있어야 복제가 확인됩니다.
Catalog 등록과 실제 guest 동작·packet 검증은 별도 단계입니다.
