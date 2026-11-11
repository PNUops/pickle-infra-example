"""Judge state in one SQLite file."""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
create table if not exists meta (key text primary key, value text not null);
create table if not exists items (
  id text primary key,
  state text not null,            -- last announced condition: OK, WARN, FAIL
  fails integer not null,         -- consecutive non-OK new samples
  since real,                     -- when the current non-OK condition began
  reason text,
  detail text,
  last_sample real,               -- host clock, used only for ordering
  seen_at real,                   -- judge clock when the newest sample arrived
  unobserved text                 -- announced observation problem reason, or null
);
create table if not exists hosts (name text primary key, state text not null, since real);
create table if not exists incidents (
  id integer primary key, item text not null, kind text not null,
  started real not null, ended real, reason text
);
create table if not exists daily (
  item text not null, day text not null,
  up real not null default 0, warn real not null default 0,
  down real not null default 0, unknown real not null default 0,
  primary key (item, day)
);
create table if not exists outbox (
  id integer primary key, created real not null, payload text not null,
  delivery text not null, attempts integer not null, next_attempt real not null,
  code text, message_id text
);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('pragma journal_mode=wal')
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def begin(self):
        self.db.execute('begin immediate')

    def commit(self):
        self.db.execute('commit')

    def rollback(self):
        self.db.execute('rollback')

    def meta(self, key, default=None):
        row = self.db.execute('select value from meta where key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        self.db.execute('insert into meta values (?, ?) on conflict(key) do update set value=excluded.value',
                        (key, json.dumps(value)))

    def item(self, item_id):
        row = self.db.execute('select * from items where id=?', (item_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['detail'] = json.loads(value['detail'] or '{}')
        return value

    def items(self):
        return {row['id']: self.item(row['id']) for row in self.db.execute('select id from items').fetchall()}

    def save_item(self, value):
        self.db.execute(
            'insert into items values (:id,:state,:fails,:since,:reason,:detail,:last_sample,:seen_at,:unobserved) '
            'on conflict(id) do update set state=excluded.state, fails=excluded.fails, since=excluded.since, '
            'reason=excluded.reason, detail=excluded.detail, last_sample=excluded.last_sample, '
            'seen_at=excluded.seen_at, unobserved=excluded.unobserved',
            dict(value, detail=json.dumps(value.get('detail') or {}, sort_keys=True)))

    def drop_items(self, keep, now):
        for row in self.db.execute('select id from items').fetchall():
            if row['id'] not in keep:
                self.db.execute('delete from items where id=?', (row['id'],))
                # An item removed from the catalog ends its open incident, so a
                # later item with the same id does not inherit it.
                self.close_incident(row['id'], now)

    def host(self, name):
        row = self.db.execute('select * from hosts where name=?', (name,)).fetchone()
        return dict(row) if row else None

    def save_host(self, name, state, since):
        self.db.execute('insert into hosts values (?,?,?) on conflict(name) do update set '
                        'state=excluded.state, since=excluded.since', (name, state, since))

    def open_incident(self, item, kind, started, reason):
        self.db.execute('insert into incidents (item, kind, started, reason) values (?,?,?,?)',
                        (item, kind, started, reason))

    def close_incident(self, item, ended):
        self.db.execute('update incidents set ended=? where item=? and ended is null', (ended, item))

    def recent_incidents(self, since, limit=50):
        return [dict(row) for row in self.db.execute(
            'select * from incidents where started>=? or ended is null order by started desc limit ?',
            (since, limit)).fetchall()]

    def add_daily(self, item, day, bucket, seconds):
        assert bucket in ('up', 'warn', 'down', 'unknown')
        self.db.execute(f'insert into daily (item, day, {bucket}) values (?,?,?) '
                        f'on conflict(item, day) do update set {bucket}={bucket}+excluded.{bucket}',
                        (item, day, seconds))

    def daily(self, first_day):
        return [dict(row) for row in self.db.execute(
            'select * from daily where day>=? order by day', (first_day,)).fetchall()]

    def prune(self, first_day, incidents_before):
        self.db.execute('delete from daily where day<?', (first_day,))
        self.db.execute('delete from incidents where ended is not null and ended<?', (incidents_before,))
        self.db.execute("delete from outbox where delivery in ('SENT','SUPERSEDED') and id not in "
                        "(select id from outbox order by id desc limit 200)")

    def enqueue(self, payload, now):
        self.db.execute("insert into outbox (created, payload, delivery, attempts, next_attempt) "
                        "values (?,?,'PENDING',0,?)", (now, json.dumps(payload, ensure_ascii=False), now))

    def outbox(self, deliveries):
        marks = ','.join('?' * len(deliveries))
        return [dict(row) for row in self.db.execute(
            f'select * from outbox where delivery in ({marks}) order by id', tuple(deliveries)).fetchall()]

    def update_outbox(self, row_id, **fields):
        assignments = ', '.join(f'{key}=?' for key in fields)
        self.db.execute(f'update outbox set {assignments} where id=?', (*fields.values(), row_id))

    def undelivered(self, since):
        return self.db.execute("select count(*) from outbox where delivery in ('FAILED','UNCERTAIN') and created>=?",
                               (since,)).fetchone()[0]
