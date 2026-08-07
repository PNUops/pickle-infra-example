#!/usr/bin/env bash
# Publishes this deployment's legal documents — terms of service and privacy
# policy — into the application database from the files under data/terms/.
#
# Why this is a script and not a migration: the text is this deployment's own
# legal content, not schema. A migration carrying it makes one organisation's
# wording part of the product, and revising a comma then costs another
# migration — which is exactly what happened before this script existed: one
# insert, then two more migrations that did nothing but reword the titles in
# place. Here a revision is an edit to a file and a re-run.
#
# There is deliberately no default document and no empty placeholder. Signing up
# requires consenting to the current version, so with no rows the service will
# not let anybody register — which is the correct failure for a service whose
# terms have not been published. A blank document would instead collect consent
# to nothing, which is worse than refusing.
#
# What a re-run does:
#   new (doc_type, version)     published
#   same version, same text     left alone
#   same version, CHANGED text  Depends on whether anyone can be bound by the
#                               published row. A version that is still pending
#                               (effective_at in the future) and has no consent
#                               records binds nobody — the application only
#                               collects consent to the version in force — so a
#                               mistake in it (a wrong effective date, a typo)
#                               is revised in place. Once it is in force, or
#                               the moment a consent row exists, it is REFUSED:
#                               changing consented wording underneath users
#                               makes the consent record describe a document
#                               that no longer exists. Publish a new version
#                               instead — add a file with the next version
#                               number, which is what re-consent is built on.
#
# File format — a short header, a `---` line, then the document in Markdown:
#
#   doc_type: TERMS_OF_SERVICE        TERMS_OF_SERVICE | PRIVACY_POLICY
#   version: 1                        integer, one higher for each revision
#   title: …                          shown above the document in the console
#   effective_at: 2026-07-30T09:51:47+09:00
#   ---
#   # …the document…
#
# effective_at is a real timestamp on purpose. The application treats the
# current version as the highest one whose effective_at has passed, so a future
# date publishes a revision that takes effect on its own; the migration this
# replaced used now(), which meant "whenever the schema happened to be applied".
#
# Usage:
#   bash scripts/apply-terms.sh
#
# Environment:
#   PICKLE_APP_CTID   101      container running PostgreSQL + the api
#   PICKLE_DB         pickle_dev
#   PICKLE_DATA_DIR   <repo>/data
set -euo pipefail

CTID="${PICKLE_APP_CTID:-101}"
DB="${PICKLE_DB:-pickle_dev}"
DATA_DIR="${PICKLE_DATA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data}"
TERMS_DIR="$DATA_DIR/terms"

# The database name is the one value that rides INSIDE the command string the
# second shell below re-parses, so it is pinned to bare-word characters here.
# This also turns a plain typo ("pickle dev") into a readable refusal instead
# of an incomprehensible psql failure.
case "$DB" in
  ''|*[!a-zA-Z0-9_]*)
    echo "PICKLE_DB must be a plain database name ([A-Za-z0-9_]), got: '$DB'" >&2
    exit 1 ;;
esac

# Statements are fed on STDIN, never as `psql -c "…"`: the documents are full of
# quotes and $ characters that the second shell `su -c` spawns would eat. The
# database name is the only piece spliced into that command string, and it is
# validated to bare-word characters above.
pgq() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'query failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
pgtx() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'write failed, nothing committed:\n%s\n' "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

header_value() { # file key
  sed -n "1,/^---$/p" "$1" | sed -n "s/^$2: *//p" | head -1
}

# Parses one document into these globals. They are globals rather than a packed
# string because the body is multi-line: `read` stops at the first newline, so
# anything that funnels a document through one would silently keep only its
# first line — and a truncated body still compares equal to itself, which
# disarms the change check below as well.
DOC_TYPE=; DOC_VERSION=; DOC_TITLE=; DOC_EFFECTIVE=; DOC_BODY=
parse_doc() { # file
  local f="$1"
  # CRLF first: every line-matching check below expects LF, so `---\r` would
  # fail the separator check and the error would then name a problem the file
  # does not have. Say what is actually wrong instead.
  if grep -q $'\r' "$f"; then
    echo "  $f: has CRLF line endings; convert to LF (e.g. dos2unix) and re-run" >&2
    return 1
  fi
  grep -qx -- '---' "$f" || { echo "  $f: no --- separator" >&2; return 1; }
  # A field that appears twice would silently resolve to its first value
  # (header_value keeps one line), so two headers disagreeing about, say, the
  # version must be an error, not a coin toss.
  local key count
  for key in doc_type version title effective_at; do
    count=$(sed -n "1,/^---$/p" "$f" | grep -c "^$key:" || true)
    [ "$count" -le 1 ] || { echo "  $f: header field $key appears $count times" >&2; return 1; }
  done
  DOC_TYPE=$(header_value "$f" doc_type)
  DOC_VERSION=$(header_value "$f" version)
  DOC_TITLE=$(header_value "$f" title)
  DOC_EFFECTIVE=$(header_value "$f" effective_at)
  DOC_BODY=$(sed '1,/^---$/d' "$f")
  # Substance is checked BEFORE the newline normalisation below: the appended
  # newline would make any plain emptiness test pass forever, and a document
  # that is only whitespace is as empty as one with nothing at all. A blank
  # document would collect consent to nothing, which is worse than refusing.
  case "$DOC_BODY" in
    *[![:space:]]*) ;;
    *) echo "  $f: the document body is empty (or whitespace only)" >&2; return 1 ;;
  esac
  # Command substitution strips every trailing newline, so the body is
  # normalised back to exactly one. Without this the stored document loses the
  # final newline the file has, and each re-run would compare a body that ends
  # one byte short against the one it just published.
  DOC_BODY+=$'\n'

  local pair
  for pair in "doc_type:$DOC_TYPE" "version:$DOC_VERSION" \
              "title:$DOC_TITLE" "effective_at:$DOC_EFFECTIVE"; do
    [ -n "${pair#*:}" ] || { echo "  $f: header field ${pair%%:*} is missing" >&2; return 1; }
  done
  case "$DOC_TYPE" in
    TERMS_OF_SERVICE|PRIVACY_POLICY) ;;
    *) echo "  $f: unknown doc_type $DOC_TYPE" >&2; return 1 ;;
  esac
  case "$DOC_VERSION" in
    ''|*[!0-9]*) echo "  $f: version must be an integer, got $DOC_VERSION" >&2; return 1 ;;
  esac
}

echo "== read the documents"
[ -d "$TERMS_DIR" ] || { echo "  no such directory: $TERMS_DIR" >&2; exit 1; }
mapfile -t FILES < <(find "$TERMS_DIR" -maxdepth 1 -name '*.md' | sort)
[ "${#FILES[@]}" -gt 0 ] || { echo "  no documents in $TERMS_DIR" >&2; exit 1; }

for f in "${FILES[@]}"; do
  parse_doc "$f" || exit 1
  echo "  $(basename "$f"): $DOC_TYPE v$DOC_VERSION," \
       "$(printf '%s' "$DOC_BODY" | wc -c) bytes, effective $DOC_EFFECTIVE"
done

echo "== compare against what is already published"
publish=(); revise=()
for f in "${FILES[@]}"; do
  parse_doc "$f" || exit 1
  dt=$(sql_escape "$DOC_TYPE")
  ef=$(sql_escape "$DOC_EFFECTIVE")
  file_digest=$(printf '%s' "$DOC_BODY" | sha256sum | cut -d' ' -f1)
  existing=$(pgq "select count(*) from terms_versions
                   where doc_type = '$dt' and version = $DOC_VERSION;")
  if [ "$existing" = "0" ]; then
    # A new version is measured against the newest published one of its type.
    prev_version=$(pgq "select coalesce(max(version), 0) from terms_versions
                         where doc_type = '$dt';")
    if [ "$prev_version" != "0" ]; then
      prev_digest=$(pgq "select encode(sha256(convert_to(body, 'UTF8')), 'hex')
                           from terms_versions
                          where doc_type = '$dt' and version = $prev_version;")
      prev_title=$(pgq "select title from terms_versions
                         where doc_type = '$dt' and version = $prev_version;")
      prev_revisable=$(pgq "select case when v.effective_at > now()
                                    and not exists (select 1 from user_consents c
                                                     where c.terms_version_id = v.id)
                                   then 't' else 'f' end
                              from terms_versions v
                             where v.doc_type = '$dt' and v.version = $prev_version;")
      if [ "$file_digest" = "$prev_digest" ] && [ "$DOC_TITLE" = "$prev_title" ]; then
        echo "  $DOC_TYPE v$DOC_VERSION is byte-identical to published v$prev_version." >&2
        echo "  A new version exists to change the document; publishing an identical one" >&2
        echo "  only forces every user to re-consent to text that has not changed." >&2
        if [ "$prev_revisable" = "t" ]; then
          echo "  (If this copy exists to fix v$prev_version's effective_at: v$prev_version is still" >&2
          echo "  pending with no consents, so edit the v$prev_version file itself — this script" >&2
          echo "  revises a pending version in place.)" >&2
        fi
        exit 1
      fi
      # Ordering is checked against the newest version anyone can be BOUND by —
      # one already in force, or one holding consent records. The application
      # takes the current version as the highest whose effective_at has passed,
      # so a new version dated at or before that one goes into force the
      # instant it lands and drops every user into re-consent. A pending,
      # consent-free version does not anchor this check: nobody is bound by
      # it, and requiring a correction to land AFTER a mistakenly far-future
      # date would make the mistake unfixable.
      anchor_version=$(pgq "select coalesce(
                              (select v.version
                                 from terms_versions v
                                where v.doc_type = '$dt'
                                  and (v.effective_at <= now()
                                       or exists (select 1 from user_consents c
                                                   where c.terms_version_id = v.id))
                                order by v.effective_at desc, v.version desc
                                limit 1), 0);")
      if [ "$anchor_version" != "0" ]; then
        anchor_effective=$(pgq "select effective_at from terms_versions
                                 where doc_type = '$dt' and version = $anchor_version;")
        is_later=$(pgq "select case when '$ef'::timestamptz > effective_at
                               then 't' else 'f' end
                          from terms_versions
                         where doc_type = '$dt' and version = $anchor_version;")
        if [ "$is_later" != "t" ]; then
          echo "  $DOC_TYPE v$DOC_VERSION: effective_at $DOC_EFFECTIVE is not after" >&2
          echo "  v$anchor_version's ($anchor_effective) — the newest version that is in force" >&2
          echo "  or has consent records." >&2
          echo "  A revision must take effect after the version users are bound by — set a" >&2
          echo "  later effective_at (a future one takes effect on its own)." >&2
          exit 1
        fi
      fi
      # A pending, consent-free version dated at or after this new one will be
      # shadowed: the current version is the highest whose effective_at has
      # passed, so once the new version is in force the older number never
      # gets its turn. That is the intended way to retire a mispublished
      # pending version, but it should happen out loud.
      while IFS='|' read -r sv se; do
        [ -n "$sv" ] || continue
        echo "  note: $DOC_TYPE v$sv (pending, effective $se, no consents) will be"
        echo "  superseded by v$DOC_VERSION before taking effect and will never be in force"
      done <<<"$(pgq "select v.version || '|' || v.effective_at
                        from terms_versions v
                       where v.doc_type = '$dt' and v.version < $DOC_VERSION
                         and v.effective_at >= '$ef'::timestamptz
                         and v.effective_at > now()
                         and not exists (select 1 from user_consents c
                                          where c.terms_version_id = v.id)
                       order by v.version;")"
    fi
    echo "  $DOC_TYPE v$DOC_VERSION: new, will publish"
    publish+=("$f")
    continue
  fi
  # Compare by digest rather than by hauling the document back out and diffing
  # it through two shells. effective_at is compared in SQL so the file's
  # spelling of the timestamp (offset, T separator) does not matter.
  db_digest=$(pgq "select encode(sha256(convert_to(body, 'UTF8')), 'hex')
                     from terms_versions
                    where doc_type = '$dt' and version = $DOC_VERSION;")
  db_title=$(pgq "select title from terms_versions
                   where doc_type = '$dt' and version = $DOC_VERSION;")
  db_effective=$(pgq "select effective_at from terms_versions
                       where doc_type = '$dt' and version = $DOC_VERSION;")
  eff_same=$(pgq "select case when effective_at = '$ef'::timestamptz
                         then 't' else 'f' end
                    from terms_versions
                   where doc_type = '$dt' and version = $DOC_VERSION;")
  if [ "$file_digest" = "$db_digest" ] && [ "$db_title" = "$DOC_TITLE" ] \
     && [ "$eff_same" = "t" ]; then
    echo "  $DOC_TYPE v$DOC_VERSION: already published, unchanged"
    continue
  fi
  # The file differs from the published row. Whether that row may still be
  # rewritten hinges on whether anyone can be bound by it. The application
  # collects consent only to the version in force (highest effective_at that
  # has passed), so a version that is still pending AND has no consent rows
  # binds nobody — refusing to fix it would leave a mispublished date or typo
  # with no exit short of hand-written SQL. Both conditions are checked here
  # and re-checked inside the UPDATE itself, so a version that goes into force
  # or gains a consent mid-run cannot be rewritten by the race.
  consents=$(pgq "select count(*) from user_consents c
                   join terms_versions v on v.id = c.terms_version_id
                  where v.doc_type = '$dt' and v.version = $DOC_VERSION;")
  is_pending=$(pgq "select case when effective_at > now() then 't' else 'f' end
                      from terms_versions
                     where doc_type = '$dt' and version = $DOC_VERSION;")
  if [ "$is_pending" = "t" ] && [ "$consents" = "0" ]; then
    # The corrected effective_at still has to respect the ordering rule new
    # versions follow: after the newest version that is in force or has
    # consent records, so the revision cannot slide under consented history.
    anchor_version=$(pgq "select coalesce(
                            (select v.version
                               from terms_versions v
                              where v.doc_type = '$dt' and v.version <> $DOC_VERSION
                                and (v.effective_at <= now()
                                     or exists (select 1 from user_consents c
                                                 where c.terms_version_id = v.id))
                              order by v.effective_at desc, v.version desc
                              limit 1), 0);")
    if [ "$anchor_version" != "0" ]; then
      anchor_effective=$(pgq "select effective_at from terms_versions
                               where doc_type = '$dt' and version = $anchor_version;")
      is_later=$(pgq "select case when '$ef'::timestamptz > effective_at
                             then 't' else 'f' end
                        from terms_versions
                       where doc_type = '$dt' and version = $anchor_version;")
      if [ "$is_later" != "t" ]; then
        echo "  $DOC_TYPE v$DOC_VERSION: corrected effective_at $DOC_EFFECTIVE is not after" >&2
        echo "  v$anchor_version's ($anchor_effective) — the newest version that is in force" >&2
        echo "  or has consent records." >&2
        echo "  A revision must take effect after the version users are bound by — set a" >&2
        echo "  later effective_at (a future one takes effect on its own)." >&2
        exit 1
      fi
    fi
    echo "  $DOC_TYPE v$DOC_VERSION: published but still pending (effective $db_effective)"
    echo "  and has no consent records — nobody is bound by it; revising in place"
    [ "$file_digest" = "$db_digest" ] || echo "    the body changes"
    [ "$db_title" = "$DOC_TITLE" ] || \
      echo "    the title changes: published '$db_title', file '$DOC_TITLE'"
    [ "$eff_same" = "t" ] || \
      echo "    the effective_at changes: published '$db_effective', file '$DOC_EFFECTIVE'"
    revise+=("$f")
  else
    echo "  $DOC_TYPE v$DOC_VERSION is already published and the file differs from it." >&2
    [ "$file_digest" = "$db_digest" ] || echo "    the body differs" >&2
    [ "$db_title" = "$DOC_TITLE" ] || \
      echo "    the title differs: published '$db_title', file '$DOC_TITLE'" >&2
    [ "$eff_same" = "t" ] || \
      echo "    the effective_at differs: published '$db_effective', file '$DOC_EFFECTIVE'" >&2
    if [ "$consents" != "0" ]; then
      echo "  $consents consent record(s) name the published wording; rewriting it would" >&2
      echo "  make them describe a document that no longer exists, so this script" >&2
      echo "  will not." >&2
    else
      echo "  It is in force — the wording users are consenting to right now — so" >&2
      echo "  this script will not rewrite it underneath them." >&2
    fi
    echo "  Publish a revision instead: copy the file, set" >&2
    echo "  version: $((DOC_VERSION + 1)) and an effective_at, and re-run." >&2
    exit 1
  fi
done

if [ "${#publish[@]}" -eq 0 ] && [ "${#revise[@]}" -eq 0 ]; then
  echo "== nothing to publish"
else
  echo "== publish"
  sql=""
  for f in "${publish[@]}"; do
    parse_doc "$f" || exit 1
    dt=$(sql_escape "$DOC_TYPE"); ti=$(sql_escape "$DOC_TITLE")
    ef=$(sql_escape "$DOC_EFFECTIVE")
    # Command substitution strips trailing newlines, and the body ends in one —
    # so capture a sentinel byte with it and take that byte back off. Without
    # this the document is stored one byte shorter than the file every time.
    bo=$(sql_escape "$DOC_BODY"; printf x); bo=${bo%x}
    sql+="insert into terms_versions (doc_type, version, title, body, effective_at)
          values ('$dt', $DOC_VERSION, '$ti', '$bo', '$ef'::timestamptz);
"
  done
  for f in "${revise[@]}"; do
    parse_doc "$f" || exit 1
    dt=$(sql_escape "$DOC_TYPE"); ti=$(sql_escape "$DOC_TITLE")
    ef=$(sql_escape "$DOC_EFFECTIVE")
    bo=$(sql_escape "$DOC_BODY"; printf x); bo=${bo%x}
    digest=$(printf '%s' "$DOC_BODY" | sha256sum | cut -d' ' -f1)
    # The revisability conditions ride in the WHERE clause so the decision is
    # made against the row as it is at write time, not as it was during the
    # comparison above. If the version went into force or gained a consent in
    # between, the UPDATE matches nothing — and the DO block right after it
    # notices the row does not carry the file's content and aborts the whole
    # transaction, so a half-applied run cannot commit.
    sql+="update terms_versions
             set title = '$ti', body = '$bo', effective_at = '$ef'::timestamptz
           where doc_type = '$dt' and version = $DOC_VERSION
             and effective_at > now()
             and not exists (select 1 from user_consents c
                              where c.terms_version_id = terms_versions.id);
do \$guard\$
begin
  if not exists (select 1 from terms_versions
                  where doc_type = '$dt' and version = $DOC_VERSION
                    and encode(sha256(convert_to(body, 'UTF8')), 'hex') = '$digest'
                    and title = '$ti'
                    and effective_at = '$ef'::timestamptz) then
    raise exception '$dt v$DOC_VERSION went into force or gained consents mid-run; nothing committed';
  end if;
end \$guard\$;
"
  done
  pgtx "$sql" >/dev/null
  [ "${#publish[@]}" -eq 0 ] || echo "  ${#publish[@]} document(s) published"
  [ "${#revise[@]}" -eq 0 ] || echo "  ${#revise[@]} document(s) revised in place"
fi

echo "== verify"
# The application picks the current version per doc_type as the highest one
# whose effective_at has passed — so a document published with a future date is
# not yet in force, and this shows which one users are actually consenting to.
pct exec "$CTID" -- su - postgres -c \
  "psql -q -X -v ON_ERROR_STOP=1 -d $DB -f -" <<'SQL'
select doc_type,
       version,
       title,
       to_char(effective_at at time zone 'Asia/Seoul', 'YYYY-MM-DD HH24:MI') as effective,
       case when effective_at <= now() then 'in force' else 'pending' end as state
  from terms_versions
 order by doc_type, version;
SQL
