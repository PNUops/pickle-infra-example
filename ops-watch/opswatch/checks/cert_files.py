"""Days left on certificate files inside a container; the soonest one decides."""
from __future__ import annotations

import calendar
import posixpath
import re
import time

from . import CheckError, _pve, bad, ok

KIND = 'cert_files'
DETAIL = {'days_left': int, 'cert_label': str}
REASONS = {
    'CERT_FILE_EXPIRING': ('인증서 {cert_label}의 유효기간이 {days_left}일 남았어요.', '인증서 만료 임박'),
    'CERT_FILE_EXPIRED': ('인증서 {cert_label}의 유효기간이 지났어요.', '인증서 만료'),
}
PATH = re.compile(r'/[A-Za-z0-9._/-]{1,250}\Z')
LABEL = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
LIVE = '/etc/letsencrypt/live/'


def validate(args):
    _pve.ct_id(args['ct'])
    paths = args['paths']
    if not isinstance(paths, list) or not paths or len(paths) > 16:
        raise ValueError('paths must be a non-empty list')
    for path in paths:
        if not isinstance(path, str) or not PATH.fullmatch(path) or '..' in path.split('/'):
            raise ValueError('paths must be absolute file paths')
    warn = _pve.number(args.get('warn_days', 20), 'warn_days', 1, 365)
    fail = _pve.number(args.get('fail_days', 7), 'fail_days', 0, 365)
    if fail > warn:
        raise ValueError('fail_days must not exceed warn_days')


def label(path):
    """A Let's Encrypt lineage is named by its directory; every file in it is fullchain.pem."""
    if path.startswith(LIVE) and path.count('/') == LIVE.count('/') + 1:
        name = posixpath.basename(posixpath.dirname(path))
    else:
        name = posixpath.basename(path)
    return name if LABEL.fullmatch(name) else 'certificate'


def not_after(text):
    match = re.fullmatch(r'notAfter=(\w{3}) +(\d{1,2}) (\d{2}:\d{2}:\d{2}) (\d{4}) GMT', text.strip())
    if not match:
        raise CheckError('CHECK_CRASHED')
    try:
        parsed = time.strptime(' '.join(match.groups()), '%b %d %H:%M:%S %Y')
    except ValueError:
        raise CheckError('CHECK_CRASHED') from None
    return calendar.timegm(parsed)


def run(args, ctx):
    soonest = None
    for path in args['paths']:
        end = not_after(ctx.pct(args['ct'], ['openssl', 'x509', '-enddate', '-noout', '-in', path]))
        if soonest is None or end < soonest[0]:
            soonest = (end, path)
    seconds = soonest[0] - ctx.now
    days = int(seconds // 86400)
    name = label(soonest[1])
    if seconds <= 0:
        return bad('FAIL', 'CERT_FILE_EXPIRED', days_left=days, cert_label=name)
    if days < args.get('fail_days', 7):
        return bad('FAIL', 'CERT_FILE_EXPIRING', days_left=days, cert_label=name)
    if days < args.get('warn_days', 20):
        return bad('WARN', 'CERT_FILE_EXPIRING', days_left=days, cert_label=name)
    return ok(days_left=days, cert_label=name)
