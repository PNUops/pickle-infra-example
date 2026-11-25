"""DNS resolution of a public name, optionally against the addresses it must hold.

Expected addresses live only in the catalog. Results carry counts, never an
address, because they reach a public channel.
"""
from __future__ import annotations

import ipaddress
import socket

from . import bad, ok
from .http import HOST, REASONS as HTTP_REASONS
from .llm_generation import LOCAL_NETWORKS

# Addresses no client outside the site can reach: the local networks plus
# "this network" (a common sinkhole answer), the shared address space (RFC 6598),
# link-local, the IETF protocol block, multicast and the reserved block, built
# from integers like them.
UNREACHABLE = LOCAL_NETWORKS + tuple(ipaddress.IPv4Network(value) for value in (
    (0x00000000, 8), (0x64400000, 10), (0xA9FE0000, 16), (0xC0000000, 24), (0xE0000000, 3)))

KIND = 'dns'
DETAIL = {'address_count': int, 'missing_count': int, 'private_count': int, 'extra_count': int}
REASONS = {
    'DNS_FAILED': HTTP_REASONS['DNS_FAILED'],
    'DNS_UNEXPECTED': ('도메인이 예상과 다른 주소를 가리키고 있어요.', 'DNS 오류'),
    'DNS_PRIVATE_ADDRESS': ('도메인이 바깥에서 닿지 않는 사설 주소 {private_count}개를 함께 알려 주고 있어요. '
                            '그 주소를 고른 사용자는 접속이 늦거나 실패해요.', 'DNS 오류'),
    'DNS_EXTRA_ADDRESS': ('도메인이 예상 밖의 주소 {extra_count}개를 함께 알려 주고 있어요.', 'DNS 오류'),
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
    expected = set(args.get('expected', []))
    missing = expected - answers
    if missing:
        return bad('FAIL', 'DNS_UNEXPECTED', address_count=len(answers), missing_count=len(missing))
    # A public name that also hands out a private address sends some clients to
    # an address they cannot reach, while the others still get through.
    private = [a for a in answers if any(ipaddress.IPv4Address(a) in net for net in UNREACHABLE)]
    if private:
        return bad('FAIL', 'DNS_PRIVATE_ADDRESS', address_count=len(answers), private_count=len(private))
    extra = answers - expected if expected else set()
    if extra:
        return bad('WARN', 'DNS_EXTRA_ADDRESS', address_count=len(answers), extra_count=len(extra))
    return ok(address_count=len(answers))
