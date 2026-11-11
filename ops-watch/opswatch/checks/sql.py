"""Read-only questions to the production database, chosen by name.

The catalog picks a query from QUERIES and passes numbers; it never supplies
SQL. Every query runs through psql as the postgres OS user inside the database
container, the same path the old production health snapshot used.
"""
from __future__ import annotations

import re

from . import CheckError, _pve, bad, ok

KIND = 'sql'
DETAIL = {
    'count': int, 'workers': int, 'stale': [str], 'requests': int, 'failed': int, 'ratio_pct': int,
    'oldest_min': int, 'balance_usd': float, 'usage_pct': int, 'snapshot_age_min': int,
}
REASONS = {
    'DB_UNREACHABLE': ('데이터베이스에 접속하지 못했어요.', '데이터베이스 장애'),
    'MIGRATION_FAILED': ('실패한 데이터베이스 마이그레이션이 {count}건 있어요.', '배포 이상'),
    'JOBRUNR_DOWN': ('백그라운드 작업 실행기가 응답하지 않아요. 예약 작업이 모두 멈춰요.', '작업 중단'),
    'JOBRUNR_EXTRA': ('백그라운드 작업 실행기가 {workers}개 동시에 떠 있어요.', '작업 이상'),
    'JOB_STALE': ('다음 예약 작업이 제때 끝나지 않았어요: {stale}', '작업 지연'),
    'JOB_FAILED_RECENT': ('최근 재시도까지 실패한 백그라운드 작업이 {count}건 있어요.', '작업 실패'),
    'LLM_FAILURE_RATE': ('최근 LLM 요청 {requests}건 중 {failed}건({ratio_pct}%)이 실패하거나 시간 초과됐어요.', 'LLM 오류'),
    'CONTACT_LOST': ('다음 구성 요소와 연락이 끊겼어요: {stale}', '연결 끊김'),
    'MAIL_BACKLOG': ('보내지 못한 메일 {count}통이 밀려 있어요. 가장 오래된 메일은 {oldest_min}분째 기다리고 있어요.', '메일 지연'),
    'OPENROUTER_KEY_EXHAUSTED': ('공용 LLM 키가 오늘 사용 한도를 다 썼어요.', 'LLM 한도'),
    'OPENROUTER_KEY_HIGH': ('공용 LLM 키가 오늘 한도의 {usage_pct}%를 썼어요.', 'LLM 한도'),
    'OPENROUTER_BALANCE_LOW': ('OpenRouter 계정 잔액이 {balance_usd:.2f}달러 남았어요.', 'LLM 잔액'),
    'OPENROUTER_SNAPSHOT_STALE': ('OpenRouter 잔액 정보가 {snapshot_age_min}분째 갱신되지 않았어요.', 'LLM 잔액'),
}
DATABASE = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
JOB_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
RELAY_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,30}\Z')
MAX_STALE = 15

# JobRunr stores its timestamps as UTC without a zone.
UTC_NOW = "(now() at time zone 'utc')"
# Requests the platform owes an answer to. A refused key, a malformed body, a
# rate limit or a client that hung up is the caller's outcome, not an outage.
SERVED = "('OK', 'UPSTREAM_ERROR', 'TIMEOUT')"
FAILED = "('UPSTREAM_ERROR', 'TIMEOUT')"


def _int(args, key, default, low, high):
    return _pve.number(args.get(key, default), key, low, high)


def _pair(args, warn_key, warn_default, fail_key, fail_default, low, high):
    warn = _int(args, warn_key, warn_default, low, high)
    fail = _int(args, fail_key, fail_default, low, high)
    if warn > fail:
        raise ValueError(f'{warn_key} must not exceed {fail_key}')


def _validate_recurring(args):
    cadence = args['cadence']
    if not isinstance(cadence, dict) or not cadence or len(cadence) > 64:
        raise ValueError('cadence must map job ids to seconds')
    for job, seconds in cadence.items():
        if not JOB_ID.fullmatch(job):
            raise ValueError('cadence job id')
        _pve.number(seconds, 'cadence seconds', 10, 2592000)
    _int(args, 'min_limit', 300, 0, 86400)


def _validate_openrouter(args):
    _pve.number(args.get('balance_warn_usd', 20), 'balance_warn_usd', 0, 100000, (int, float))
    _int(args, 'stale_min', 30, 1, 1440)
    _pair(args, 'usage_warn_pct', 80, 'usage_fail_pct', 100, 1, 1000)
    keys = args.get('key_ids', [])
    if not isinstance(keys, list) or len(keys) > 8 or any(not isinstance(k, str) or not UUID.fullmatch(k) for k in keys):
        raise ValueError('key_ids must be key public ids')


VALIDATORS = {
    'read': lambda args: None,
    'migrations': lambda args: None,
    'jobrunr_heartbeat': lambda args: (_int(args, 'heartbeat_s', 90, 15, 3600), _int(args, 'max_workers', 1, 1, 16)),
    'recurring_jobs': _validate_recurring,
    'failed_jobs': lambda args: _int(args, 'window_min', 15, 1, 1440),
    'llm_failure_rate': lambda args: (
        _int(args, 'window_min', 10, 1, 120), _int(args, 'min_requests', 10, 1, 100000),
        _pve.number(args.get('fail_ratio', 0.2), 'fail_ratio', 0.01, 1, (int, float)),
        _pve.number(args.get('warn_ratio', args.get('fail_ratio', 0.2)), 'warn_ratio', 0.01,
                    args.get('fail_ratio', 0.2), (int, float))),
    'contacts': lambda args: (_int(args, 'gateway_max_age_s', 180, 30, 86400),
                              _int(args, 'route_pending_min', 15, 1, 1440)),
    'mail_backlog': lambda args: _pair(args, 'warn_min', 15, 'fail_min', 60, 1, 10080),
    'openrouter': _validate_openrouter,
}


def validate(args):
    _pve.ct_id(args['ct'])
    if not isinstance(args['database'], str) or not DATABASE.fullmatch(args['database']):
        raise ValueError('database name')
    query = args['query']
    if query not in VALIDATORS:
        raise ValueError('query must be one of ' + ', '.join(sorted(VALIDATORS)))
    VALIDATORS[query](args)


def sql(ctx, args, query, timeout=15):
    return ctx.pct(args['ct'], ['runuser', '-u', 'postgres', '--', 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
                                '-tA', '-d', args['database'], '-c', query], timeout=timeout)


def rows(text, width):
    out = [line.split('|') for line in text.splitlines() if line]
    if any(len(row) != width for row in out):
        raise CheckError('CHECK_CRASHED')
    return out


def read(args, ctx):
    try:
        answer = sql(ctx, args, 'select 1', timeout=10)
    except CheckError:
        # Either psql could not connect or the container is gone; both mean the
        # database is not serving, which is the thing this item exists to say.
        return bad('FAIL', 'DB_UNREACHABLE')
    return ok() if answer == '1' else bad('FAIL', 'DB_UNREACHABLE')


def migrations(args, ctx):
    count = _pve.as_int(sql(ctx, args, 'select count(*) from flyway_schema_history where not success'))
    return bad('FAIL', 'MIGRATION_FAILED', count=count) if count else ok(count=0)


def jobrunr_heartbeat(args, ctx):
    seconds = args.get('heartbeat_s', 90)
    workers = _pve.as_int(sql(ctx, args, 'select count(*) from jobrunr_backgroundjobservers '
                                         f"where lastheartbeat > {UTC_NOW} - interval '{seconds} seconds'"))
    if workers == 0:
        return bad('FAIL', 'JOBRUNR_DOWN', workers=0)
    if workers > args.get('max_workers', 1):
        # Two workers is normal for the minute a deploy overlaps; fail_after absorbs that.
        return bad('WARN', 'JOBRUNR_EXTRA', workers=workers)
    return ok(workers=workers)


def recurring_jobs(args, ctx):
    """Every recurring job has a SUCCEEDED run within three of its cadences.

    JobRunr keeps SUCCEEDED rows for 36 hours, so a job slower than that has no
    row to read most of the time and cannot be in the cadence list.
    """
    out = sql(ctx, args, "select recurringjobid, floor(extract(epoch from "
                         f"({UTC_NOW} - max(updatedat))))::bigint from jobrunr_jobs "
                         "where state = 'SUCCEEDED' and recurringjobid is not null group by recurringjobid")
    ages = {job: _pve.as_int(age) for job, age in rows(out, 2)}
    floor = args.get('min_limit', 300)
    stale = sorted(job for job, cadence in args['cadence'].items()
                   if job not in ages or ages[job] > max(3 * cadence, floor))
    if stale:
        shown = stale if len(stale) <= MAX_STALE else stale[:MAX_STALE - 1] + [f'외 {len(stale) - MAX_STALE + 1}건']
        return bad('FAIL', 'JOB_STALE', stale=shown, count=len(stale))
    return ok(count=0)


def failed_jobs(args, ctx):
    minutes = args.get('window_min', 15)
    count = _pve.as_int(sql(ctx, args, "select count(*) from jobrunr_jobs where state = 'FAILED' "
                                       f"and updatedat > {UTC_NOW} - interval '{minutes} minutes'"))
    return bad('WARN', 'JOB_FAILED_RECENT', count=count) if count else ok(count=0)


def llm_failure_rate(args, ctx):
    minutes = args.get('window_min', 10)
    out = sql(ctx, args, f'select count(*) filter (where status in {SERVED}), '
                         f'count(*) filter (where status in {FAILED}) from llm_usage_events '
                         f"where requested_at > now() - interval '{minutes} minutes'")
    (requests, failed), = [[_pve.as_int(v) for v in row] for row in rows(out, 2)]
    ratio = failed / requests if requests else 0.0
    detail = {'requests': requests, 'failed': failed, 'ratio_pct': int(round(ratio * 100))}
    if requests < args.get('min_requests', 10):
        return ok(**detail)
    if ratio > args.get('fail_ratio', 0.2):
        return bad('FAIL', 'LLM_FAILURE_RATE', **detail)
    if ratio > args.get('warn_ratio', args.get('fail_ratio', 0.2)):
        return bad('WARN', 'LLM_FAILURE_RATE', **detail)
    return ok(**detail)


def contacts(args, ctx):
    """The gateway's last poll, relays the api has marked lost, and route pushes left pending.

    No table records proxy-agent contact. A route the agent has not applied for
    a while is the only database trace of it being unreachable, so a PENDING
    route older than route_pending_min stands in for that contact.
    """
    gateway_age = args.get('gateway_max_age_s', 180)
    pending = args.get('route_pending_min', 15)
    out = sql(ctx, args,
              "select 'gateway', '', '' from llm_gateway_state where contact_lost_since is not null "
              f"or last_contact_at is null or last_contact_at < now() - interval '{gateway_age} seconds' "
              "union all select 'relay', id::text, case when name ~ '^[a-z0-9][a-z0-9-]{0,30}$' "
              "then name else '' end from relays "
              "where enabled and contact_lost_since is not null "
              "union all select 'routes', '', '' where exists (select 1 from routes where status = 'PENDING' "
              f"and updated_at < now() - interval '{pending} minutes')")
    stale = []
    for kind, relay_id, name in rows(out, 3):
        if kind == 'gateway':
            stale.append('LLM 게이트웨이')
        elif kind == 'relay':
            # The query already blanks a name that is not a plain label.
            stale.append('중계 서버 ' + (name if RELAY_NAME.fullmatch(name) else 'relay-' + relay_id))
        elif kind == 'routes':
            stale.append('프록시 에이전트')
        else:
            raise CheckError('CHECK_CRASHED')
    if stale:
        return bad('FAIL', 'CONTACT_LOST', stale=stale[:MAX_STALE])
    return ok()


def mail_backlog(args, ctx):
    out = sql(ctx, args, "select count(*), coalesce(floor(extract(epoch from now() - min(created_at)) / 60), 0)"
                         "::bigint from mail_deliveries where state in ('PENDING', 'SENDING')")
    (count, oldest), = [[_pve.as_int(v) for v in row] for row in rows(out, 2)]
    if count and oldest >= args.get('fail_min', 60):
        return bad('FAIL', 'MAIL_BACKLOG', count=count, oldest_min=oldest)
    if count and oldest >= args.get('warn_min', 15):
        return bad('WARN', 'MAIL_BACKLOG', count=count, oldest_min=oldest)
    return ok(count=count, oldest_min=oldest)


def _number(text):
    if not re.fullmatch(r'-?\d{1,12}(\.\d{1,12})?', text):
        raise CheckError('CHECK_CRASHED')
    return float(text)


def openrouter(args, ctx):
    """Business-account balance, its poll freshness, and watched runtime keys' daily use.

    An account that has never been read counts from its creation, so a new
    account warns only if its first poll is late too.
    """
    out = sql(ctx, args, "select count(*), coalesce(min(credits_total - credits_usage)::text, ''), "
                         "coalesce(max(floor(extract(epoch from now() - coalesce(credits_observed_at, created_at)) "
                         "/ 60))::bigint::text, '') from openrouter_accounts where status = 'ACTIVE'")
    (accounts, balance, age), = rows(out, 3)
    detail, found = {}, []
    if _pve.as_int(accounts):
        if balance:
            detail['balance_usd'] = round(_number(balance), 2)
            if detail['balance_usd'] < args.get('balance_warn_usd', 20):
                found.append(('WARN', 'OPENROUTER_BALANCE_LOW'))
        detail['snapshot_age_min'] = _pve.as_int(age)
        if detail['snapshot_age_min'] >= args.get('stale_min', 30):
            found.append(('WARN', 'OPENROUTER_SNAPSHOT_STALE'))
    keys = args.get('key_ids', [])
    if keys:
        listed = ', '.join(f"'{key}'" for key in keys)
        usage = sql(ctx, args, "select coalesce(max(ceil(100 * (credit_limit - openrouter_limit_remaining) "
                               "/ credit_limit))::bigint::text, '') from llm_api_keys "
                               f"where public_id in ({listed}) and credit_limit > 0 "
                               "and openrouter_limit_remaining is not null")
        if usage:
            detail['usage_pct'] = _pve.as_int(usage)
            if detail['usage_pct'] >= args.get('usage_fail_pct', 100):
                found.insert(0, ('FAIL', 'OPENROUTER_KEY_EXHAUSTED'))
            elif detail['usage_pct'] >= args.get('usage_warn_pct', 80):
                found.insert(0, ('WARN', 'OPENROUTER_KEY_HIGH'))
    if found:
        return bad(found[0][0], found[0][1], **detail)
    return ok(**detail)


QUERIES = {
    'read': read, 'migrations': migrations, 'jobrunr_heartbeat': jobrunr_heartbeat,
    'recurring_jobs': recurring_jobs, 'failed_jobs': failed_jobs, 'llm_failure_rate': llm_failure_rate,
    'contacts': contacts, 'mail_backlog': mail_backlog, 'openrouter': openrouter,
}


def run(args, ctx):
    return QUERIES[args['query']](args, ctx)
