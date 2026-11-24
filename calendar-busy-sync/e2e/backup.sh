#!/usr/bin/env bash
# End-to-end: calendar-busy-sync's optional backups through the real
# conductor daemon, the real google-calendar and backup plugins, against the
# fake Google Calendar API (gcalfake.py): a backup writes JSON + ICS per
# calendar and an index to a local tier; verify restore-tests every set; a
# corrupted snapshot fails verify.
#
# Usage: bash calendar-busy-sync/e2e/backup.sh   (KEEP=1 keeps the work dir)
#   CONDUCTOR / PLUGINS_SRC as in run.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PACK="$(cd "$HERE/.." && pwd)"
FAKEPY="$HERE/gcalfake.py"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/calbak-e2e.XXXXXX")"
FAKE_PID=""; DAEMON_PID=""
cleanup() { [ -n "${BK:-}" ] && rm -rf "$BK"; [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null; [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null; [ -n "${KEEP:-}" ] && echo "kept $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

pass=0; fail=0
ok()  { echo "  PASS: $1"; pass=$((pass+1)); }
bad() { echo "  FAIL: $1"; fail=$((fail+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want $3, got $2)"; fi; }

CONDUCTOR="${CONDUCTOR:-$(command -v conductor)}"
src="${PLUGINS_SRC:-$HOME/Projects/conductor-plugins}"
(cd "$src" && CGO_ENABLED=0 go build -o "$WORK/conductor-google-calendar" ./connectors/google-calendar \
           && CGO_ENABLED=0 go build -o "$WORK/conductor-backup" ./connectors/backup)

export HOME="$WORK/home" XDG_STATE_HOME="$WORK/xdg/state" XDG_CONFIG_HOME="$WORK/xdg/config" XDG_CACHE_HOME="$WORK/xdg/cache"
unset CONDUCTOR_CONFIG PASEO_HOME
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$WORK/cfg"
# The backup plugin's jail refuses a directory under /tmp (other programs'
# sockets live there), so the tier lives under the real home.
BK="$(mktemp -d "$(eval echo ~"$(id -un)")/.calbak-e2e.XXXXXX")"
cp -r "$PACK" "$WORK/cfg/calendar-busy-sync"; rm -rf "$WORK/cfg/calendar-busy-sync/tests" "$WORK/cfg/calendar-busy-sync/e2e"

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
FAKE="http://127.0.0.1:$PORT"
python3 "$FAKEPY" "$PORT" & FAKE_PID=$!
for _ in $(seq 50); do curl -fs "$FAKE/_fake/state" >/dev/null 2>&1 && break; sleep 0.1; done

D() { python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc).replace(hour=15,minute=0,second=0,microsecond=0)+timedelta(days=$1)).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }
evs=""
for i in $(seq 1 12); do evs="$evs{\"id\":\"e$i\",\"status\":\"confirmed\",\"summary\":\"Thing; $i, ok\",\"start\":{\"dateTime\":\"$(D $i)\"},\"end\":{\"dateTime\":\"$(D $i | sed 's/T15/T16/')\"}},"; done
curl -fsS -X POST "$FAKE/_fake/seed" -H 'content-type: application/json' -d "{\"home\":[${evs%,}],\"work\":[{\"id\":\"w\",\"status\":\"confirmed\",\"summary\":\"Weekly\",\"recurrence\":[\"RRULE:FREQ=WEEKLY\"],\"start\":{\"dateTime\":\"$(D -30)\",\"timeZone\":\"UTC\"},\"end\":{\"dateTime\":\"$(D -30 | sed 's/T15/T16/')\",\"timeZone\":\"UTC\"}}]}" >/dev/null

gc() { cat <<YAML
  $1:
    use: $WORK/conductor-google-calendar
    allow_unverified: true
    trust: full
    api_base: $FAKE/calendar/v3
    auth: { grant: refresh_token, client_id: cid, client_secret: csec, token_url: $FAKE/token, refresh_token: rt-$1 }
YAML
}
cat > "$WORK/cfg/config.yaml" <<YAML
connectors:
$(gc home)
$(gc work)
  timer: { use: cron, schedules: { busy_sync: { every: 24h }, busy_backup: { every: 24h }, busy_verify: { every: 24h } } }
  calbackup:
    use: $WORK/conductor-backup
    allow_unverified: true
    allow_paths: [$BK]
    tiers:
      - { name: local, driver: local, dir: $BK/cal, keep_daily: 7 }
packs:
  calbak:
    use: ./calendar-busy-sync
    connectors: { sources: [home], targets: [work], cron: timer, backup: calbackup }
    triggers: { backup: { enabled: true }, verify: { enabled: true } }
YAML

(cd "$WORK/cfg" && "$CONDUCTOR" init --config config.yaml >"$WORK/init.log" 2>&1) || { cat "$WORK/init.log"; exit 1; }
(cd "$WORK/cfg" && exec "$CONDUCTOR" run --config config.yaml >"$WORK/daemon.log" 2>&1) & DAEMON_PID=$!
for _ in $(seq 150); do grep -q "control socket listening" "$WORK/daemon.log" && break; sleep 0.2; done

fire() { # trigger → waits for "workflow done|failed" for it; prints the outcome
  local before; before=$(grep -cE "calbak/$1 .*(workflow failed|\[complete\]|\[failed\])|notify \[(complete|failed)\] manual:calbak/$1" "$WORK/daemon.log" || true)
  (cd "$WORK/cfg" && "$CONDUCTOR" run "calbak/$1" --config config.yaml >/dev/null)
  for _ in $(seq 300); do
    local now; now=$(grep -cE "calbak/$1 .*(workflow failed|\[complete\]|\[failed\])|notify \[(complete|failed)\] manual:calbak/$1" "$WORK/daemon.log" || true)
    [ "$now" -gt "$before" ] && { grep -E "notify \[(complete|failed)\] manual:calbak/$1|calbak/$1 .*workflow (done|failed)" "$WORK/daemon.log" | tail -1; return 0; }
    sleep 0.2
  done
  echo "timeout"
}

echo "== backup"
out=$(fire backup); echo "     $out" | cut -c1-200
case "$out" in *failed*|timeout) bad "backup run"; tail -20 "$WORK/daemon.log";; *) ok "backup run";; esac
sets=$(find "$BK/cal" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort | tr '\n' ' ')
check "five sets on the tier" "$sets" "calendar-home-ics calendar-home-json calendar-index calendar-work-ics calendar-work-json "
ics=$(find "$BK/cal/calendar-home-ics" -type f -name '*.ics' | head -1)
[ -n "$ics" ] && grep -q 'SUMMARY:Thing\\; 3\\, ok' "$ics" && ok "ICS escapes and holds the events" || bad "ICS content"
json=$(find "$BK/cal/calendar-home-json" -type f -name '*.json' | head -1)
check "JSON holds all 12 events (paged reads)" "$(python3 -c "import json,sys; print(len(json.load(open('$json'))))")" 12

echo "== unchanged backup is skipped"
n=$(find "$BK/cal" -type f | wc -l)
fire backup >/dev/null
check "no new snapshot files" "$(find "$BK/cal" -type f | wc -l)" "$n"

echo "== verify"
out=$(fire verify); echo "     $out" | cut -c1-200
case "$out" in *failed*|timeout) bad "verify passes"; grep -i "restore\|verify" "$WORK/daemon.log" | tail -5;; *) ok "verify passes";; esac

echo "== a corrupted snapshot fails verify"
chmod u+w "$json"; printf 'garbage' >> "$json"
out=$(fire verify)
case "$out" in *failed*) ok "verify fails";; *) bad "verify should fail: $out";; esac

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
