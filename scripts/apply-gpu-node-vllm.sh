#!/usr/bin/env bash
# Installs (or refreshes) the vLLM serving setup on the GPU node: the env file,
# the hf-cache directory, and the pickle-vllm systemd unit from hosts/gpu-node/.
# Idempotent — safe to re-run; systemd only restarts the service when the unit
# or env actually changed, and never touches a running model otherwise unless
# RESTART=1 is passed.
#
# Runs from a machine whose ~/.ssh/config carries the GPU_HOST alias (default
# gpu-node-root — a development machine, per the node-intake runbook's
# access-path convention). This is unlike the host-local apply scripts: the GPU
# node has no platform checkout and gets none; everything it needs travels over
# ssh.
#
# The env master is the operator vault's serving env. It is looked up from the
# workspace layout when present, or passed as ENV_FILE. The unit master is this
# repository's hosts/gpu-node/systemd/pickle-vllm.service — edit there,
# re-apply here, never on the host.
#
# NOT managed here: docker itself, the NVIDIA container toolkit runtime
# registration, and the model download (first ExecStart pulls the image and
# model into the mounted cache; the runbook covers pre-warming by hand).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PICKLE_ROOT="${PICKLE_ROOT:-$(cd "$REPO/.." && pwd)}"

GPU_HOST="${GPU_HOST:-gpu-node-root}"
ENV_FILE="${ENV_FILE:-${PICKLE_ROOT}/secrets/gpu-node-vllm.env}"
UNIT_SRC="$REPO/hosts/gpu-node/systemd/pickle-vllm.service"

REMOTE_ENV=/etc/pickle/vllm.env
REMOTE_UNIT=/etc/systemd/system/pickle-vllm.service
CACHE_DIR=/var/lib/pickle-vllm/hf-cache

[ -f "$UNIT_SRC" ] || { echo "unit master missing: $UNIT_SRC" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "env file missing: $ENV_FILE (vault checkout, or pass ENV_FILE=)" >&2; exit 1; }
# A git-crypt checkout that is still locked yields the ciphertext blob, which
# begins with a NUL byte then the ASCII magic "GITCRYPT"; strip NULs from the
# first bytes and compare. Refuse to install ciphertext as an env file.
if [ "$(head -c10 "$ENV_FILE" | tr -d '\000')" = "GITCRYPT" ]; then
  echo "env file is git-crypt ciphertext (vault locked): $ENV_FILE" >&2; exit 1
fi

echo "== preflight ($GPU_HOST)"
ssh -o BatchMode=yes "$GPU_HOST" 'command -v docker >/dev/null && systemctl is-enabled docker >/dev/null'

changed=0
sync_file() { # src remote-path mode
  local src="$1" dst="$2" mode="$3"
  if ssh -o BatchMode=yes "$GPU_HOST" "test -f '$dst'" \
     && [ "$(ssh -o BatchMode=yes "$GPU_HOST" "sha256sum '$dst'" | cut -d' ' -f1)" = "$(sha256sum "$src" | cut -d' ' -f1)" ]; then
    echo "unchanged: $dst"
  else
    # mkdir -p, not install -d -m: `install -d -mMODE` re-chmods an EXISTING
    # target, so it would silently drop /etc/systemd/system from 0755 to the
    # mode every run. The one directory we own (/etc/pickle) gets its mode set
    # explicitly below; the unit's parent is a shared system dir we must leave.
    ssh -o BatchMode=yes "$GPU_HOST" "mkdir -p \"\$(dirname '$dst')\""
    scp -q "$src" "$GPU_HOST:$dst.tmp"
    ssh -o BatchMode=yes "$GPU_HOST" "chmod $mode '$dst.tmp' && mv '$dst.tmp' '$dst'"
    echo "installed: $dst"
    changed=1
  fi
}

# /etc/pickle is ours; keep it operator-only. (Owns REMOTE_ENV's parent.)
ssh -o BatchMode=yes "$GPU_HOST" "install -d -m750 \"\$(dirname '$REMOTE_ENV')\""
sync_file "$ENV_FILE" "$REMOTE_ENV" 600
sync_file "$UNIT_SRC" "$REMOTE_UNIT" 644
ssh -o BatchMode=yes "$GPU_HOST" "install -d -m755 '$CACHE_DIR'"

ssh -o BatchMode=yes "$GPU_HOST" 'systemctl daemon-reload && systemctl enable pickle-vllm >/dev/null 2>&1 || systemctl enable pickle-vllm'

if [ "$changed" = 1 ] || [ "${RESTART:-0}" = 1 ]; then
  echo "== restarting pickle-vllm (model load takes minutes; TimeoutStartSec=1800)"
  ssh -o BatchMode=yes "$GPU_HOST" 'systemctl restart pickle-vllm'
else
  echo "== nothing changed; service left as-is (RESTART=1 to force)"
fi

echo "== status"
ssh -o BatchMode=yes "$GPU_HOST" 'systemctl --no-pager --lines=0 status pickle-vllm || true'
echo "done. Health: curl -H \"Authorization: Bearer \$VLLM_API_KEY\" http://192.0.2.20:8000/v1/models"
