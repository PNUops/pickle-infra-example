"""GPU temperature and utilization from nvidia-smi on the host itself."""
from __future__ import annotations

from . import CheckError, bad, ok

KIND = 'gpu'
DETAIL = {'temperature_c': int, 'utilization_pct': int, 'gpus': int}
REASONS = {
    'GPU_TOO_HOT': ('GPU 온도가 {temperature_c}°C까지 올라갔어요.', 'GPU 과열'),
    'GPU_UNAVAILABLE': ('GPU 상태를 읽지 못했어요. 드라이버나 장치에 문제가 있을 수 있어요.', 'GPU 이상'),
}
QUERY = ['nvidia-smi', '--query-gpu=temperature.gpu,utilization.gpu', '--format=csv,noheader,nounits']


def validate(args):
    limit = args.get('max_temp_c', 90)
    if type(limit) is not int or not 50 <= limit <= 120:
        raise ValueError('max_temp_c')
    timeout = args.get('timeout', 10)
    if type(timeout) is not int or not 2 <= timeout <= 30:
        raise ValueError('timeout')


def _number(text):
    text = text.strip()
    return int(text) if text.isdigit() else None


def run(args, ctx):
    try:
        output = ctx.run(QUERY, timeout=args.get('timeout', 10))
    except CheckError:
        # nvidia-smi exits non-zero when the driver has lost the device, which
        # is a fault on the observed host rather than in this check.
        return bad('FAIL', 'GPU_UNAVAILABLE')
    temperatures, utilizations = [], []
    for line in output.splitlines():
        fields = line.split(',')
        if len(fields) != 2:
            raise CheckError('CHECK_CRASHED')
        temperature, utilization = _number(fields[0]), _number(fields[1])
        if temperature is None:
            raise CheckError('CHECK_CRASHED')
        temperatures.append(temperature)
        # Unified-memory parts can print [N/A] for utilization; leave it out then.
        if utilization is not None:
            utilizations.append(utilization)
    if not temperatures:
        return bad('FAIL', 'GPU_UNAVAILABLE')
    detail = {'temperature_c': max(temperatures), 'gpus': len(temperatures)}
    if utilizations:
        detail['utilization_pct'] = max(utilizations)
    if detail['temperature_c'] >= args.get('max_temp_c', 90):
        return bad('FAIL', 'GPU_TOO_HOT', **detail)
    return ok(**detail)
