#!/usr/bin/env bash
# Whole-system READ-ONLY health snapshot for pickle: pve-node host (root fs, LVM
# thin pool, VG headroom, load), every LXC container the host reports (running
# state and rootfs allocation both),
# pickle-api + console, PostgreSQL, JobRunr recurring jobs, proxy-agent, the SSH
# gateway + WireGuard tunnel, TLS cert, DB backups, and the dev domain end-to-end.
#
# Prints an aligned OK/WARN/FAIL table and a Korean summary. Exits non-zero if
# ANY check is FAIL (WARN does not fail the run). Every probe is time-bounded so
# a hung guest cannot wedge the snapshot. Mutates nothing — safe to run from
# cron (explicit PATH set below); NOT registered as a cron job by this repo.
#
# Usage: health-check.sh            (full snapshot, exit 1 on any FAIL)
set -uo pipefail

export PATH="/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

# ---- thresholds (override via env) ------------------------------------------
DISK_WARN="${DISK_WARN:-80}"; DISK_FAIL="${DISK_FAIL:-90}"   # root fs used %
THINPOOL_WARN="${THINPOOL_WARN:-70}"; THINPOOL_FAIL="${THINPOOL_FAIL:-85}"  # thin pool data%/meta%
LXC_DISK_WARN="${LXC_DISK_WARN:-80}"; LXC_DISK_FAIL="${LXC_DISK_FAIL:-90}"  # LXC rootfs thin-LV alloc %
VGFREE_WARN_G="${VGFREE_WARN_G:-8}"                           # VG free GiB (lvextend headroom)
# The pool guest disks live in, and the volume group holding it. Named rather
# than hardcoded because the environment runbook builds hosts whose storage may
# differ; the VG follows the pool unless it too is overridden.
THINPOOL_LV="${THINPOOL_LV:-pve/data}"
# The value has to be `vg/lv`: the VG is derived from it, and a name that is
# only a VG makes lvs answer with every volume in the group — the read below
# would take the first row and report some other volume's figures as the pool's.
# Say so here, naming the variable, rather than three rows down blaming LVM.
case "$THINPOOL_LV" in
  */*/*|/*|*/) echo "THINPOOL_LV='$THINPOOL_LV' must be vg/lv" >&2; exit 2 ;;
  */*) : ;;
  *) echo "THINPOOL_LV='$THINPOOL_LV' must be vg/lv (a volume group alone is not enough)" >&2
     exit 2 ;;
esac
THINPOOL_VG="${THINPOOL_VG:-${THINPOOL_LV%%/*}}"
LVM_TIMEOUT="${LVM_TIMEOUT:-10}"                              # seconds per lvs/vgs query
LOAD_WARN="${LOAD_WARN:-40}"                                  # 1m loadavg (40 threads)
BACKUP_MAX_HOURS="${BACKUP_MAX_HOURS:-26}"                    # nightly 04:10 + margin
WG_HANDSHAKE_MAX="${WG_HANDSHAKE_MAX:-180}"                   # seconds since last handshake
CERT_WARN_DAYS="${CERT_WARN_DAYS:-30}"
PGCONN_WARN="${PGCONN_WARN:-80}"                              # pickle_dev connections
PCT_TIMEOUT="${PCT_TIMEOUT:-20}"
STATE_DIR="${PICKLE_OPS_STATE_DIR:-/var/lib/pickle-ops}"
DOMAIN="${PICKLE_DEV_DOMAIN:-https://pickle.pusan.ac.kr}"
# Platform wildcard certificates, one per root domain. A single scalar could not
# see a second root, and after the domain cutover it would have kept reporting a
# retired certificate as healthy while the live one went unwatched.
ORIGIN_CERT_GLOB="${ORIGIN_CERT_GLOB:-/etc/nginx/pickle-certs/*.crt}"
BACKUP_DIR="${BACKUP_DIR:-/srv/pickle/backup/db}"

# Per-recurring-job max age (seconds) before "stalled". Cadences per
# the recurring-job schedule; thresholds ~4x the interval so one slow cycle (or the
# hourly vm-expiry) does not false-alarm under a single global gate.
JOB_IDS=(vm-status-poller domain-verification notification-dispatcher \
         route-reconcile deletion-sweeper drift-reconciler \
         stale-task-recovery vm-expiry)
declare -A JOB_MAX=(
  [vm-status-poller]=300 [domain-verification]=300 [notification-dispatcher]=300
  [route-reconcile]=600 [deletion-sweeper]=1200
  [drift-reconciler]=2400 [stale-task-recovery]=2400 [vm-expiry]=7200
)

# ---- result table -----------------------------------------------------------
declare -a R_NAME R_STAT R_DET
FAILS=0; WARNS=0
rec() {
  R_NAME+=("$1"); R_STAT+=("$2"); R_DET+=("${3:-}")
  case "$2" in FAIL) FAILS=$((FAILS+1));; WARN) WARNS=$((WARNS+1));; esac
}

# helpers ---------------------------------------------------------------------
pex()  { timeout "$PCT_TIMEOUT" pct exec "$1" -- "${@:2}"; }
# lvmq <cmd...> — LVM queries, time-bounded. When a thin pool exhausts, device
# mapper suspends its volumes and LVM commands block indefinitely: unbounded,
# the checks written to report pool exhaustion would hang at exactly the moment
# they are needed and the snapshot would never print.
# SIGKILL follows two seconds later: a command blocked in the kernel on a
# suspended device does not necessarily act on SIGTERM, and then a plain
# timeout would still hold the snapshot open.
lvmq() { timeout -k 2 "$LVM_TIMEOUT" "$@"; }
# psqv <sql> — run a scalar/rowset SELECT as postgres in LXC 101, tuples-only.
psqv() { timeout "$PCT_TIMEOUT" pct exec 101 -- su - postgres -c "psql -d pickle_dev -tAc \"$1\"" 2>/dev/null; }

NOW=$(date +%s)

# ---- 1. host basics ---------------------------------------------------------
used=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ -z "$used" ]; then rec "host:disk" FAIL "df failed"
elif [ "$used" -ge "$DISK_FAIL" ]; then rec "host:disk" FAIL "root ${used}% >= ${DISK_FAIL}%"
elif [ "$used" -ge "$DISK_WARN" ]; then rec "host:disk" WARN "root ${used}%"
else rec "host:disk" OK "root ${used}%"; fi

# The root-fs check above sees only pve-root; every guest disk lives in the
# thin pool, whose exhaustion turns ALL guests read-only at once.
# data% and metadata% exhaust independently and metadata is the quieter killer,
# so each gets its own row. Values are decimals — compare via awk (host:load
# pattern); a failed read is FAIL, not a skip: an unwatched pool is the exact
# blind spot this check exists to close.
tp_rows=$(lvmq lvs --noheadings -o data_percent,metadata_percent "$THINPOOL_LV" 2>/dev/null \
          | awk 'NF {gsub(/,/,"."); print $1, $2}')
tp_count=$(printf '%s' "$tp_rows" | grep -c . || true)
read -r tp_data tp_meta <<<"$tp_rows"
if [ "${tp_count:-0}" != 1 ]; then
  # Exactly one volume has to answer. Zero means the read failed; more than one
  # means the name does not identify a single pool, and taking the first row
  # would report some other volume's fullness as the guest pool's.
  rec "host:thinpool" FAIL "$THINPOOL_LV did not resolve to one volume (${tp_count:-0} rows) — thin pool unwatched"
elif [ -z "${tp_data:-}" ] || [ -z "${tp_meta:-}" ]; then
  rec "host:thinpool" FAIL "$THINPOOL_LV carries no thin figures — not a thin pool?"
else
  for ax in data meta; do
    v=$tp_data; [ "$ax" = meta ] && v=$tp_meta
    if awk -v x="$v" -v m="$THINPOOL_FAIL" 'BEGIN{exit !(x>=m)}'; then
      rec "host:thinpool-${ax}" FAIL "${v}% >= ${THINPOOL_FAIL}% — pool exhaustion turns all guests read-only"
    elif awk -v x="$v" -v m="$THINPOOL_WARN" 'BEGIN{exit !(x>=m)}'; then
      rec "host:thinpool-${ax}" WARN "${v}% >= ${THINPOOL_WARN}%"
    else rec "host:thinpool-${ax}" OK "${v}%"; fi
  done
fi

# Free space left in the volume group is the only room for an emergency
# lvextend when the pool fills — surface the margin in every snapshot so an
# incident responder sees it without running anything.
vgfree=$(lvmq vgs --noheadings --units g --nosuffix -o vg_free "$THINPOOL_VG" 2>/dev/null | awk '{gsub(/,/,"."); printf "%d", $1}')
if [ -z "${vgfree:-}" ]; then rec "host:vg-free" WARN "cannot read VG $THINPOOL_VG"
elif [ "$vgfree" -lt "$VGFREE_WARN_G" ]; then rec "host:vg-free" WARN "${vgfree}G free (<${VGFREE_WARN_G}G) — no lvextend headroom"
else rec "host:vg-free" OK "${vgfree}G free"; fi

load1=$(cut -d' ' -f1 /proc/loadavg)
if awk -v l="$load1" -v m="$LOAD_WARN" 'BEGIN{exit !(l>m)}'; then
  rec "host:load" WARN "1m=${load1} (>${LOAD_WARN})"
else rec "host:load" OK "1m=${load1}"; fi

# ---- 2. containers running --------------------------------------------------
# Enumerated from the host rather than from a list kept here: a fixed list
# silently stops covering the container somebody adds next, which is exactly
# what happened — the gateway container ran unwatched while the report looked
# complete because its rootfs row was there and its running row was not.
ct_ok=1
ct_rows=$(timeout "$PCT_TIMEOUT" pct list 2>/dev/null | awk 'NR>1 && NF {print $1, $2, $3}') || ct_ok=0
if [ "$ct_ok" = 0 ] || [ -z "$ct_rows" ]; then
  rec "lxc:list" FAIL "cannot list containers — no container is being watched"
else
  while read -r id st nm; do
    [ -n "$id" ] || continue
    if [ "$st" = running ]; then rec "lxc:${id}(${nm:-?})" OK "running"
    else rec "lxc:${id}(${nm:-?})" FAIL "status=${st:-unknown}"; fi
  done <<<"$ct_rows"
fi

# Container rootfs usage, read from the host's thin-LV allocation: no guest
# exec needed, and thin alloc% >= filesystem use% (space freed in the guest
# stays allocated until trimmed), so the threshold fires no later than the
# guest actually filling — errs only toward early warning. Ids come from
# `pct list` so a new container is covered without touching this script;
# extra volumes (vm-<id>-disk-1…) report the worst value.
#
# Both reads happen once and a failed read is FAIL, not a row that quietly goes
# missing: a loop that yields nothing would print a green report while every
# container's disk went unwatched — the same silent-deletion shape the terminal
# bridge block further down was rewritten to remove.
lv_ok=1
lv_rows=$(lvmq lvs --noheadings -o lv_name,data_percent "$THINPOOL_VG" 2>/dev/null) || lv_ok=0
if [ "$ct_ok" = 0 ] || [ -z "$ct_rows" ]; then
  rec "lxc:rootfs" FAIL "cannot list containers — rootfs usage unwatched"
elif [ "$lv_ok" = 0 ]; then
  rec "lxc:rootfs" FAIL "cannot read VG $THINPOOL_VG (the same read the row above reports) — rootfs usage unwatched"
else
  while read -r ctid _ _; do
    [ -n "$ctid" ] || continue
    # A measurable volume must not hide an unmeasurable sibling: a container can
    # hold several volumes, and reporting only the worst number would leave a
    # plain-LVM volume filling to 100% behind a healthy-looking figure. So the
    # verdict carries both parts — the highest allocation seen, and whether any
    # volume could not be measured at all.
    worst=$(printf '%s\n' "$lv_rows" | awk -v p="vm-${ctid}-disk-" '
      {gsub(/,/,".")}
      index($1,p)==1 {
        seen=1
        if ($2 ~ /^[0-9]+(\.[0-9]+)?$/) { thin=1; if ($2+0>m) m=$2+0 } else { opaque=1 }
      }
      END {
        if (thin && opaque) printf "%.1f partial", m+0
        else if (thin) printf "%.1f", m+0
        else if (seen) print "no-thin-figure"
      }')
    case "${worst:-}" in
      '')
        rec "lxc:${ctid}-rootfs" WARN "no volume named vm-${ctid}-disk-* in $THINPOOL_VG" ;;
      no-thin-figure)
        rec "lxc:${ctid}-rootfs" WARN "volume is not thin-provisioned — allocation unmeasurable" ;;
      *" partial")
        # Report the number that is known and the fact that it is not the whole
        # story, at WARN even when the measured part is comfortable.
        rec "lxc:${ctid}-rootfs" WARN "thin alloc ${worst%% *}% on some volumes; another volume is unmeasurable" ;;
      *)
        if awk -v x="$worst" -v m="$LXC_DISK_FAIL" 'BEGIN{exit !(x>=m)}'; then
          rec "lxc:${ctid}-rootfs" FAIL "thin alloc ${worst}% >= ${LXC_DISK_FAIL}%"
        elif awk -v x="$worst" -v m="$LXC_DISK_WARN" 'BEGIN{exit !(x>=m)}'; then
          rec "lxc:${ctid}-rootfs" WARN "thin alloc ${worst}%"
        else rec "lxc:${ctid}-rootfs" OK "thin alloc ${worst}%"; fi ;;
    esac
  done <<<"$ct_rows"
fi

# ---- 3. api health ----------------------------------------------------------
h=$(pex 101 sh -c 'curl -fsS --max-time 8 http://127.0.0.1:8080/actuator/health' 2>/dev/null)
if printf '%s' "$h" | grep -q '"status":"UP"'; then rec "api:health" OK "UP"
else rec "api:health" FAIL "${h:-no response}"; fi

# ---- 4. console static ------------------------------------------------------
c=$(pex 101 sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 8 http://127.0.0.1/' 2>/dev/null)
if [ "$c" = 200 ]; then rec "console:static" OK "200"; else rec "console:static" FAIL "http=${c:-none}"; fi

# ---- 5. postgres ------------------------------------------------------------
if [ "$(psqv 'select 1')" = 1 ]; then
  conns=$(psqv "select count(*) from pg_stat_activity where datname='pickle_dev'")
  if [ "${conns:-0}" -ge "$PGCONN_WARN" ]; then rec "db:postgres" WARN "up, ${conns} conns (>=${PGCONN_WARN})"
  else rec "db:postgres" OK "up, ${conns:-?} conns"; fi
else rec "db:postgres" FAIL "no 'select 1' response"; fi

# ---- 6. JobRunr recurring-job freshness ------------------------------------
# One query returns each recurring job's seconds-since-last-SUCCEEDED, computed
# entirely in-DB (TZ-independent: updatedat is a UTC timestamp, compared to the
# DB's own UTC clock) so a host/guest clock skew cannot distort the age.
declare -A AGE
jr_rows=$(psqv "select recurringjobid||' '||floor(extract(epoch from (now() at time zone 'utc' - max(updatedat))))::bigint from jobrunr_jobs where state='SUCCEEDED' and recurringjobid is not null group by recurringjobid")
if [ -n "$jr_rows" ]; then
  while read -r jid age; do [ -n "$jid" ] && AGE[$jid]=$age; done <<<"$jr_rows"
fi
for jid in "${JOB_IDS[@]}"; do
  mx=${JOB_MAX[$jid]}; a=${AGE[$jid]:-}
  if [ -z "$a" ]; then rec "job:${jid}" FAIL "no SUCCEEDED run found"
  elif [ "$a" -gt "$mx" ]; then rec "job:${jid}" FAIL "last ok ${a}s ago (>${mx}s)"
  else rec "job:${jid}" OK "${a}s ago"; fi
done
fj=$(psqv "select count(*) from jobrunr_jobs where state='FAILED'")
if [ "${fj:-0}" -gt 0 ]; then rec "job:failed-count" WARN "${fj} FAILED job(s) present"
else rec "job:failed-count" OK "0 failed"; fi

# ---- 7. proxy-agent ---------------------------------------------------------
paa=$(pex 100 systemctl is-active pickle-proxy-agent 2>/dev/null)
if [ "$paa" = active ]; then rec "proxy-agent:unit" OK "active"
else rec "proxy-agent:unit" FAIL "is-active=${paa:-?}"; fi
# /status fails closed on BOTH token AND source IP (the allowlist is pickle-api,
# 198.18.1.20) — so it must be probed FROM LXC 101, not the agent's own container
# (from LXC 100 a healthy agent returns 403, source-rejected). Read URL+token
# from LXC 101 api.env; 200 = healthy. Single quotes: greps run inside LXC 101.
# shellcheck disable=SC2016
pa=$(pex 101 sh -c 'U=$(grep "^PICKLE_PROXY_AGENT_URL=" /etc/pickle/api.env | cut -d= -f2); T=$(grep "^PICKLE_PROXY_AGENT_TOKEN=" /etc/pickle/api.env | cut -d= -f2); curl -s -o /dev/null -w "%{http_code}" --max-time 8 -H "Authorization: Bearer $T" "$U/status"' 2>/dev/null)
if [ "$pa" = 200 ]; then rec "proxy-agent:status" OK "200 (probed from LXC 101)"
else rec "proxy-agent:status" FAIL "GET /status http=${pa:-none}"; fi

# ---- 8. sshgw services + listeners -----------------------------------------
for u in sshpiperd sshgw-proxyfront wg-quick@wg0 nftables; do
  a=$(pex 102 systemctl is-active "$u" 2>/dev/null)
  if [ "$a" = active ]; then rec "sshgw:${u}" OK "active"; else rec "sshgw:${u}" FAIL "is-active=${a:-?}"; fi
done
l22=$(pex 102 sh -c "ss -tln | grep -c '100.64.0.2:22'" 2>/dev/null)
if [ "${l22:-0}" -ge 1 ]; then rec "sshgw:listen-22" OK "100.64.0.2:22 (wg0)"
else rec "sshgw:listen-22" FAIL "no :22 on wg0 addr"; fi
l2222=$(pex 102 sh -c "ss -tln | grep -c '127.0.0.1:2222'" 2>/dev/null)
if [ "${l2222:-0}" -ge 1 ]; then rec "sshgw:listen-2222" OK "127.0.0.1:2222"
else rec "sshgw:listen-2222" FAIL "no sshpiperd :2222"; fi

# ---- 8b. web-terminal bridge — probed only once its unit exists ------
# The unit probe decides whether the whole block runs, so `pct exec` failing
# (container down, pct timing out) used to delete every bridge check from the
# report without recording anything. Separate "the container answered" from
# "the unit exists": only the second may skip the block.
if ! pex 102 true >/dev/null 2>&1; then
  rec "terminal:bridge" FAIL "LXC 102 unreachable (pct exec failed) — bridge unchecked"
elif pex 102 test -f /etc/systemd/system/sshgw-terminal-bridge.service 2>/dev/null; then
  a=$(pex 102 systemctl is-active sshgw-terminal-bridge 2>/dev/null)
  if [ "$a" = active ]; then rec "terminal:bridge" OK "active"
  else rec "terminal:bridge" FAIL "is-active=${a:-?}"; fi
  for p in 8082 8083; do
    l=$(pex 102 sh -c "ss -tln | grep -c ':${p}'" 2>/dev/null)
    if [ "${l:-0}" -ge 1 ]; then rec "terminal:listen-${p}" OK ":${p}"
    else rec "terminal:listen-${p}" FAIL "bridge not listening on :${p}"; fi
  done
  # /terminal/ws through the real TLS path must reach the bridge: a plain GET
  # (no upgrade) is answered by the bridge with 4xx — 502 means nginx cannot
  # reach it, 404 means the location branch is missing.
  tws=$(timeout 10 curl -sk -o /dev/null -w '%{http_code}' \
        --resolve pickle.pusan.ac.kr:443:198.18.1.10 https://pickle.pusan.ac.kr/terminal/ws 2>/dev/null)
  # 403 is the bridge's own answer (a plain GET fails its source-IP/Origin gate).
  # 404 is nginx saying the location branch is not there at all, which the 4*
  # catch-all used to record as healthy — the exact failure the comment warns
  # about.
  case "$tws" in
    403) rec "terminal:ws-path" OK "reaches bridge (HTTP 403 non-upgrade)";;
    404) rec "terminal:ws-path" FAIL "404 — no /terminal/ws location branch on the TLS tier";;
    502) rec "terminal:ws-path" FAIL "nginx->bridge 502 (bridge down or nftables)";;
    *) rec "terminal:ws-path" FAIL "unexpected HTTP ${tws:-timeout} on /terminal/ws";;
  esac
fi

# ---- 9. WireGuard handshake (doubles as relay reachability) -----------------
hs=$(pex 102 sh -c "wg show wg0 latest-handshakes 2>/dev/null | awk '{print \$2}' | head -1" 2>/dev/null)
if [ -n "$hs" ] && [ "$hs" -gt 0 ] 2>/dev/null; then
  age=$((NOW - hs))
  if [ "$age" -le "$WG_HANDSHAKE_MAX" ]; then rec "sshgw:wg-handshake" OK "${age}s ago"
  else rec "sshgw:wg-handshake" FAIL "${age}s ago (>${WG_HANDSHAKE_MAX}s) — relay unreachable?"; fi
else rec "sshgw:wg-handshake" FAIL "no handshake (peer down / unconfigured)"; fi

# ---- 10. nginx config validity ---------------------------------------------
for id in 100 101; do
  if pex "$id" nginx -t >/dev/null 2>&1; then rec "nginx:${id}" OK "config valid"
  else rec "nginx:${id}" FAIL "nginx -t failed"; fi
done

# ---- 11. origin TLS cert expiry (LXC 100) ----------------------------------
# Every wildcard pair on the host is checked by name, so adding a root domain
# brings its certificate under watch without touching this script.
origin_certs=$(pex 100 sh -c "ls -1 $ORIGIN_CERT_GLOB 2>/dev/null")
if [ -z "$origin_certs" ]; then
  rec "cert:origin" FAIL "no wildcard certificate found at $ORIGIN_CERT_GLOB"
else
  for cert_path in $origin_certs; do
    cert_name=$(basename "$cert_path" .crt)
    end=$(pex 100 sh -c "openssl x509 -enddate -noout -in $cert_path 2>/dev/null | cut -d= -f2")
    if [ -n "$end" ]; then
      ee=$(date -d "$end" +%s 2>/dev/null || true)
      if [ -n "$ee" ]; then
        days=$(( (ee - NOW) / 86400 ))
        if [ "$days" -lt 0 ]; then rec "cert:origin:${cert_name}" FAIL "EXPIRED (${days}d)"
        elif [ "$days" -lt "$CERT_WARN_DAYS" ]; then rec "cert:origin:${cert_name}" WARN "${days}d left"
        else rec "cert:origin:${cert_name}" OK "${days}d left"; fi
      else rec "cert:origin:${cert_name}" WARN "unparseable enddate ($end)"; fi
    else rec "cert:origin:${cert_name}" FAIL "cannot read $cert_path"; fi
  done
fi

# ---- 11b. main-entry Let's Encrypt cert expiry (LXC 100) -------------------
# The origin cert above is a 15-year pair that cannot realistically lapse; the
# cert that actually fronts the main domain is renewed by the certbot timer
# every ~60 days out of a 90-day life, so a silent renewal failure is the real
# expiry risk. Warn early enough that several renewal windows remain.
LE_CERT="${LE_CERT:-/etc/letsencrypt/live/pickle.pusan.ac.kr/fullchain.pem}"
LE_WARN_DAYS="${LE_WARN_DAYS:-21}"
end=$(pex 100 sh -c "openssl x509 -enddate -noout -in $LE_CERT 2>/dev/null | cut -d= -f2")
if [ -n "$end" ]; then
  ee=$(date -d "$end" +%s 2>/dev/null || true)
  if [ -n "$ee" ]; then
    days=$(( (ee - NOW) / 86400 ))
    if [ "$days" -lt 0 ]; then rec "cert:main" FAIL "EXPIRED (${days}d)"
    elif [ "$days" -lt "$LE_WARN_DAYS" ]; then rec "cert:main" FAIL "${days}d left — renewal is overdue"
    else rec "cert:main" OK "${days}d left"; fi
  else rec "cert:main" WARN "unparseable enddate ($end)"; fi
else rec "cert:main" FAIL "cannot read $LE_CERT"; fi

# renewal machinery itself: a stopped timer means the cert above will lapse
# silently, and a missing deploy-hook means a renewed cert is not served until
# something else reloads nginx.
if ! pex 100 systemctl is-active certbot.timer >/dev/null 2>&1; then
  rec "cert:renewal" FAIL "certbot.timer not active"
elif pex 100 systemctl is-failed certbot.service >/dev/null 2>&1; then
  # An armed timer proves nothing about the runs it triggers: a timer waiting
  # for its next window is "active" even when every renewal so far has failed.
  # The last unit result is the signal that closes that gap.
  rec "cert:renewal" FAIL "last certbot run failed (systemctl status certbot.service on LXC 100)"
elif ! pex 100 test -x /etc/letsencrypt/renewal-hooks/deploy/pickle-nginx-reload.sh; then
  rec "cert:renewal" WARN "certbot.timer active but reload hook missing"
else rec "cert:renewal" OK "certbot.timer active, last run clean, reload hook present"; fi

# ---- 12. DB backup freshness + integrity (both sides) ----------------------
latest_host=$(find "$BACKUP_DIR" -maxdepth 1 -name 'pickle_dev-*.sql.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
if [ -n "$latest_host" ]; then
  mt=$(stat -c %Y "$latest_host"); age_h=$(( (NOW - mt) / 3600 ))
  if ! gzip -t "$latest_host" 2>/dev/null; then rec "backup:host" FAIL "gzip -t failed ($(basename "$latest_host"))"
  elif [ "$age_h" -le "$BACKUP_MAX_HOURS" ]; then rec "backup:host" OK "${age_h}h old, gzip ok"
  else rec "backup:host" FAIL "${age_h}h old (>${BACKUP_MAX_HOURS}h)"; fi
else rec "backup:host" FAIL "no dump in $BACKUP_DIR"; fi

latest_lxc=$(pex 101 sh -c "ls -t /var/backups/pickle/pickle_dev-*.sql.gz 2>/dev/null | head -1")
if [ -n "$latest_lxc" ]; then
  if pex 101 gzip -t "$latest_lxc" >/dev/null 2>&1; then rec "backup:lxc101" OK "gzip ok ($(basename "$latest_lxc"))"
  else rec "backup:lxc101" FAIL "gzip -t failed on LXC copy"; fi
else rec "backup:lxc101" FAIL "no dump in LXC 101 /var/backups/pickle"; fi

# cron-wrap failure marker (populated once db-backup runs under cron-wrap.sh)
if [ -f "$STATE_DIR/db-backup.fail" ]; then
  rec "backup:cron-marker" FAIL "db-backup.fail present: $(head -1 "$STATE_DIR/db-backup.fail")"
elif [ -f "$STATE_DIR/db-backup.ok" ]; then
  rec "backup:cron-marker" OK "last $(head -1 "$STATE_DIR/db-backup.ok")"
else
  rec "backup:cron-marker" WARN "no cron-wrap marker (db-backup not yet wrapped by cron-wrap.sh)"
fi

# ---- 13. main domain end-to-end ---------------------------------------------
# Scope note: pve-node resolves the main domain through its own /etc/hosts entry
# (the campus NAT has no hairpin), so this exercises the full nginx/app stack
# but NOT the public path. The DNS assertion below is what would catch a
# campus-side DNS change; genuine public reachability can only be proven from
# an off-host vantage point (the relay), which is out of scope for a 10-minute
# read-only cron.
dc=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 "$DOMAIN/" 2>/dev/null)
if [ "$dc" = 200 ]; then rec "e2e:domain" OK "$DOMAIN 200 (via host resolution)"
else rec "e2e:domain" FAIL "$DOMAIN http=${dc:-none}"; fi

# public DNS still points the main domain at this host's public address. The
# record lives in a university zone we do not administer, so a silent change
# there would strand every real user while the check above stays green.
dns_expect="${MAIN_DOMAIN_PUBLIC_IP:-203.0.113.10}"
dns_host=${DOMAIN#https://}; dns_host=${dns_host#http://}; dns_host=${dns_host%%/*}
if ! command -v dig >/dev/null 2>&1; then
  # Without a resolver tool the check cannot fail open silently.
  rec "dns:main" WARN "dig not installed — main-domain DNS unverified"
else
  # Match every address the name resolves to, not just the last line: a CNAME
  # chain or a second A record would otherwise make the verdict depend on
  # resolver ordering.
  dns_got=$(dig +short +time=3 +tries=1 "$dns_host" A 2>/dev/null | grep -E '^[0-9.]+$' | sort | tr '\n' ' ' | sed 's/ $//')
  if [ "$dns_got" = "$dns_expect" ]; then rec "dns:main" OK "$dns_host → $dns_got"
  elif [ -z "$dns_got" ]; then rec "dns:main" FAIL "$dns_host has no A record (expected $dns_expect)"
  else rec "dns:main" FAIL "$dns_host → $dns_got (expected $dns_expect)"; fi
fi

# ---- output -----------------------------------------------------------------
echo "== pickle health-check $(date '+%Y-%m-%d %H:%M:%S %z') =="
w=0; for n in "${R_NAME[@]}"; do [ "${#n}" -gt "$w" ] && w=${#n}; done
for i in "${!R_NAME[@]}"; do
  printf '  %-*s  %-4s  %s\n' "$w" "${R_NAME[$i]}" "${R_STAT[$i]}" "${R_DET[$i]}"
done
echo

total=${#R_NAME[@]}; oks=$((total - FAILS - WARNS))
echo "요약: 총 ${total}개 점검 — 정상 ${oks} / 경고 ${WARNS} / 실패 ${FAILS}"
if [ "$FAILS" -gt 0 ]; then
  echo "실패 항목:"
  for i in "${!R_NAME[@]}"; do
    [ "${R_STAT[$i]}" = FAIL ] && echo "  - ${R_NAME[$i]}: ${R_DET[$i]}"
  done
  echo "상태: 비정상 (조치 필요)"
  exit 1
fi
if [ "$WARNS" -gt 0 ]; then echo "상태: 주의 (경고 있음, 치명적 실패 없음)"
else echo "상태: 정상"; fi
exit 0
