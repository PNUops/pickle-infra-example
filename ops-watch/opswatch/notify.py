"""Discord delivery with the same guarantees the previous notifier proved.

An accepted message is recorded by its message id. A response lost after
submission is UNCERTAIN and is never resent, because a duplicate incident post
is worse than a missing confirmation. A 429 is honoured for the whole webhook.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request

from .util import OpsError, read_protected

WEBHOOK = re.compile(r'https://discord\.com/api/webhooks/[0-9]{16,22}/[A-Za-z0-9_-]{32,256}\Z')
MAX_ATTEMPTS = 3
REQUESTS_PER_RUN = 4


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def load_webhook(path):
    value = read_protected(path, private=True, require_root=False).decode('ascii').strip()
    if not WEBHOOK.fullmatch(value):
        raise OpsError('webhook format is not acceptable')
    return value


def send(webhook, payload, *, opener=None):
    """Return a fixed outcome dict; an exception text could contain the secret URL."""
    if not WEBHOOK.fullmatch(webhook):
        raise OpsError('webhook format is not acceptable')
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    payload = dict(payload, allowed_mentions={'parse': []})
    request = urllib.request.Request(webhook + '?wait=true', data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'PickleOpsWatch/1'},
                                     method='POST')
    try:
        with opener.open(request, timeout=15) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                return {'delivery': 'UNCERTAIN', 'code': 'INVALID_SUCCESS'}
            message = json.loads(body)
            message_id = message.get('id') if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not re.fullmatch(r'[0-9]{16,22}', message_id):
                return {'delivery': 'UNCERTAIN', 'code': 'NO_MESSAGE_ID'}
            return {'delivery': 'SENT', 'code': 'ACKNOWLEDGED', 'message_id': message_id}
    except urllib.error.HTTPError as error:
        try:
            if error.code == 429:
                try:
                    delay = float(json.loads(error.read(65537))['retry_after'])
                    if not 0 <= delay <= 3600:
                        raise ValueError
                    return {'delivery': 'PENDING', 'code': 'RATE_LIMITED', 'retry_after': max(1.0, delay)}
                except (OSError, ValueError, KeyError, TypeError):
                    return {'delivery': 'FAILED', 'code': 'INVALID_RATE_LIMIT'}
            if 400 <= error.code < 500:
                return {'delivery': 'FAILED', 'code': 'REJECTED'}
            return {'delivery': 'UNCERTAIN', 'code': 'HTTP_UNCONFIRMED'}
        finally:
            error.close()
    except urllib.error.URLError as error:
        # A name that did not resolve or a refused connection proves nothing was
        # sent, so it is retried. Anything after the connection opened may have
        # reached Discord and is never resent.
        if isinstance(error.reason, (socket.gaierror, ConnectionRefusedError)):
            return {'delivery': 'PENDING', 'code': 'NOT_CONNECTED'}
        return {'delivery': 'UNCERTAIN', 'code': 'RESPONSE_UNKNOWN'}
    except (OSError, ValueError, TypeError):
        return {'delivery': 'UNCERTAIN', 'code': 'RESPONSE_UNKNOWN'}


def dispatch(store, webhook, now, *, sender=send):
    """Send pending messages, at most a few requests per run."""
    for row in store.outbox(['ATTEMPTED']):
        store.update_outbox(row['id'], delivery='UNCERTAIN', code='INTERRUPTED')
    if (store.meta('webhook_retry_after') or 0) > now:
        return 0
    sent = 0
    requests = 0
    for row in store.outbox(['PENDING']):
        if row['next_attempt'] > now:
            continue
        if requests >= REQUESTS_PER_RUN:
            break
        attempts = row['attempts'] + 1
        store.update_outbox(row['id'], delivery='ATTEMPTED', attempts=attempts)
        requests += 1
        try:
            outcome = sender(webhook, json.loads(row['payload']))
        except Exception:  # noqa: BLE001 - never let a transport error leak the URL or replay a send
            outcome = {'delivery': 'UNCERTAIN', 'code': 'SENDER_INTERRUPTED'}
        delivery = outcome['delivery']
        fields = {'delivery': delivery, 'code': outcome.get('code'), 'message_id': outcome.get('message_id')}
        if outcome.get('code') == 'RATE_LIMITED':
            delay = outcome.get('retry_after', 60)
            store.set_meta('webhook_retry_after', now + delay)
            # A rate limit is a known rejection, not a failed attempt.
            store.update_outbox(row['id'], attempts=attempts - 1, next_attempt=now + delay, **fields)
            break
        if delivery == 'PENDING':
            if attempts >= MAX_ATTEMPTS:
                fields.update(delivery='FAILED', code='RETRIES_EXHAUSTED')
            store.update_outbox(row['id'], next_attempt=now + (60 if attempts == 1 else 300), **fields)
            continue
        store.update_outbox(row['id'], **fields)
        sent += delivery == 'SENT'
    return sent
