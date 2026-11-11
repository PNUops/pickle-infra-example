"""Root filesystem usage inside containers; the fullest one decides."""
from __future__ import annotations

from . import _pve, bad, ok

KIND = 'container_disk'
DETAIL = {'worst_pct': int, 'ct_label': str}
REASONS = {
    'CT_DISK_HIGH': ('{ct_label} 컨테이너의 디스크 사용률이 {worst_pct}%예요.', '디스크 부족'),
}
# Same arithmetic as the old production health snapshot: blocks available to
# unprivileged writers, so the root reserve counts as used.
PROBE = 'import os;s=os.statvfs("/");print(round(100*(1-s.f_bavail/s.f_blocks)))'


def validate(args):
    _pve.ct_list(args['cts'])
    warn = _pve.number(args.get('warn_pct', 80), 'warn_pct', 1, 100)
    fail = _pve.number(args.get('fail_pct', 90), 'fail_pct', 1, 100)
    if warn > fail:
        raise ValueError('warn_pct must not exceed fail_pct')


def run(args, ctx):
    worst, where = -1, None
    for ct in args['cts']:
        pct = _pve.as_int(ctx.pct(ct, ['python3', '-I', '-c', PROBE]))
        if pct > worst:
            worst, where = pct, ct
    label = _pve.ct_label(where)
    if worst >= args.get('fail_pct', 90):
        return bad('FAIL', 'CT_DISK_HIGH', worst_pct=worst, ct_label=label)
    if worst >= args.get('warn_pct', 80):
        return bad('WARN', 'CT_DISK_HIGH', worst_pct=worst, ct_label=label)
    return ok(worst_pct=worst, ct_label=label)
