#!/usr/bin/env bash
# End-to-end: pr-autopilot's moved-head steps, through the real conductor
# (`conductor once`), the real pack, and the real github plugin against the
# GitHub fake. The fixer is a sandboxed cli agent (a stand-in for the claude
# CLI, ./claude) that commits a fix; while it works, the PR branch moves.
# Conductor's end-of-step push is refused (fast-forward only), and the pack
# lands the change with github.apply_diff — or, when it conflicts, runs the
# fixer once more.
#
# Then a reviewer's inline thread: the fixer reports the comment its change
# addressed, and once conductor has pushed it (straight, or landed on a moved
# head) the pack resolves that thread.
#
# Last, on_merged: your merged PR's branch is deleted, but not once someone
# pushed to it after the merge, nor for someone else's PR. No agent.
#
# Git: the agent clones and pushes over a local bare repository (a git
# insteadOf in the harness's own HOME); GitHub's API is the fake, holding the
# same files. The network is used only for builds and for the released js
# engine plugin the pack's code step runs on (`conductor init` fetches it).
#
# Usage: bash pr-autopilot/e2e/moved-head.sh
#   CONDUCTOR_SRC / PLUGINS_SRC: local checkouts to build (default: clone the
#   refs in pins.env).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PACK="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/autopilot-e2e.XXXXXX")"
FAKE_PID=""
DAEMON_PID=""
cleanup() { [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null; [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null; [ -n "${KEEP:-}" ] && echo "kept $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT
. "$HERE/pins.env"

pass=0; fail=0
ok()  { echo "  PASS: $1"; pass=$((pass+1)); }
bad() { echo "  FAIL: $1"; fail=$((fail+1)); }

echo "== build =="
export GOFLAGS=-mod=mod GOCACHE="$(go env GOCACHE)" GOMODCACHE="$(go env GOMODCACHE)"
src() { # name url ref → a checkout
  local dir="$WORK/src/$1"
  git clone -q --filter=blob:none --depth 1 --branch "$3" "$2" "$dir"
  echo "$dir"
}
CSRC="${CONDUCTOR_SRC:-$(src conductor https://github.com/NodeSpy/conductor.git "$CONDUCTOR_REF")}"
PSRC="${PLUGINS_SRC:-$(src plugins https://github.com/NodeSpy/conductor-plugins.git "$PLUGINS_REF")}"
mkdir -p "$WORK/bin"
( cd "$CSRC" && go build -o "$WORK/bin/conductor" ./cmd/conductor )
( cd "$PSRC" && CGO_ENABLED=0 go build -o "$WORK/bin/conductor-github" ./connectors/github && go build -o "$WORK/bin/ghfake" ./internal/githubkit/ghfake/cmd/ghfake )
cp "$HERE/claude" "$WORK/bin/claude"

# Everything conductor and git write stays under $WORK.
export HOME="$WORK/home" XDG_STATE_HOME="$WORK/xdg/state" XDG_CONFIG_HOME="$WORK/xdg/config" XDG_CACHE_HOME="$WORK/xdg/cache"
unset PASEO_HOME CONDUCTOR_CONFIG GH_TOKEN GITHUB_TOKEN
mkdir -p "$HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"
git config --global user.email harness@example.invalid
git config --global user.name harness
git config --global init.defaultBranch main
git config --global url."file://$WORK/forge/acme/".insteadOf "git@github.com:acme/"
git config --global --add url."file://$WORK/forge/acme/".insteadOf "https://github.com/acme/"

BASE_GO='package main

func a() int {
	return 1
}

func b() int {
	return 2
}
'

FAKE=""
fake() { curl -fsS -X POST "$FAKE/_fake/$1" -H 'content-type: application/json' -d "$2"; }
q() { curl -fsS "$FAKE/_fake/q?$1"; }
json() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }

# start_fake <log> [<pr author>]: a fresh fake, seeded with PR #1 (feat → main).
start_fake() {
  [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null && wait "$FAKE_PID" 2>/dev/null || true
  rm -f "$WORK/fake.url"
  "$WORK/bin/ghfake" -url-file "$WORK/fake.url" >"$1" 2>&1 & FAKE_PID=$!
  for _ in $(seq 1 100); do [ -s "$WORK/fake.url" ] && break; sleep 0.05; done
  FAKE="$(cat "$WORK/fake.url")"
  fake seed '{"users":[{"login":"acme","type":"Organization"},{"login":"me"},{"login":"rev"}],"tokens":{"tok-me":"me","tok-rev":"rev"},"repos":[{"name":"acme/w","collaborators":["rev"]}],"hooks":[{"url":"'"${HOOK_URL:-http://127.0.0.1:9/nowhere}"'","secret":"e2e"}],"prs":[{"repo":"acme/w","author":"'"${2:-me}"'","head":"feat","base":"main","title":"feature"}]}' >/dev/null
  python3 - "$BASE_GO" <<'PY' | fake act @- >/dev/null
import json,sys; print(json.dumps({"op":"commit_files","repo":"acme/w","branch":"feat","by":"me","message":"base","files":{"main.go":sys.argv[1],"FEATURE.md":"the feature\n"}}))
PY
}

# start_forge: PR #1's branch in git, as start_fake seeds it on GitHub.
start_forge() {
  rm -rf "$WORK/forge" "$WORK/seed" "$XDG_STATE_HOME/conductor/state.json" "$XDG_STATE_HOME/conductor/worktrees" "$XDG_STATE_HOME/conductor/checkouts"
  mkdir -p "$WORK/forge/acme"
  git init -q --bare "$WORK/forge/acme/w.git"
  git init -q "$WORK/seed" && printf '%s' "$BASE_GO" > "$WORK/seed/main.go"
  git -C "$WORK/seed" add main.go && git -C "$WORK/seed" commit -qm base
  git -C "$WORK/seed" push -q "$WORK/forge/acme/w.git" main
  printf 'the feature\n' > "$WORK/seed/FEATURE.md"
  git -C "$WORK/seed" add FEATURE.md && git -C "$WORK/seed" commit -qm feature
  git -C "$WORK/seed" push -q "$WORK/forge/acme/w.git" main:feat main:refs/pull/1/head
}

# scenario <name> <theirs main.go> [<comment>] [review]: a fresh world; their
# push lands while the fixer works (none when <theirs> is empty). With
# `review`, the comment is an inline thread of a COMMENT review on main.go.
scenario() {
  local name="$1" theirs="$2" comment="${3:-please make a() return 10}" kind="${4:-comment}" w="$WORK/$1"
  mkdir -p "$w"
  start_fake "$w/ghfake.log"

  start_forge

  cat > "$w/config.yaml" <<YAML
connectors:
  gh:
    use: $WORK/bin/conductor-github
    api_base: $FAKE
    identity: { read_token: tok-me, write_token: tok-me }
    me: { logins: [me] }
sandbox:
  push: { sign: false }
runtimes:
  claude-cli: { use: cli, tool: claude-code, bin: $WORK/bin/claude, default: true }
packs:
  autopilot:
    source: $PACK
    triggers:
      on_new_comment: { enabled: true, repos: [acme/w] }
YAML
  # The event: the fake's own webhook for a reviewer's comment (delivered
  # nowhere; read back from the fake), as `once`'s fixture.
  python3 - "$comment" "$kind" <<'PY' | fake act @- > "$w/act.json"
import json,sys
body, kind = sys.argv[1], sys.argv[2]
if kind == "review":
    print(json.dumps({"op":"review","repo":"acme/w","number":1,"by":"rev","state":"COMMENTED","body":"","comments":[{"path":"main.go","line":4,"body":body}]}))
else:
    print(json.dumps({"op":"comment","repo":"acme/w","number":1,"by":"rev","body":body}))
PY
  fake act '{"op":"flush"}' >/dev/null
  local ev=issue_comment; [ "$kind" = review ] && ev=pull_request_review_comment
  curl -fsS "$FAKE/_fake/deliveries" | python3 -c '
import json,sys
d=[x for x in json.load(sys.stdin) if x["event"]==sys.argv[1]][-1]
print(json.dumps({"event": d["event"], "body": d["payload"]}))' "$ev" > "$w/event.json"

  "$WORK/bin/conductor" init --config "$w/config.yaml" >"$w/init.log" 2>&1 || { echo "init failed:"; cat "$w/init.log"; return 1; }

  # Their push, the moment the fixer has committed: the git branch and GitHub's.
  ( for _ in $(seq 1 600); do
      ready="$(find "$WORK/xdg" "$w" -name .e2e-ready 2>/dev/null | head -1)"
      if [ -n "$ready" ] && [ -z "$theirs" ]; then
        touch "$(dirname "$ready")/.e2e-go"
        exit 0
      fi
      if [ -n "$ready" ]; then
        rm -rf "$WORK/theirs" && git clone -q "$WORK/forge/acme/w.git" -b feat "$WORK/theirs"
        printf '%s' "$theirs" > "$WORK/theirs/main.go"
        git -C "$WORK/theirs" commit -qam theirs && git -C "$WORK/theirs" push -q origin feat feat:refs/pull/1/head
        python3 - "$theirs" <<'PY' | fake act @- >/dev/null
import json,sys; print(json.dumps({"op":"commit_files","repo":"acme/w","branch":"feat","by":"rev","message":"theirs","files":{"main.go":sys.argv[1]}}))
PY
        touch "$(dirname "$ready")/.e2e-go"
        exit 0
      fi
      sleep 0.1
    done ) &
  local mover=$!
  set +e
  PATH="$WORK/bin:$PATH" "$WORK/bin/conductor" once autopilot/on_new_comment --config "$w/config.yaml" --state-dir "$XDG_STATE_HOME/conductor" --fail-on none --fixture "$w/event.json" >"$w/once.log" 2>&1
  echo $? > "$w/exit"
  set -e
  wait "$mover" 2>/dev/null || true
}

ran() { grep -q "^  [✓✗] $2 " "$WORK/$1/once.log"; }
outcome() { sed -n 's/^outcome: //p' "$WORK/$1/once.log" | head -1; }
show() { echo "    --- $1/once.log"; grep -v '^plugin ' "$WORK/$1/once.log" | cut -c1-300 | tail -30 | sed 's/^/    /'; }

THEIRS_CLEAN="${BASE_GO/return 2/return 20}"
[ -n "${ONLY_THREAD:-}${ONLY_MERGED:-}${ONLY_SERIAL:-}${ONLY_PROGRESS:-}" ] || {
echo "== clean: their change elsewhere in the file; the fix lands on the new head =="
scenario clean "$THEIRS_CLEAN"
got="$(q 'what=files&repo=acme/w&branch=feat')"
main="$(echo "$got" | json 'd["files"]["main.go"]')"
case "$main" in *"return 10"*"return 20"*) ok "GitHub's branch has both the fix and their change" ;; *) bad "main.go on GitHub: $main" ;; esac
[ "$(echo "$got" | json 'd["author"]')" = me ] && ok "the commit is the write identity's" || bad "author: $got"
grep -q "diverged" "$WORK/clean/once.log" && ok "conductor's push was refused (diverged)" || bad "no diverged push"
ran clean landed && ok "landed ran" || bad "landed did not run"
ran clean refix && bad "refix ran on a clean apply" || ok "no second attempt"
[ "$(outcome clean)" = ok ] && ok "the run's outcome is ok" || { bad "outcome: $(outcome clean)"; show clean; }

echo "== conflict: their change on the same line; the fixer runs once more =="
THEIRS_CONFLICT="${BASE_GO/return 1/return 99}"
scenario conflict "$THEIRS_CONFLICT"
ran conflict landed && ok "landed ran" || bad "landed did not run"
ran conflict refix && ok "refix ran" || bad "refix did not run"
main="$(git -C "$WORK/forge/acme/w.git" show feat:main.go)"
case "$main" in *"return 10"*) ok "the second attempt was pushed by conductor" ;; *) bad "main.go on the branch: $main" ;; esac
[ "$(q 'what=files&repo=acme/w&branch=feat' | json 'd["author"]')" = rev ] && ok "apply_diff committed nothing on a conflict" || bad "GitHub's branch moved on a conflict"
[ "$(outcome conflict)" = ok ] && ok "the run's outcome is ok" || { bad "outcome: $(outcome conflict)"; show conflict; }

echo "== clipped: the change is past the diff's cap; nothing is applied, the run fails =="
scenario clipped "$THEIRS_CLEAN" "please make a() return 10 and add the big table"
ran clipped landed && bad "landed ran on a clipped diff" || ok "landed did not run"
[ "$(q 'what=files&repo=acme/w&branch=feat' | json 'd["author"]')" = rev ] && ok "GitHub's branch is untouched" || bad "GitHub's branch moved"
case "$(outcome clipped)" in failed*) ok "the run failed" ;; *) bad "outcome: $(outcome clipped)"; show clipped ;; esac

}
# thread_resolved <scenario>: whether the review's inline thread is resolved
# on GitHub (the fake's GraphQL, as the plugin reads it).
thread_resolved() {
  curl -fsS "$FAKE/graphql" -H 'Authorization: Bearer tok-me' -H 'content-type: application/json' \
    -d '{"query":"query{repository(owner:\"acme\",name:\"w\"){pullRequest(number:1){reviewThreads(first:10){nodes{isResolved}}}}}"}' |
    json 'd["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]["isResolved"]'
}

[ -n "${ONLY_MERGED:-}${ONLY_SERIAL:-}${ONLY_PROGRESS:-}" ] || {
echo "== review thread: the fix is pushed, the thread it addressed is resolved =="
scenario thread "" "please make a() return 10" review
[ "$(git -C "$WORK/forge/acme/w.git" show feat:main.go | grep -c 'return 10')" = 1 ] && ok "conductor pushed the fix" || bad "the fix was not pushed"
ran thread resolve && ok "resolve ran" || bad "resolve did not run"
[ "$(thread_resolved)" = True ] && ok "the thread is resolved" || { bad "the thread is still open"; show thread; }
[ "$(outcome thread)" = ok ] && ok "the run's outcome is ok" || { bad "outcome: $(outcome thread)"; show thread; }

echo "== review thread, moved head: the change lands on the new head, the thread is resolved =="
scenario thread-moved "$THEIRS_CLEAN" "please make a() return 10" review
ran thread-moved landed && ok "landed ran" || bad "landed did not run"
[ "$(thread_resolved)" = True ] && ok "the thread is resolved" || { bad "the thread is still open"; show thread-moved; }

}

# merged <name> <pr author> [moved]: PR #1 merges (and, with `moved`, someone
# pushes to its branch afterwards); on_merged runs on the close event.
merged() {
  local w="$WORK/$1"
  mkdir -p "$w"
  start_fake "$w/ghfake.log" "$2"
  # A push (start_fake's commit_files doesn't move the PR's head), then the merge.
  fake act '{"op":"push","repo":"acme/w","branch":"feat","by":"me","message":"work","paths":["main.go"]}' >/dev/null
  fake act '{"op":"merge","repo":"acme/w","number":1,"by":"me"}' >/dev/null
  [ "${3:-}" = moved ] && fake act '{"op":"commit_files","repo":"acme/w","branch":"feat","by":"rev","message":"after","files":{"LATE.md":"late\n"}}' >/dev/null
  fake act '{"op":"flush"}' >/dev/null
  curl -fsS "$FAKE/_fake/deliveries" | python3 -c '
import json,sys
d=[x for x in json.load(sys.stdin) if x["event"]=="pull_request" and x["payload"].get("action")=="closed"][-1]
print(json.dumps({"event": d["event"], "body": d["payload"]}))' > "$w/event.json"
  cat > "$w/config.yaml" <<YAML
connectors:
  gh:
    use: $WORK/bin/conductor-github
    api_base: $FAKE
    identity: { read_token: tok-me, write_token: tok-me }
    me: { logins: [me] }
packs:
  autopilot:
    source: $PACK
    triggers:
      on_merged: { enabled: true, repos: [acme/w] }
YAML
  "$WORK/bin/conductor" init --config "$w/config.yaml" >"$w/init.log" 2>&1 || { echo "init failed:"; cat "$w/init.log"; return 1; }
  "$WORK/bin/conductor" once autopilot/on_merged --config "$w/config.yaml" --state-dir "$XDG_STATE_HOME/conductor" --fail-on none --fixture "$w/event.json" >"$w/once.log" 2>&1 || true
}
# branch_status: GitHub's answer for the PR's branch (200, or 404 once deleted).
branch_status() { curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: token tok-me' "$FAKE/repos/acme/w/branches/feat"; }

[ -n "${ONLY_SERIAL:-}${ONLY_PROGRESS:-}" ] || {
echo "== merged: your merged PR's branch is deleted =="
merged merged me
ran merged delete && ok "delete ran" || bad "delete did not run"
[ "$(branch_status)" = 404 ] && ok "the branch is gone" || { bad "the branch is still there"; show merged; }
[ "$(outcome merged)" = ok ] && ok "the run's outcome is ok" || { bad "outcome: $(outcome merged)"; show merged; }

echo "== merged, then pushed to: the branch moved since the merge and is kept =="
merged merged-moved me moved
ran merged-moved delete && ok "delete ran" || bad "delete did not run"
[ "$(branch_status)" = 200 ] && ok "the branch is kept" || { bad "the branch was deleted"; show merged-moved; }

echo "== merged, someone else's PR: nothing is deleted =="
merged merged-theirs rev
[ "$(branch_status)" = 200 ] && ok "the branch is kept" || { bad "the branch was deleted"; show merged-theirs; }
grep -q "delete" "$WORK/merged-theirs/once.log" && ! ran merged-theirs delete && ok "delete was skipped" || { bad "delete ran on someone else's PR"; show merged-theirs; }

}

# serial: two fixers on one PR, through the daemon. A comment starts
# on_new_comment; while its agent works, a changes-requested review arrives,
# and on_changes_requested waits on the PR's key until the first run ends.
serial() {
  local w="$WORK/serial" port
  mkdir -p "$w"
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  HOOK_URL="http://127.0.0.1:$port/webhook" start_fake "$w/ghfake.log"
  start_forge
  # GitHub learns of a push to the branch as it lands, as it would: the
  # forge's hook moves the fake's copy of the branch (and the PR's head) to
  # the pushed commit before the push returns.
  cat > "$WORK/forge/acme/w.git/hooks/post-receive" <<HOOK
#!/usr/bin/env bash
while read -r old new ref; do
  [ "\$ref" = refs/heads/feat ] || continue
  curl -fsS -X POST "$FAKE/_fake/act" -H 'content-type: application/json' \\
    -d "{\"op\":\"push_sha\",\"repo\":\"acme/w\",\"branch\":\"feat\",\"by\":\"me\",\"sha\":\"\$new\"}" >/dev/null
done
HOOK
  chmod +x "$WORK/forge/acme/w.git/hooks/post-receive"
  cat > "$w/config.yaml" <<YAML
connectors:
  gh:
    use: $WORK/bin/conductor-github
    api_base: $FAKE
    identity: { read_token: tok-me, write_token: tok-me }
    me: { logins: [me] }
    webhook: { listen: "127.0.0.1:$port", secret: e2e }
    sweep: { enabled: false }
sandbox:
  push: { sign: false }
runtimes:
  claude-cli: { use: cli, tool: claude-code, bin: $WORK/bin/claude, default: true }
packs:
  autopilot:
    source: $PACK
    triggers:
      on_new_comment:       { enabled: true, repos: [acme/w] }
      on_changes_requested: { enabled: true, repos: [acme/w] }
YAML
  "$WORK/bin/conductor" init --config "$w/config.yaml" >"$w/init.log" 2>&1 || { echo "init failed:"; cat "$w/init.log"; return 1; }
  PATH="$WORK/bin:$PATH" "$WORK/bin/conductor" run --config "$w/config.yaml" >"$w/daemon.log" 2>&1 & DAEMON_PID=$!
  for _ in $(seq 1 300); do (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && break; sleep 0.1; done

  fake act '{"op":"comment","repo":"acme/w","number":1,"by":"rev","body":"please make a() return 10"}' >/dev/null
  local ready=""
  for _ in $(seq 1 600); do
    ready="$(find "$WORK/xdg" -name .e2e-ready 2>/dev/null | head -1)"; [ -n "$ready" ] && break; sleep 0.1
  done
  [ -n "$ready" ] && ok "the comment's fixer is working" || bad "no fixer started for the comment"
  fake act '{"op":"review","repo":"acme/w","number":1,"by":"rev","state":"CHANGES_REQUESTED","body":"please make b() return 20","comments":[]}' >/dev/null
  for _ in $(seq 1 300); do grep -q "waiting on acme/w#1 (held by autopilot/on_new_comment)" "$w/daemon.log" && break; sleep 0.1; done
  grep -q "waiting on acme/w#1 (held by autopilot/on_new_comment)" "$w/daemon.log" &&
    ok "the review's fixer waits on the PR" || bad "the review's fixer did not wait"
  # Let every fixer finish, the first and then the second.
  ( for _ in $(seq 1 1200); do
      for r in $(find "$WORK/xdg" -name .e2e-ready 2>/dev/null); do touch "$(dirname "$r")/.e2e-go"; done
      sleep 0.1
    done ) & local mover=$!
  for _ in $(seq 1 1200); do
    grep -q 'notify \[complete\] acme/w#1 changes_requested' "$w/daemon.log" && break; sleep 0.1
  done
  kill "$mover" 2>/dev/null; wait "$mover" 2>/dev/null || true
  kill "$DAEMON_PID" 2>/dev/null; wait "$DAEMON_PID" 2>/dev/null || true
  DAEMON_PID="" # reaped: its pid may be reused, so cleanup must not kill it
  # The second fixer worked on the head the first pushed (read when its step
  # dispatched, not its event's), so its push is a plain fast-forward: both
  # fixes are on the branch, and nothing went through the moved-head steps.
  local main; main="$(git -C "$WORK/forge/acme/w.git" show feat:main.go 2>/dev/null)"
  case "$main" in *"return 10"*"return 20"*) ok "both fixes are on the branch" ;; *) bad "main.go on the branch: $main" ;; esac
  grep -q "changes_requested\].*the target moved since its event" "$w/daemon.log" &&
    ok "the second fixer started on the current head" || bad "the second fixer did not read the current head"
  if grep -q "changes_requested\].*diverged\|changes_requested\].*step landed" "$w/daemon.log"; then
    bad "the second fixer's push was not a fast-forward"; grep 'changes_requested\]' "$w/daemon.log" | cut -c1-250 | sed 's/^/    /'
  else
    ok "the second fixer's push was a fast-forward"
  fi
  # The review's run started only after the comment's run ended.
  local a_end b_start
  a_end="$(grep -n 'new_comment\].* step ' "$w/daemon.log" | tail -1 | cut -d: -f1)"
  b_start="$(grep -n 'changes_requested\].* step ' "$w/daemon.log" | head -1 | cut -d: -f1)"
  [ -n "$a_end" ] && [ -n "$b_start" ] && [ "$b_start" -gt "$a_end" ] &&
    ok "the second fixer ran after the first" || { bad "runs overlapped (first ended at line ${a_end:-?}, second started at ${b_start:-?})"; grep -v '^plugin ' "$w/daemon.log" | cut -c1-250 | tail -40 | sed 's/^/    /'; }
}

[ -n "${ONLY_THREAD:-}${ONLY_MERGED:-}${ONLY_PROGRESS:-}" ] || {
echo "== one fixer per PR: a second event's fixer waits for the first =="
serial
}

# progress: the PR's status rows while runs wait and work, through the
# daemon. A comment's fixer says what it is doing (step.progress); a
# changes-requested review arrives meanwhile and waits on the PR. Every
# status GitHub got is read back in order: the sequence a person watching the
# PR saw.
prhead() { curl -fsS "$FAKE/_fake/q?what=pr&repo=acme/w&number=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["head_sha"])'; }
progress_run() {
  local w="$WORK/progress" port dport
  mkdir -p "$w"
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  dport="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  HOOK_URL="http://127.0.0.1:$port/webhook" start_fake "$w/ghfake.log"
  start_forge
  cat > "$WORK/forge/acme/w.git/hooks/post-receive" <<HOOK
#!/usr/bin/env bash
while read -r old new ref; do
  [ "\$ref" = refs/heads/feat ] || continue
  curl -fsS -X POST "$FAKE/_fake/act" -H 'content-type: application/json' \\
    -d "{\"op\":\"push_sha\",\"repo\":\"acme/w\",\"branch\":\"feat\",\"by\":\"me\",\"sha\":\"\$new\"}" >/dev/null
done
HOOK
  chmod +x "$WORK/forge/acme/w.git/hooks/post-receive"
  # The dashboard serves localhost only here; public_url is where a person
  # would reach it, so a run's link is built from it.
  cat > "$w/config.yaml" <<YAML
connectors:
  gh:
    use: $WORK/bin/conductor-github
    api_base: $FAKE
    identity: { read_token: tok-me, write_token: tok-me }
    me: { logins: [me] }
    webhook: { listen: "127.0.0.1:$port", secret: e2e }
    sweep: { enabled: false }
sandbox:
  push: { sign: false }
runtimes:
  claude-cli: { use: cli, tool: claude-code, bin: $WORK/bin/claude, default: true }
dashboard:
  enabled: true
  port: $dport
  public_url: https://dash.example.invalid
packs:
  autopilot:
    source: $PACK
    triggers:
      on_new_comment:       { enabled: true, repos: [acme/w] }
      on_changes_requested: { enabled: true, repos: [acme/w] }
YAML
  "$WORK/bin/conductor" init --config "$w/config.yaml" >"$w/init.log" 2>&1 || { echo "init failed:"; cat "$w/init.log"; return 1; }
  PATH="$WORK/bin:$PATH" "$WORK/bin/conductor" run --config "$w/config.yaml" >"$w/daemon.log" 2>&1 & DAEMON_PID=$!
  # The plugin's listener is up (its port answers before the jailed plugin
  # behind it does).
  for _ in $(seq 1 600); do grep -qs "webhook listener on" "$w/daemon.log" && break; sleep 0.1; done

  # The PR's heads over the scenario: statuses go on the head a run saw.
  local start_sha heads; start_sha="$(prhead)"; heads="$start_sha"
  fake act '{"op":"comment","repo":"acme/w","number":1,"by":"rev","body":"PROGRESS-E2E: please make a() return 10"}' >/dev/null
  for _ in $(seq 1 300); do grep -q "notify \[dispatch\] acme/w#1 new_comment" "$w/daemon.log" && break; sleep 0.1; done
  sleep 3
  fake act '{"op":"review","repo":"acme/w","number":1,"by":"rev","state":"CHANGES_REQUESTED","body":"please make b() return 20","comments":[]}' >/dev/null
  # The review's fixer (not a progress one) waits for the harness's go.
  ( for _ in $(seq 1 3000); do
      for r in $(find "$WORK/xdg" -name .e2e-ready 2>/dev/null); do touch "$(dirname "$r")/.e2e-go"; done
      sleep 0.1
    done ) & local mover=$!
  for _ in $(seq 1 3000); do
    grep -q 'notify \[complete\] acme/w#1 new_comment' "$w/daemon.log" && break; sleep 0.1
  done
  heads="$heads $(prhead)"
  for _ in $(seq 1 3000); do
    grep -q 'notify \[complete\] acme/w#1 changes_requested' "$w/daemon.log" && break; sleep 0.1
  done
  heads="$heads $(prhead)"
  kill "$mover" 2>/dev/null; wait "$mover" 2>/dev/null || true
  kill "$DAEMON_PID" 2>/dev/null; wait "$DAEMON_PID" 2>/dev/null || true
  DAEMON_PID=""
  # Every status, oldest first, on every head the PR had.
  for sha in $(echo $heads | tr ' ' '\n' | awk '!seen[$0]++'); do
    curl -fsS -H 'Authorization: token tok-me' "$FAKE/repos/acme/w/commits/$sha/statuses?per_page=100" 2>/dev/null |
      python3 -c '
import json,sys
for s in json.load(sys.stdin):
    print("%d\t%s\t%s\t%s\t%s\t%s" % (s["id"], sys.argv[1][:7], s["context"], s["state"], s.get("description") or "", s.get("target_url") or ""))
' "$sha" || true
  done | sort -n | cut -f2- > "$w/statuses.tsv"
  echo "    --- the PR's status rows, in the order GitHub got them"
  cut -f1-4 "$w/statuses.tsv" | sed 's/	/  |  /g; s/^/    /'
  comment="$(awk -F'	' '$2=="me / comment"' "$w/statuses.tsv")"
  review="$(awk -F'	' '$2=="me / review"' "$w/statuses.tsv")"
  echo "$comment" | head -1 | grep -q $'pending	replying to rev.s comment	' && ok "the comment row starts pending" || bad "first comment row: $(echo "$comment" | head -1)"
  for note in "reading the comment" "editing main.go" "running the tests"; do
    echo "$comment" | grep -q "pending	replying to rev.s comment: $note · " && ok "the comment row said: $note" || bad "the comment row never said: $note"
  done
  echo "$comment" | grep -qE "pending	replying to rev.s comment: [^	]* · [0-9]+m	" && ok "elapsed time shows in minutes" || bad "no elapsed minutes on the comment row"
  [ "$(echo "$comment" | tail -1 | cut -f3)" = success ] && ok "the comment row ends success" || bad "last comment row: $(echo "$comment" | tail -1)"
  [ "$(echo "$comment" | awk -F'	' '$3=="pending"' | tail -1 | cut -f1)" = "${start_sha:0:7}" ] && ok "progress went on the commit the run started on" || bad "progress on another commit"
  echo "$review" | grep -q "pending	addressing rev.s review: queued — waiting for a run of autopilot/on_new_comment to finish · " &&
    ok "the review row said it was queued, and behind what" || bad "the review row never said queued: $(echo "$review" | head -3)"
  [ "$(echo "$review" | tail -1 | cut -f3)" = success ] && ok "the review row ends success" || bad "last review row: $(echo "$review" | tail -1)"
  # A queued run has no page yet: its rows link nothing until it starts.
  if awk -F'\t' '$4 !~ /queued/ && $5 !~ /^https:\/\/dash\.example\.invalid\/#\/runs\/r/' "$w/statuses.tsv" | grep -q .; then
    bad "a status without the run's link: $(awk -F'\t' '$4 !~ /queued/ && $5 !~ /dash/' "$w/statuses.tsv" | head -2)"
  else
    ok "every status of a started run links the run on the dashboard"
  fi
  # Pacing: no two progress rows of one run less than 30s apart is not
  # observable here (the fake has no clock), but the count is: one row per
  # change, not one per tick.
  n="$(echo "$comment" | wc -l)"
  [ "$n" -le 12 ] && ok "the comment row changed $n times (no spam)" || bad "the comment row changed $n times"
  grep -q "step.progress failed" "$(find "$WORK/xdg" -name .e2e-progress.log 2>/dev/null | head -1)" 2>/dev/null && bad "a step.progress call failed" || true
}

[ -n "${ONLY_THREAD:-}${ONLY_MERGED:-}${ONLY_SERIAL:-}" ] || {
echo "== progress: the PR's status says what each run is doing, and since when =="
progress_run
}

echo "== $pass passed, $fail failed =="
[ "$fail" = 0 ]
