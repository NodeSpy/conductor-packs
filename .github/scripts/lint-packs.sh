#!/usr/bin/env bash
# Lint every pack with the latest conductor release. A pack whose
# requires.conductor floor is newer than that release targets an unreleased
# daemon; it is reported and skipped rather than linted against a binary that
# predates the fields it uses.
set -euo pipefail

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export HOME="$work/home" XDG_CONFIG_HOME="$work/config" XDG_STATE_HOME="$work/state" XDG_CACHE_HOME="$work/cache"
mkdir -p "$HOME"

tag="$(gh release view --repo NodeSpy/conductor --json tagName --jq .tagName)"
gh release download "$tag" --repo NodeSpy/conductor --dir "$work" --pattern conductor_linux_amd64 --pattern checksums.txt
(cd "$work" && grep ' conductor_linux_amd64$' checksums.txt | sha256sum -c -)
bin="$work/conductor_linux_amd64"
chmod +x "$bin"
released="${tag#v}"
echo "conductor $released"

newer() { # newer A B: A > B as versions
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}

status=0
for manifest in */conductor-pack.yaml; do
  dir="$(dirname "$manifest")"
  floor="$(sed -n 's/^ *conductor: *"*>= *\([0-9][0-9.]*\)"*.*/\1/p' "$manifest" | head -1)"
  if [ -n "$floor" ] && newer "$floor" "$released"; then
    echo "::notice::$dir requires conductor >=$floor; latest release is $released — lint skipped until it ships"
    continue
  fi
  echo "== $dir"
  "$bin" pack lint "$dir" || status=1
done
exit "$status"
