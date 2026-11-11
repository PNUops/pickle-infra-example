#!/usr/bin/env python3
"""External-vantage check tests: TLS, DNS, generation, SSH banner, units, WireGuard.

Sockets, HTTP and commands are mocked; nothing here touches the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opswatch import catalog as catalog_mod  # noqa: E402
from opswatch.agent import Context, run_check  # noqa: E402
from opswatch.checks import CheckError, REGISTRY, reasons  # noqa: E402
from opswatch.checks import dns, llm_generation, ssh_banner, systemd_unit, tls, wireguard  # noqa: E402
from opswatch.result import clean  # noqa: E402

NOW = 1794402000.0  # 2026-11-11 22:00 KST
DAY = 86400
CATALOG = Path(__file__).resolve().parents[1] / 'catalog'
NEW_KINDS = ('tls', 'dns', 'llm_generation', 'ssh_banner', 'systemd_unit', 'wireguard')


def ctx(state=None, outputs=None, now=NOW):
    """A Context whose commands answer from a {argv tuple: stdout or exception} map."""
    outputs = outputs or {}
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        value = outputs.get(tuple(argv))
        if value is None:
            return mock.Mock(returncode=1, stdout='')
        if isinstance(value, BaseException):
            raise value
        return mock.Mock(returncode=0, stdout=value)

    context = Context(now, {} if state is None else state, runner)
    context.calls = calls
    return context


def valid(result, kind):
    """Every result must pass the receiver's own validation."""
    return clean(result, kind, item_id='probe', sample_at=NOW)


def cert(days):
    expires = NOW + days * DAY + 3600
    return {'notAfter': time.strftime('%b %d %H:%M:%S %Y GMT', time.gmtime(expires))}


class RegistryTests(unittest.TestCase):
    def test_new_kinds_are_registered(self):
        for kind in NEW_KINDS:
            self.assertIn(kind, REGISTRY)

    def test_reason_table_has_no_conflicts_and_valid_categories(self):
        table = reasons()
        for kind in NEW_KINDS:
            for code, (sentence, category) in REGISTRY[kind].REASONS.items():
                self.assertEqual(table[code], (sentence, category))
                self.assertTrue(sentence.endswith('요.') or sentence.endswith('}'), code)
                self.assertLessEqual(len(category), 12)

    def test_samples_reason_is_declared_by_tls(self):
        self.assertIn('CERT_EXPIRING', tls.REASONS)
        valid({'status': 'WARN', 'reason': 'CERT_EXPIRING', 'detail': {'days_left': 12}}, 'tls')


class TlsTests(unittest.TestCase):
    args = {'host': 'console.example.org'}

    def run_with(self, certificate=None, error=None, **extra):
        side = {'side_effect': error} if error else {'return_value': certificate}
        with mock.patch.object(tls, '_handshake', **side) as handshake:
            result = tls.run(dict(self.args, **extra), ctx())
        return valid(result, 'tls'), handshake

    def test_healthy_reports_days_left(self):
        result, handshake = self.run_with(cert(60))
        self.assertEqual((result['status'], result['detail']['days_left']), ('OK', 60))
        handshake.assert_called_once_with('console.example.org', 443, 10)

    def test_warn_and_fail_thresholds(self):
        self.assertEqual(self.run_with(cert(12))[0]['status'], 'WARN')
        self.assertEqual(self.run_with(cert(12))[0]['reason'], 'CERT_EXPIRING')
        result = self.run_with(cert(3))[0]
        self.assertEqual((result['status'], result['reason'], result['detail']), ('FAIL', 'CERT_EXPIRED_SOON', {'days_left': 3}))
        self.assertEqual(self.run_with(cert(-2))[0]['detail'], {'days_left': 0})
        self.assertEqual(self.run_with(cert(25), warn_days=30, fail_days=10)[0]['status'], 'WARN')

    def test_verification_failure_is_invalid(self):
        error = ssl.SSLCertVerificationError('hostname mismatch')
        self.assertEqual(self.run_with(error=error)[0]['reason'], 'CERT_INVALID')

    def test_unreachable_is_fail_not_error(self):
        for error in (socket.timeout(), ConnectionRefusedError(), ssl.SSLError('handshake')):
            result = self.run_with(error=error)[0]
            self.assertEqual((result['status'], result['reason']), ('FAIL', 'TLS_CONNECT'))
        self.assertEqual(self.run_with(error=socket.gaierror())[0]['reason'], 'DNS_FAILED')

    def test_validate(self):
        tls.validate(self.args)
        for bad in ({'host': '192.0.2.1'}, {'host': 'Example.org'}, dict(self.args, warn_days=5, fail_days=7),
                    dict(self.args, port='443'), {}):
            with self.assertRaises((ValueError, KeyError)):
                tls.validate(bad)


class DnsTests(unittest.TestCase):
    def answers(self, *addresses):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 0)) for address in addresses]

    def run_with(self, args, value):
        side = {'side_effect': value} if isinstance(value, BaseException) else {'return_value': value}
        with mock.patch.object(dns.socket, 'getaddrinfo', **side):
            return valid(dns.run(args, ctx()), 'dns')

    def test_resolves_and_matches_expected(self):
        args = {'name': 'llm.example.org', 'expected': ['192.0.2.10']}
        result = self.run_with(args, self.answers('192.0.2.10', '192.0.2.11'))
        self.assertEqual((result['status'], result['detail']), ('OK', {'address_count': 2}))

    def test_unexpected_answer_carries_counts_only(self):
        args = {'name': 'llm.example.org', 'expected': ['192.0.2.10']}
        result = self.run_with(args, self.answers('198.51.100.7'))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'DNS_UNEXPECTED'))
        self.assertEqual(result['detail'], {'address_count': 1, 'missing_count': 1})
        self.assertNotIn('198.51', json.dumps(result))

    def test_resolution_failure_shares_http_text(self):
        result = self.run_with({'name': 'llm.example.org'}, socket.gaierror())
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'DNS_FAILED'))
        self.assertEqual(dns.REASONS['DNS_FAILED'], REGISTRY['http'].REASONS['DNS_FAILED'])
        self.assertEqual(self.run_with({'name': 'llm.example.org'}, [])['reason'], 'DNS_FAILED')

    def test_validate(self):
        dns.validate({'name': 'ssh.example.org', 'expected': ['203.0.113.5']})
        for bad in ({'name': 'ssh.example.org', 'expected': ['not-an-ip']}, {'name': 'ssh.example.org',
                    'expected': '203.0.113.5'}, {'name': '203.0.113.5'}):
            with self.assertRaises(ValueError):
                dns.validate(bad)


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self, limit):
        return self._body[:limit]


class FakeConnection:
    instances = []
    reply = (200, b'{}')
    error = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.sent = None
        FakeConnection.instances.append(self)

    def request(self, method, path, body, headers):
        if FakeConnection.error:
            raise FakeConnection.error
        self.sent = (method, path, json.loads(body), headers)

    def getresponse(self):
        return FakeResponse(*FakeConnection.reply)

    def close(self):
        pass


def completion(content='4', finish='stop', tokens=19):
    return json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content},
                                    'finish_reason': finish}],
                       'usage': {'prompt_tokens': 19, 'completion_tokens': tokens}}).encode()


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Path(self.tmp.name) / 'key'
        self.key.write_text('sk-test-value\n')
        os.chmod(self.key, 0o600)
        self.args = {'url': 'https://llm.example.org/v1/chat/completions', 'model': 'pickle-general',
                     'key_file': str(self.key)}
        FakeConnection.instances, FakeConnection.reply, FakeConnection.error = [], (200, completion()), None
        # Tests run unprivileged, so the root-ownership requirement is relaxed
        # while every other protection of read_protected still applies.
        real = llm_generation.read_protected
        patches = [mock.patch.object(llm_generation.http.client, 'HTTPSConnection', FakeConnection),
                   mock.patch.object(llm_generation.http.client, 'HTTPConnection', FakeConnection),
                   mock.patch.object(llm_generation, 'read_protected',
                                     lambda path, **kw: real(path, **dict(kw, require_root=False)))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_once(self, state=None, now=NOW, **extra):
        return valid(llm_generation.run(dict(self.args, **extra), ctx(state, now=now)), 'llm_generation')

    def test_known_answer_is_healthy_and_request_is_bounded(self):
        result = self.run_once()
        self.assertEqual(result['status'], 'OK')
        self.assertEqual((result['detail']['http_status'], result['detail']['completion_tokens']), (200, 19))
        method, path, body, headers = FakeConnection.instances[0].sent
        self.assertEqual((method, path), ('POST', '/v1/chat/completions'))
        self.assertEqual(body['messages'][0]['content'], 'What is 2+2? Reply with the digit only.')
        self.assertEqual((body['max_completion_tokens'], body['temperature'], body['stream']), (128, 0, False))
        self.assertNotIn('chat_template_kwargs', body)
        self.assertEqual(headers['Authorization'], 'Bearer sk-test-value')
        self.assertNotIn('sk-test', json.dumps(result))

    def test_wrong_empty_and_truncated_answers(self):
        cases = [(completion('5'), 'GENERATION_WRONG_ANSWER'), (completion(''), 'GENERATION_EMPTY'),
                 (completion(None, 'length', 128), 'GENERATION_EMPTY'),
                 (completion('4', 'length'), 'GENERATION_WRONG_ANSWER'),
                 (completion('4', tokens=500), 'GENERATION_WRONG_ANSWER'),
                 (b'{"choices": []}', 'GENERATION_EMPTY'), (b'<html>', 'GENERATION_EMPTY')]
        for index, (body, reason) in enumerate(cases):
            FakeConnection.reply = (200, body)
            result = self.run_once(now=NOW + index * 300)
            self.assertEqual((result['status'], result['reason']), ('FAIL', reason), body)

    def test_http_and_transport_failures(self):
        FakeConnection.reply = (503, b'busy')
        result = self.run_once()
        self.assertEqual((result['status'], result['reason'], result['detail']['http_status']),
                         ('FAIL', 'GENERATION_HTTP', 503))
        FakeConnection.reply = (401, b'')
        self.assertEqual(self.run_once(now=NOW + 300)['status'], 'ERROR')
        for offset, (error, reason) in enumerate(((socket.timeout(), 'GENERATION_TIMEOUT'),
                                                  (ConnectionRefusedError(), 'GENERATION_CONNECT'),
                                                  (socket.gaierror(), 'GENERATION_CONNECT'),
                                                  (ssl.SSLCertVerificationError('x'), 'TLS_INVALID')), start=2):
            FakeConnection.error = error
            result = self.run_once(now=NOW + offset * 300)
            self.assertEqual((result['status'], result['reason']), ('FAIL', reason))

    def test_missing_or_loose_key_is_error_and_spends_nothing(self):
        state = {}
        result = valid(llm_generation.run(dict(self.args, key_file=str(self.key) + '.absent'), ctx(state)),
                       'llm_generation')
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'GENERATION_KEY_MISSING'))
        os.chmod(self.key, 0o644)
        self.assertEqual(self.run_once(state)['reason'], 'GENERATION_KEY_MISSING')
        self.assertEqual((state, FakeConnection.instances), ({}, []))

    def test_daily_budget_and_spacing(self):
        state = {}
        self.assertEqual(self.run_once(state, daily_budget=2)['status'], 'OK')
        # Too soon after the last request, even within the scheduling tolerance window.
        self.assertEqual(self.run_once(state, now=NOW + 60, daily_budget=2)['reason'], 'GENERATION_BUDGET')
        # The agent may run an item a few seconds early; that must still be admitted.
        self.assertEqual(self.run_once(state, now=NOW + 295, daily_budget=2)['status'], 'OK')
        result = self.run_once(state, now=NOW + 600, daily_budget=2)
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'GENERATION_BUDGET'))
        self.assertEqual(len(FakeConnection.instances), 2)
        # A new KST day resets the count.
        self.assertEqual(self.run_once(state, now=NOW + DAY, daily_budget=2)['status'], 'OK')

    def test_clock_moving_backwards_refuses(self):
        state = {}
        self.run_once(state)
        self.assertEqual(self.run_once(state, now=NOW - 3600)['reason'], 'GENERATION_BUDGET')

    def test_local_http_needs_explicit_opt_in(self):
        local = dict(self.args, url='http://127.0.0.1:8000/v1/chat/completions')
        with self.assertRaises(ValueError):
            llm_generation.validate(local)
        llm_generation.validate(dict(local, allow_local_http=True))
        llm_generation.validate(dict(local, url='http://198.18.1.2:8000/v1/chat/completions', allow_local_http=True))
        result = self.run_once(url=local['url'], allow_local_http=True, disable_thinking=True)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual((FakeConnection.instances[0].host, FakeConnection.instances[0].port), ('127.0.0.1', 8000))
        self.assertEqual(FakeConnection.instances[0].sent[2]['chat_template_kwargs'], {'enable_thinking': False})

    def test_validate_refuses_unsafe_targets(self):
        llm_generation.validate(self.args)
        for change in ({'url': 'http://llm.example.org/v1/chat/completions'},
                       {'url': 'https://192.0.2.5/v1/chat/completions'},
                       {'url': 'http://192.0.2.1:8000/v1/chat/completions', 'allow_local_http': True},
                       {'url': 'https://llm.example.org/v1/models'},
                       {'url': 'https://u:p@llm.example.org/v1/chat/completions'},
                       {'max_output_tokens': 512}, {'daily_budget': 0}, {'key_file': 'relative.key'},
                       {'model': 'bad model'}, {'timeout': 120}):
            with self.assertRaises(ValueError, msg=change):
                llm_generation.validate(dict(self.args, **change))


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def settimeout(self, value):
        pass

    def recv(self, size):
        if not self.chunks:
            return b''
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        pass


class SshBannerTests(unittest.TestCase):
    args = {'host': 'ssh.example.org', 'port': 22, 'timeout': 5}

    def run_with(self, value):
        side = {'side_effect': value} if isinstance(value, BaseException) else {'return_value': value}
        with mock.patch.object(ssh_banner.socket, 'create_connection', **side) as connect:
            return valid(ssh_banner.run(self.args, ctx()), 'ssh_banner'), connect

    def test_banner_split_across_reads(self):
        result, connect = self.run_with(FakeSocket([b'SSH-2.0-sshpi', b'per\r\n']))
        self.assertEqual(result['status'], 'OK')
        connect.assert_called_once_with(('ssh.example.org', 22), timeout=5)

    def test_preamble_lines_before_banner_are_allowed(self):
        self.assertEqual(self.run_with(FakeSocket([b'hello\r\nSSH-2.0-x\r\n']))[0]['status'], 'OK')

    def test_bad_or_missing_banner(self):
        for chunks in ([b'SSH-1.5-old\r\n'], [b'HTTP/1.1 400\r\n'], [], [socket.timeout()], [ConnectionResetError()]):
            result = self.run_with(FakeSocket(chunks))[0]
            self.assertEqual((result['status'], result['reason']), ('FAIL', 'SSH_NO_BANNER'), chunks)

    def test_connect_failures(self):
        for error, reason in ((socket.timeout(), 'SSH_TIMEOUT'), (ConnectionRefusedError(), 'SSH_CONNECT'),
                              (socket.gaierror(), 'DNS_FAILED')):
            result = self.run_with(error)[0]
            self.assertEqual((result['status'], result['reason']), ('FAIL', reason))

    def test_validate(self):
        ssh_banner.validate(self.args)
        for bad in ({'host': '127.0.0.1'}, dict(self.args, port=0), dict(self.args, timeout=100)):
            with self.assertRaises(ValueError):
                ssh_banner.validate(bad)


SHOW = ('systemctl', 'show', '--property=ActiveState', '--')


class SystemdUnitTests(unittest.TestCase):
    units = ['haproxy.service', 'relay-agent.service', 'wg-quick@wg0.service']

    def test_all_active(self):
        context = ctx(outputs={SHOW + tuple(self.units): 'ActiveState=active\n\nActiveState=reloading\n\nActiveState=active\n'})
        self.assertEqual(valid(systemd_unit.run({'units': self.units}, context), 'systemd_unit')['status'], 'OK')

    def test_inactive_units_are_named(self):
        context = ctx(outputs={SHOW + tuple(self.units): 'ActiveState=failed\n\nActiveState=active\n\nActiveState=inactive\n'})
        result = valid(systemd_unit.run({'units': self.units}, context), 'systemd_unit')
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'UNIT_INACTIVE'))
        self.assertEqual(result['detail']['failed_units'], ['haproxy.service', 'wg-quick@wg0.service'])

    def test_unparseable_output_is_a_check_error(self):
        context = ctx(outputs={SHOW + tuple(self.units): 'ActiveState=active\n'})
        with self.assertRaises(CheckError):
            systemd_unit.run({'units': self.units}, context)

    def test_validate(self):
        systemd_unit.validate({'units': self.units})
        for bad in ({'units': []}, {'units': ['haproxy']}, {'units': ['a.service', 'a.service']},
                    {'units': ['--now.service']}, {'units': ['x;y.service']}):
            with self.assertRaises(ValueError):
                systemd_unit.validate(bad)


class WireguardTests(unittest.TestCase):
    def outputs(self, handshakes, interfaces='wg0', prefix=()):
        return {prefix + ('wg', 'show', 'interfaces'): interfaces,
                prefix + ('wg', 'show', 'wg0', 'latest-handshakes'): handshakes}

    def run_with(self, outputs, **args):
        context = ctx(outputs=outputs)
        return valid(wireguard.run(dict({'iface': 'wg0'}, **args), context), 'wireguard'), context

    def test_recent_handshake(self):
        result, _ = self.run_with(self.outputs(f'peerA=\t{int(NOW) - 40}\npeerB=\t0'))
        self.assertEqual((result['status'], result['detail']), ('OK', {'handshake_age_s': 40}))

    def test_stale_and_never(self):
        result, _ = self.run_with(self.outputs(f'peerA=\t{int(NOW) - 600}'))
        self.assertEqual((result['status'], result['reason'], result['detail']), ('FAIL', 'WG_STALE', {'handshake_age_s': 600}))
        self.assertEqual(self.run_with(self.outputs('peerA=\t0'))[0]['reason'], 'WG_STALE')
        self.assertEqual(self.run_with(self.outputs(f'peerA=\t{int(NOW) - 600}'), max_age=900)[0]['status'], 'OK')

    def test_missing_interface_or_peer_is_down(self):
        self.assertEqual(self.run_with(self.outputs('', interfaces='wg1'))[0]['reason'], 'WG_DOWN')
        self.assertEqual(self.run_with(self.outputs(''))[0]['reason'], 'WG_DOWN')

    def test_in_container_goes_through_pct(self):
        prefix = ('pct', 'exec', '102', '--')
        result, context = self.run_with(self.outputs(f'peerA=\t{int(NOW) - 10}', prefix=prefix), in_container=102)
        self.assertEqual(result['status'], 'OK')
        self.assertTrue(all(call[:4] == prefix for call in context.calls))

    def test_missing_wg_binary_is_a_check_error(self):
        with self.assertRaises(CheckError):
            wireguard.run({'iface': 'wg0'}, ctx(outputs={}))

    def test_validate(self):
        wireguard.validate({'iface': 'wg0', 'max_age': 180, 'in_container': 102})
        for bad in ({'iface': 'wg0;rm'}, {'iface': 'wg0', 'max_age': 5}, {'iface': 'wg0', 'in_container': '102'}):
            with self.assertRaises(ValueError):
                wireguard.validate(bad)


class AgentIntegrationTests(unittest.TestCase):
    def test_runner_turns_command_failure_into_error(self):
        item = {'id': 'relay-services', 'check': 'systemd_unit', 'args': {'units': ['haproxy.service']}}
        result = run_check(item, ctx(outputs={}), timeout=5)
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'COMMAND_FAILED'))


AWS_ITEMS = {'console-web', 'console-api', 'llm-generation', 'user-ssh', 'tls-console', 'tls-llm',
             'dns-console', 'dns-llm', 'dns-ssh', 'relay-services', 'relay-tunnel', 'staging-web',
             'tls-staging', 'dev-console-web', 'tls-dev-console'}


class CatalogTests(unittest.TestCase):
    def test_aws_items_load_and_validate(self):
        catalog = catalog_mod.load(CATALOG)
        items = catalog['items']
        self.assertTrue(AWS_ITEMS <= set(items), AWS_ITEMS - set(items))
        for item_id in AWS_ITEMS:
            item = items[item_id]
            self.assertEqual(item['host'], 'aws')
            self.assertEqual(item['every'], 300 if item['check'] == 'llm_generation' else 60)
            public = item['group'] == 'user' and item['check'] in ('http', 'llm_generation')
            self.assertEqual(item['fail_after'], 2 if public else 1, item_id)
            if item['group'] == 'nonprod':
                self.assertFalse(item['user_facing'])
            for field in ('target', 'page_label', 'impact', 'what'):
                self.assertNotRegex(item[field], r'\d{1,3}(\.\d{1,3}){3}|CT ?\d|pve\d|dgx', (item_id, field))
            self.assertTrue(item['impact'].endswith('요.'), item_id)

    def test_generation_item_respects_the_canary_bounds(self):
        item = catalog_mod.load(CATALOG)['items']['llm-generation']
        args = item['args']
        self.assertLessEqual(args['max_output_tokens'], 128)
        self.assertLessEqual(args['daily_budget'] * item['every'], 312 * 300)
        self.assertGreaterEqual(args['daily_budget'] * item['every'], DAY)


if __name__ == '__main__':
    os.environ.setdefault('TZ', 'UTC')
    unittest.main(verbosity=1)
