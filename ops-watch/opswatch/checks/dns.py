"""DNS resolution of a public name, optionally against the addresses it must hold.

Expected addresses live only in the catalog. Results carry counts, never an
address, because they reach a public channel.
"""
from __future__ import annotations

import ipaddress
import socket

from . import bad, ok
from .http import HOST, REASONS as HTTP_REASONS

KIND = 'dns'
DETAIL = {'address_count': int, 'missing_count': int}
REASONS = {
    'DNS_FAILED': HTTP_REASONS['DNS_FAILED'],
    'DNS_UNEXPECTED': ('도메인이 예상과 다른 주소를 가리키고 있어요.', 'DNS 오류'),
}


def validate(args):
    if not isinstance(args.get('name'), str) or not HOST.fullmatch(args['name']):
        raise ValueError('name must be a lowercase DNS name')
    expected = args.get('expected', [])
    if not isinstance(expected, list) or len(expected) > 8:
        raise ValueError('expected must be a list of at most 8 addresses')
    for value in expected:
        if not isinstance(value, str):
            raise ValueError('expected address')
        ipaddress.IPv4Address(value)  # raises ValueError on anything else


def run(args, ctx):
    try:
        infos = socket.getaddrinfo(args['name'], None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return bad('FAIL', 'DNS_FAILED')
    answers = {info[4][0] for info in infos}
    if not answers:
        return bad('FAIL', 'DNS_FAILED')
    missing = set(args.get('expected', [])) - answers
    if missing:
        return bad('FAIL', 'DNS_UNEXPECTED', address_count=len(answers), missing_count=len(missing))
    return ok(address_count=len(answers))
