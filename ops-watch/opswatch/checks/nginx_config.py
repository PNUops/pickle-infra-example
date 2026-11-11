"""`nginx -t` inside a container: the next reload would not be refused."""
from __future__ import annotations

from . import CheckError, _pve, bad, ok

KIND = 'nginx_config'
DETAIL = {}
REASONS = {
    'NGINX_CONFIG_INVALID': ('nginx 설정 검사에 실패했어요. 다음 재시작이나 다시 읽기가 거부돼요.', '설정 오류'),
}


def validate(args):
    _pve.ct_id(args['ct'])


def run(args, ctx):
    code = _pve.exit_code(ctx, args['ct'], ['nginx', '-t'], timeout=20)
    if code in (126, 127):
        # nginx is not installed or not executable: nothing was tested.
        raise CheckError('COMMAND_FAILED')
    if code:
        return bad('FAIL', 'NGINX_CONFIG_INVALID')
    return ok()
