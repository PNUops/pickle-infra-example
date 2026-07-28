#!/usr/bin/env bash
# Builds the Ubuntu 24.04 cloud-init VM template (VMID 1000) on a Proxmox node.
# Idempotent: safe to re-run; refuses to overwrite an existing template unless
# --rebuild is given.
set -euo pipefail

TEMPLATE_VMID="${TEMPLATE_VMID:-1000}"
TEMPLATE_NAME="${TEMPLATE_NAME:-ubuntu-2404-template}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr2}"
# Guest admin account. Must match the cloud image's default user so cloud-init
# and the sudoers drop-in below refer to the same account (ubuntu for Ubuntu
# images; other distros follow their own cloud-standard name, e.g. rocky).
# Changing it also requires the api side to send the same name as ciuser
# (vms.ssh_username row default) — the template alone does not decide it.
CIUSER="${CIUSER:-ubuntu}"
IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
SUMS_URL="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
IMG_DIR="/var/lib/vz/template/iso"
IMG_PATH="${IMG_DIR}/noble-server-cloudimg-amd64.img"

REBUILD=0
[ "${1:-}" = "--rebuild" ] && REBUILD=1

if qm status "$TEMPLATE_VMID" >/dev/null 2>&1; then
  if [ "$REBUILD" -eq 1 ]; then
    echo "removing existing VMID $TEMPLATE_VMID for rebuild"
    qm destroy "$TEMPLATE_VMID" --purge
  else
    echo "VMID $TEMPLATE_VMID already exists; use --rebuild to replace. Nothing to do."
    exit 0
  fi
fi

command -v virt-customize >/dev/null || {
  echo "installing libguestfs-tools (for virt-customize)"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libguestfs-tools
}

mkdir -p "$IMG_DIR"
if [ ! -f "$IMG_PATH" ]; then
  echo "downloading cloud image"
  curl -fL --retry 3 -o "$IMG_PATH.tmp" "$IMG_URL"
  expected=$(curl -fsSL "$SUMS_URL" | awk '/noble-server-cloudimg-amd64.img$/ {print $1}')
  actual=$(sha256sum "$IMG_PATH.tmp" | awk '{print $1}')
  if [ "$expected" != "$actual" ]; then
    echo "SHA256 mismatch: expected $expected got $actual" >&2
    rm -f "$IMG_PATH.tmp"
    exit 1
  fi
  mv "$IMG_PATH.tmp" "$IMG_PATH"
fi

# Bake in the guest agent (cloud image ships without it) on a working copy,
# keeping the pristine upstream image cached for future rebuilds.
WORK_IMG="${IMG_PATH%.img}-pickle.img"
cp -f "$IMG_PATH" "$WORK_IMG"
# Ubuntu cloud images ship PasswordAuthentication=no (60-cloudimg-settings.conf)
# and Proxmox cloud-init does not set ssh_pwauth, so enable password SSH here.
# Root login stays at the distro default (no password root login).
# PasswordAuthentication yes is intentional: enforcement of who may use a
# password lives in the SSH gateway (per-VM ssh_password_enabled opt-in), not the
# guest sshd.
#
# sudoers: cloud-init writes /etc/sudoers.d/90-cloud-init-users granting the
# default user NOPASSWD. Pickle requires sudo to demand the password instead
# (the VM password = the sudo credential, gated by password_reveal_min_role).
# sudo reads /etc/sudoers.d in C-locale lexical order and the LAST matching rule
# wins, so 99-pickle sorts after 90-cloud-init-users and overrides it to PASSWD.
# visudo -cf validates the drop-in at build time (a syntax error would otherwise
# only surface as a broken sudo on every provisioned VM). 0440 is the mode sudo
# requires for sudoers files.
virt-customize -a "$WORK_IMG" \
  --install qemu-guest-agent \
  --timezone Asia/Seoul \
  --run-command "rm -f /etc/ssh/sshd_config.d/60-cloudimg-settings.conf" \
  --write "/etc/ssh/sshd_config.d/55-pickle.conf:PasswordAuthentication yes" \
  --write "/etc/sudoers.d/99-pickle:${CIUSER} ALL=(ALL:ALL) PASSWD:ALL" \
  --run-command "chmod 440 /etc/sudoers.d/99-pickle && visudo -cf /etc/sudoers.d/99-pickle" \
  --truncate /etc/machine-id

echo "creating VM $TEMPLATE_VMID"
qm create "$TEMPLATE_VMID" \
  --name "$TEMPLATE_NAME" \
  --ostype l26 \
  --cpu x86-64-v2-AES \
  --cores 2 \
  --memory 2048 \
  --net0 "virtio,bridge=${BRIDGE}" \
  --scsihw virtio-scsi-single \
  --agent enabled=1 \
  --serial0 socket \
  --vga serial0

qm set "$TEMPLATE_VMID" --scsi0 "${STORAGE}:0,import-from=${WORK_IMG},discard=on"
qm set "$TEMPLATE_VMID" --ide2 "${STORAGE}:cloudinit"
qm set "$TEMPLATE_VMID" --boot order=scsi0
qm template "$TEMPLATE_VMID"
rm -f "$WORK_IMG"

echo "template $TEMPLATE_VMID ($TEMPLATE_NAME) ready"
