# Runbook — vLLM serving on the GPU node

Operates the self-hosted model behind the campus LLM API. The serving box is an
aarch64 GPU node with a unified-memory architecture (no dedicated VRAM; the
model-size budget and the OS share one pool). Access rides the operator root
key (`Host gpu-node-root` in the development machine's ssh config, key in the
workspace key store); nothing here runs on the box's own crontab or checkout.

The moving parts, and where each is mastered:

| Piece | Master | Applied by |
|---|---|---|
| systemd unit `pickle-vllm.service` (image pin, model, flags) | `hosts/gpu-node/systemd/` | `scripts/apply-gpu-node-vllm.sh` |
| env `/etc/pickle/vllm.env` (serving API key) | operator vault serving env | same script |
| model weights + container image | Hugging Face / Docker Hub, cached under `/var/lib/pickle-vllm/hf-cache` and the docker image store | first service start (or a by-hand pre-pull below) |

The serving endpoint is `http://192.0.2.20:8000/v1` (campus band only, no public
inbound), OpenAI-compatible, authenticated with the `VLLM_API_KEY` bearer value
from the env file. The LLM gateway is the only intended client and routes the
self-serve catalog model here — a restart is a brief self-serve outage that
the gateway surfaces as upstream errors and absorbs on its own; no other
service is affected.

## Start / stop

```bash
ssh gpu-node-root systemctl start pickle-vllm    # cold start loads the model: minutes, TimeoutStartSec=1800
ssh gpu-node-root systemctl stop pickle-vllm
ssh gpu-node-root journalctl -u pickle-vllm -f   # watch the load; "Application startup complete" = ready
```

First request after a start is slow (JIT warmup); warm it:

```bash
curl -s -H "Authorization: Bearer $KEY" http://192.0.2.20:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<served name>","messages":[{"role":"user","content":"ping"}],"max_completion_tokens":8}'
```

The unit's `ExecStartPre` drops page caches before every start — deliberate, a
vLLM unified-memory workaround (its startup memory check counts reclaimable
cache as used, vllm-project/vllm#35313). Expect the box's file cache to be cold
after a restart.

## Health check

```bash
KEY=$(ssh gpu-node-root "grep ^VLLM_API_KEY= /etc/pickle/vllm.env | cut -d= -f2")
curl -s -H "Authorization: Bearer $KEY" http://192.0.2.20:8000/v1/models
```

Unauthenticated requests must get 401 — if `/v1/models` answers without the
bearer, the env file did not reach the process; stop the service and re-apply.

After any model or quantization change, make the first request a known-answer
prompt (e.g. "What is 2+2?" must contain "4") before trusting the endpoint: a
mis-selected FP4 GEMM backend on this GPU class emits plausible garbage
instead of failing, and only an answer check catches it.

## Model or flag change, and rollback

The unit file is the whole configuration — model id, quantization, memory
fraction, context length. To change any of it:

1. Edit `hosts/gpu-node/systemd/pickle-vllm.service` in this repository.
2. `scripts/apply-gpu-node-vllm.sh` (restarts only when something changed).
3. Verify health as above, then commit the unit change — the git history of
   that one file IS the rollback path: revert the commit and re-apply.

A new model downloads on first start into the persistent hf-cache; to avoid
paying the download inside a service start, pre-pull by hand:

```bash
ssh gpu-node-root docker run --rm --gpus all \
  -v /var/lib/pickle-vllm/hf-cache:/root/.cache/huggingface \
  --entrypoint hf vllm/vllm-openai:v0.27.1 download <org/model>
```

Old weights stay in the cache and make rollback instant; prune consciously.

## Failure recovery

- **OOM / failed to allocate during load**: the unified pool is shared with the
  OS — check `free -h` first. Lowering `--gpu-memory-utilization` (or
  `--max-model-len`) in the unit is the knob, via the change procedure above.
- **"Unknown SF transformation" at engine start**: the FP8 MoE path on this GPU
  needs `VLLM_USE_DEEP_GEMM=0`, which the unit carries; seeing this means that
  env line was dropped — put it back.
- **Service flapping (Restart=on-failure loop)**: `journalctl -u pickle-vllm
  --since -30min`. A bad flag or a model id typo fails identically every time —
  stop the service, fix the unit, re-apply.
- **Serving wedged but process alive**: `systemctl restart pickle-vllm` is the
  right move — the gateway retries upstreams and its own error envelope covers
  the brief self-serve gap.

## Box reboot

A GPU node without a BMC has no remote console — a reboot that does not come
back needs physical access. Reboot only over a working ssh session:

```bash
ssh gpu-node-root systemctl reboot
```

The unit is `WantedBy=multi-user.target` and docker is enabled, so serving
returns on its own; the model load makes that minutes, then run the health
check.

## Interactions to keep in mind

- The gateway reaches the GPU node over the existing infra-bridge egress; no
  host firewall rule is involved and none should be added for this path.
- The serving API key is one value in two places by design: the vault master
  here, and the gateway env's upstream block. Rotate by
  generating a new value into the vault, re-applying here, then updating the
  gateway env — in that order, the gap is a brief upstream-auth failure the
  gateway absorbs as upstream error.
