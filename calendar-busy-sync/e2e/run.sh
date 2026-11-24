#!/usr/bin/env bash
# End-to-end: calendar-busy-sync through the real conductor daemon, the real
# google-calendar plugin (managed OAuth2, paging, list sockets, member:
# writes) and the real js engine, against a fake Google Calendar API
# (gcalfake.py). Three calendars, Erik's #42 layout: `personal` is only a
# source, `work` and `side` are in both lists.
#
# Usage: bash calendar-busy-sync/e2e/run.sh
#   CONDUCTOR (default: conductor on PATH) and GCAL_PLUGIN (default: build
#   google-calendar from PLUGINS_SRC, or ~/Projects/conductor-plugins) pick
#   the binaries. KEEP=1 keeps the work dir.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PACK="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/busy-e2e.XXXXXX")"
FAKE_PID=""; DAEMON_PID=""
cleanup() { [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null; [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null; [ -n "${KEEP:-}" ] && echo "kept $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

pass=0; fail=0
ok()  { echo "  PASS: $1"; pass=$((pass+1)); }
bad() { echo "  FAIL: $1"; fail=$((fail+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want $3, got $2)"; fi; }

CONDUCTOR="${CONDUCTOR:-$(command -v conductor)}"
if [ -z "${GCAL_PLUGIN:-}" ]; then
  src="${PLUGINS_SRC:-$HOME/Projects/conductor-plugins}"
  (cd "$src" && CGO_ENABLED=0 go build -o "$WORK/conductor-google-calendar" ./connectors/google-calendar)
  GCAL_PLUGIN="$WORK/conductor-google-calendar"
fi
echo "conductor: $("$CONDUCTOR" version 2>/dev/null | head -1)"

export HOME="$WORK/home" XDG_STATE_HOME="$WORK/xdg/state" XDG_CONFIG_HOME="$WORK/xdg/config" XDG_CACHE_HOME="$WORK/xdg/cache"
unset CONDUCTOR_CONFIG PASEO_HOME
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$WORK/cfg"
cp -r "$PACK" "$WORK/cfg/calendar-busy-sync"; rm -rf "$WORK/cfg/calendar-busy-sync/tests" "$WORK/cfg/calendar-busy-sync/e2e"

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
FAKE="http://127.0.0.1:$PORT"
python3 "$HERE/gcalfake.py" "$PORT" & FAKE_PID=$!
for _ in $(seq 50); do curl -fs "$FAKE/_fake/state" >/dev/null 2>&1 && break; sleep 0.1; done

j() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }
state() { curl -fsS "$FAKE/_fake/state"; }
seed() { curl -fsS -X POST "$FAKE/_fake/seed" -H 'content-type: application/json' -d "$1" >/dev/null; }
put() { curl -fsS -X POST "$FAKE/_fake/put" -H 'content-type: application/json' -d "$1" >/dev/null; }
# blocks <cal>: this pack's live blocks on cal, as sorted "key" lines
blocks() { state | j "'\n'.join(sorted(e['extendedProperties']['private']['conductorBusySync'] for e in d.get('$1',{}).values() if e.get('status')!='cancelled' and 'conductorBusySync' in e.get('extendedProperties',{}).get('private',{})))"; }
nblocks() { state | j "sum(1 for e in d.get('$1',{}).values() if e.get('status')!='cancelled' and 'conductorBusySync' in e.get('extendedProperties',{}).get('private',{}))"; }
writes() { curl -fsS "$FAKE/_fake/log" | j "len(d)"; }

# Dates relative to now so they sit inside the window.
D() { python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc).replace(hour=15,minute=0,second=0,microsecond=0)+timedelta(days=$1)).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }
DAY() { python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(days=$1)).strftime('%Y-%m-%d'))"; }
P1=$(D 2); P1E=$(D 2 | sed 's/T15/T16/'); W1=$(D 3); W1E=$(D 3 | sed 's/T15/T16/')
FAR=$(D 45); FARE=$(D 45 | sed 's/T15/T16/'); BEYOND=$(D 75); BEYONDE=$(D 75 | sed 's/T15/T16/')
REP=$(D -14); REPE=$(D -14 | sed 's/T15/T16/'); AD=$(DAY 5); ADE=$(DAY 6)

seed "$(cat <<JSON
{
 "personal": [
  {"id":"p1","status":"confirmed","iCalUID":"p1@x","summary":"Dentist","start":{"dateTime":"$P1"},"end":{"dateTime":"$P1E"}},
  {"id":"pfree","status":"confirmed","summary":"Maybe","transparency":"transparent","start":{"dateTime":"$W1"},"end":{"dateTime":"$W1E"}},
  {"id":"pfar","status":"confirmed","summary":"Trip","start":{"dateTime":"$FAR"},"end":{"dateTime":"$FARE"}},
  {"id":"pbeyond","status":"confirmed","summary":"Later","start":{"dateTime":"$BEYOND"},"end":{"dateTime":"$BEYONDE"}},
  {"id":"pday","status":"confirmed","summary":"Off","start":{"date":"$AD"},"end":{"date":"$ADE"}},
  {"id":"pbday","status":"confirmed","summary":"Birthday","transparency":"transparent","start":{"date":"$AD"},"end":{"date":"$ADE"}},
  {"id":"pnosync","status":"confirmed","summary":"x","description":"[nosync]","start":{"dateTime":"$W1"},"end":{"dateTime":"$W1E"}},
  {"id":"weekly","status":"confirmed","iCalUID":"weekly@x","summary":"Standup","recurrence":["RRULE:FREQ=WEEKLY"],"start":{"dateTime":"$REP","timeZone":"UTC"},"end":{"dateTime":"$REPE","timeZone":"UTC"}}
 ],
 "work": [
  {"id":"w1","status":"confirmed","iCalUID":"w1@x","summary":"1:1","start":{"dateTime":"$W1"},"end":{"dateTime":"$W1E"}},
  {"id":"bb0123456789abc","status":"confirmed","summary":"Busy","start":{"dateTime":"$P1"},"end":{"dateTime":"$P1E"},"extendedProperties":{"private":{"bb_proj_id":"1"}}}
 ],
 "side": []
}
JSON
)"

freeport() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
LISTEN_personal=127.0.0.1:$(freeport); LISTEN_work=127.0.0.1:$(freeport); LISTEN_side=127.0.0.1:$(freeport)
gc() { local l="LISTEN_$1"; cat <<YAML
  $1:
    use: $GCAL_PLUGIN
    allow_unverified: true
    trust: full
    api_base: $FAKE/calendar/v3
    auth: { grant: refresh_token, client_id: cid, client_secret: csec, token_url: $FAKE/token, refresh_token: rt-$1 }
    watch: { listen: ${!l}, address: "https://push-$1.example.invalid" }
YAML
}
write_config() { # $1 = settings yaml (flow map)
  cat > "$WORK/cfg/config.yaml" <<YAML
connectors:
$(gc personal)
$(gc work)
$(gc side)
  timer: { use: cron, schedules: { busy_sync: { every: 24h } } }
packs:
  busy:
    use: ./calendar-busy-sync
    connectors:
      sources: [personal, work, side]
      targets: [work, side]
      cron: timer
    settings: $1
    triggers:
      sweep: { enabled: true }
      on_change: { enabled: true }
      on_target_change: { enabled: true }
      cleanup: { enabled: true }
YAML
}

start() {
  [ -n "$DAEMON_PID" ] && { kill "$DAEMON_PID" 2>/dev/null; wait "$DAEMON_PID" 2>/dev/null || true; DAEMON_PID=""; }
  # the old daemon's push listeners must be gone before the new one binds them
  for _ in $(seq 50); do [ -e "$XDG_STATE_HOME/conductor/control.sock" ] || break; sleep 0.1; done
  (cd "$WORK/cfg" && "$CONDUCTOR" init --config config.yaml >"$WORK/init.log" 2>&1) || { cat "$WORK/init.log"; exit 1; }
  (cd "$WORK/cfg" && exec "$CONDUCTOR" run --config config.yaml >>"$WORK/daemon.log" 2>&1) & DAEMON_PID=$!
  local marks; marks=$(grep -c "control socket listening" "$WORK/daemon.log" || true)
  for _ in $(seq 150); do
    [ "$(grep -c "control socket listening" "$WORK/daemon.log" || true)" -gt "${STARTS:-0}" ] && { STARTS=$((${STARTS:-0}+1)); return 0; }
    sleep 0.2
  done
  echo "daemon did not start"; tail -30 "$WORK/daemon.log"; exit 1
}
runs=0
sweep() { # run one sweep and wait for it to finish
  local before; before=$(grep -c "calendar-busy-sync: " "$WORK/daemon.log" || true)
  (cd "$WORK/cfg" && "$CONDUCTOR" run busy/sweep --config config.yaml >/dev/null)
  for _ in $(seq 150); do
    local now; now=$(grep -c "calendar-busy-sync: " "$WORK/daemon.log" || true)
    if [ "$now" -gt "$before" ]; then
      sleep 3; return 0
    fi
    sleep 0.2
  done
  echo "sweep did not finish"; tail -40 "$WORK/daemon.log"; return 1
}
last_plan() { grep "calendar-busy-sync: " "$WORK/daemon.log" | tail -1 | sed 's/.*calendar-busy-sync: //'; }

echo "== 1. dry run writes nothing"
write_config "{ dry_run: true }"
start
sweep
check "no writes in dry run" "$(writes)" 0
echo "     plan: $(last_plan)"

echo "== 2. first sync"
write_config "{}"
start
sweep
echo "     plan: $(last_plan)"
check "personal never gets blocks (not a target)" "$(nblocks personal)" 0
WANT_SIDE=$(printf '%s\n' personal/p1 personal/pday personal/pfar personal/weekly work/w1 | sort)
check "side: dentist, all-day busy, 45-day trip, one repeating standup, work's 1:1" "$(blocks side)" "$WANT_SIDE"
WANT_WORK=$(printf '%s\n' personal/p1 personal/pday personal/pfar personal/weekly | sort)
check "work: the personal ones, not its own 1:1, not BusyBridge's block" "$(blocks work)" "$WANT_WORK"
check "standup is ONE repeating block on side" "$(state | j "[e.get('recurrence',[''])[0] for e in d['side'].values() if e.get('extendedProperties',{}).get('private',{}).get('conductorBusySync')=='personal/weekly']")" "['RRULE:FREQ=WEEKLY']"
check "blocks are private, opaque, silent, graphite" "$(state | j "sorted({(e['visibility'],e['transparency'],json.dumps(e['reminders'],sort_keys=True),e['colorId']) for c in ('work','side') for e in d[c].values() if 'conductorBusySync' in e.get('extendedProperties',{}).get('private',{})} ) if (json:=__import__('json')) else 0")" "[('private', 'opaque', '{\"overrides\": [], \"useDefault\": false}', '8')]"

echo "== 3. second run is a no-op"
n=$(writes); sweep
check "no writes" "$(writes)" "$n"
echo "     plan: $(last_plan)"

echo "== 4. source changes follow"
put "{\"cal\":\"personal\",\"event\":{\"id\":\"p1\",\"status\":\"confirmed\",\"iCalUID\":\"p1@x\",\"summary\":\"Dentist\",\"start\":{\"dateTime\":\"$(D 4)\"},\"end\":{\"dateTime\":\"$(D 4 | sed 's/T15/T16/')\"}}}"
put "{\"cal\":\"personal\",\"event\":{\"id\":\"pfar\",\"status\":\"cancelled\",\"summary\":\"Trip\",\"start\":{\"dateTime\":\"$FAR\"},\"end\":{\"dateTime\":\"$FARE\"}}}"
sweep
check "moved event moved its block" "$(state | j "[e['start']['dateTime'][:10] for e in d['side'].values() if e.get('status')!='cancelled' and e.get('extendedProperties',{}).get('private',{}).get('conductorBusySync')=='personal/p1']")" "['$(D 4 | cut -c1-10)']"
check "cancelled event's blocks removed" "$(blocks side | grep -c pfar || true)" 0

echo "== 5. a block deleted by hand comes back"
bid=$(state | j "[k for k,e in d['work'].items() if e.get('status')!='cancelled' and e.get('extendedProperties',{}).get('private',{}).get('conductorBusySync')=='personal/p1'][0]")
curl -fsS -X POST "$FAKE/_fake/del" -H 'content-type: application/json' -d "{\"cal\":\"work\",\"id\":\"$bid\"}" >/dev/null
sweep
check "restored on work" "$(blocks work | grep -c '^personal/p1$')" 1

echo "== 5b. a push from a source syncs without a sweep"
for _ in $(seq 50); do [ "$(curl -fsS "$FAKE/_fake/channels" | j "len(d)")" = 3 ] && break; sleep 0.2; done
check "a watch channel is open on every calendar" "$(curl -fsS "$FAKE/_fake/channels" | j "sorted(d)")" "['personal', 'side', 'work']"
P2=$(D 6); P2E=$(D 6 | sed 's/T15/T16/')
put "{\"cal\":\"personal\",\"event\":{\"id\":\"p2\",\"status\":\"confirmed\",\"summary\":\"Call\",\"start\":{\"dateTime\":\"$P2\"},\"end\":{\"dateTime\":\"$P2E\"}}}"
before=$(grep -c "calendar-busy-sync: " "$WORK/daemon.log")
curl -fsS -X POST "$FAKE/_fake/push" -H 'content-type: application/json' -d "{\"cal\":\"personal\",\"listen\":\"$LISTEN_personal\"}" >/dev/null
for _ in $(seq 100); do [ "$(grep -c "calendar-busy-sync: " "$WORK/daemon.log")" -gt "$before" ] && break; sleep 0.2; done; sleep 3
check "pushed event blocked on work and side" "$(blocks work | grep -c '^personal/p2$')$(blocks side | grep -c '^personal/p2$')" 11
grep -q "busy/on_change" "$WORK/daemon.log" && ok "it was on_change that ran" || bad "on_change never ran"

echo "== 5c. a push from a target puts a hand-deleted block back"
bid=$(state | j "[k for k,e in d['side'].items() if e.get('status')!='cancelled' and e.get('extendedProperties',{}).get('private',{}).get('conductorBusySync')=='personal/p2'][0]")
curl -fsS -X POST "$FAKE/_fake/del" -H 'content-type: application/json' -d "{\"cal\":\"side\",\"id\":\"$bid\"}" >/dev/null
before=$(grep -c "calendar-busy-sync: " "$WORK/daemon.log")
curl -fsS -X POST "$FAKE/_fake/push" -H 'content-type: application/json' -d "{\"cal\":\"side\",\"listen\":\"$LISTEN_side\"}" >/dev/null
for _ in $(seq 100); do [ "$(grep -c "calendar-busy-sync: " "$WORK/daemon.log")" -gt "$before" ] && break; sleep 0.2; done; sleep 3
check "restored on side" "$(blocks side | grep -c '^personal/p2$')" 1
grep -q "busy/on_target_change" "$WORK/daemon.log" && ok "it was on_target_change that ran" || bad "on_target_change never ran"

echo "== 6. Erik's 0.1.0 blocks are adopted, not duplicated"
reseed() { curl -fsS -X POST "$FAKE/_fake/seed" -H 'content-type: application/json' -d "$(state | j "__import__('json').dumps({c: [e for e in v.values() if $1] for c,v in d.items()})")" >/dev/null; }
reseed "True"  # clears the write log, keeps the calendars
put "{\"cal\":\"side\",\"event\":{\"id\":\"old01\",\"status\":\"confirmed\",\"summary\":\"Busy\",\"transparency\":\"opaque\",\"visibility\":\"private\",\"colorId\":\"8\",\"reminders\":{\"useDefault\":false,\"overrides\":[]},\"start\":{\"dateTime\":\"$W1\"},\"end\":{\"dateTime\":\"$W1E\"},\"extendedProperties\":{\"private\":{\"conductorBusySync\":\"work/w1\"}}}}"
old=$(state | j "[k for k,e in d['side'].items() if k!='old01' and e.get('extendedProperties',{}).get('private',{}).get('conductorBusySync')=='work/w1' and e.get('status')!='cancelled'][0]")
sweep
check "one work/w1 block on side" "$(blocks side | grep -c '^work/w1$')" 1

echo "== 7. the delete guard"
reseed "c!='personal'"  # the personal calendar reads back empty
write_config "{ max_deletes: 2 }"
start
sweep || true
check "refused, nothing deleted" "$(writes)" 0
grep -q "refusing to delete" "$WORK/daemon.log" && ok "the run says why" || bad "no refusal in the log"

echo "== 8. remove_all clears every block"
write_config "{ remove_all: true }"
start
sweep; sweep
check "work clear" "$(nblocks work)" 0
check "side clear" "$(nblocks side)" 0
check "the target's own events stay" "$(state | j "sorted(k for k,e in d['work'].items() if e.get('status')!='cancelled')")" "['bb0123456789abc', 'w1']"

echo "== 9. BusyBridge cleanup"
write_config "{}"
start
(cd "$WORK/cfg" && "$CONDUCTOR" run busy/cleanup --config config.yaml >/dev/null); sleep 4
check "report only by default" "$(state | j "d['work']['bb0123456789abc']['status']")" confirmed
(cd "$WORK/cfg" && "$CONDUCTOR" run busy/cleanup --input apply=true --config config.yaml >/dev/null); sleep 4
check "applied: BusyBridge's future block deleted" "$(state | j "d['work']['bb0123456789abc']['status']")" cancelled

echo "== 10. attempt limit: many sweeps keep running"
before=$(grep -c "calendar-busy-sync: " "$WORK/daemon.log")
for i in 1 2 3 4 5 6 7 8; do sweep; done
after=$(grep -c "calendar-busy-sync: " "$WORK/daemon.log")
check "8 more sweeps all ran" "$((after-before))" 8
grep -q "parked" "$WORK/daemon.log" && bad "a trigger was parked" || ok "nothing parked"

echo
grep -iE "panic|error" "$WORK/daemon.log" | grep -v "refusing to delete\|max_deletes" | head -5 || true
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
