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
cleanup() { [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null; [ -n "${KEEP:-}" ] && echo "kept $WORK" || rm -rf "$WORK"; }
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

# scenario <name> <theirs main.go> [<comment>] [review]: a fresh world; their
# push lands while the fixer works (none when <theirs> is empty). With
# `review`, the comment is an inline thread of a COMMENT review on main.go.
scenario() {
  local name="$1" theirs="$2" comment="${3:-please make a() return 10}" kind="${4:-comment}" w="$WORK/$1"
  mkdir -p "$w"
  [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null && wait "$FAKE_PID" 2>/dev/null || true
  rm -f "$WORK/fake.url"
  "$WORK/bin/ghfake" -url-file "$WORK/fake.url" >"$w/ghfake.log" 2>&1 & FAKE_PID=$!
  for _ in $(seq 1 100); do [ -s "$WORK/fake.url" ] && break; sleep 0.05; done
  FAKE="$(cat "$WORK/fake.url")"
  fake seed '{"users":[{"login":"acme","type":"Organization"},{"login":"me"},{"login":"rev"}],"tokens":{"tok-me":"me","tok-rev":"rev"},"repos":[{"name":"acme/w","collaborators":["rev"]}],"hooks":[{"url":"http://127.0.0.1:9/nowhere","secret":"e2e"}],"prs":[{"repo":"acme/w","author":"me","head":"feat","base":"main","title":"feature"}]}' >/dev/null
  python3 - "$BASE_GO" <<'PY' | fake act @- >/dev/null
import json,sys; print(json.dumps({"op":"commit_files","repo":"acme/w","branch":"feat","by":"me","message":"base","files":{"main.go":sys.argv[1],"FEATURE.md":"the feature\n"}}))
PY

  # The same branch in git.
  rm -rf "$WORK/forge" "$WORK/seed" "$XDG_STATE_HOME/conductor/state.json" "$XDG_STATE_HOME/conductor/worktrees" "$XDG_STATE_HOME/conductor/checkouts"
  mkdir -p "$WORK/forge/acme"
  git init -q --bare "$WORK/forge/acme/w.git"
  git init -q "$WORK/seed" && printf '%s' "$BASE_GO" > "$WORK/seed/main.go"
  git -C "$WORK/seed" add main.go && git -C "$WORK/seed" commit -qm base
  git -C "$WORK/seed" push -q "$WORK/forge/acme/w.git" main
  printf 'the feature\n' > "$WORK/seed/FEATURE.md"
  git -C "$WORK/seed" add FEATURE.md && git -C "$WORK/seed" commit -qm feature
  git -C "$WORK/seed" push -q "$WORK/forge/acme/w.git" main:feat main:refs/pull/1/head

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
[ -n "${ONLY_THREAD:-}" ] || {
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

echo "== $pass passed, $fail failed =="
[ "$fail" = 0 ]
