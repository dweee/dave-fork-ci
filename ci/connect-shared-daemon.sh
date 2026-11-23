#!/usr/bin/env bash
# Connect to the untrusted listener of Icicle's one shared Nix store instead of
# starting a daemon. Tool jobs mount a read-only alias of the store and the
# untrusted socket only; they are never trusted clients and get no shared
# credential tmpfs. The daemon owns substituters and signing trust: a job can
# build, add content-addressed paths and import paths signed by a key the
# daemon trusts (for example Niks3 paths it fetched with its own credential),
# but never unsigned paths or restricted settings. Reviewed copy of Flakey's
# machines/icicle/forgejo-runner/determinate-image/connect-daemon.sh
# (untrusted mode).
#
# Jobs never take the store lock; the daemon container holds it exclusively.
set -euo pipefail
version=$(nix --version)
[[ "$version" == *'Determinate Nix 3.24.1'* ]] || {
  printf 'Unexpected Nix client: %s\n' "$version" >&2
  exit 1
}
socket=/nix/var/nix/daemon-socket/socket
wait=${NIX_DAEMON_CONNECT_WAIT:-120}
info=
for _ in $(seq 1 "$wait"); do
  if [[ -S "$socket" ]] && info=$(env -u NIX_DAEMON_SOCKET_PATH timeout 5 nix store info --store daemon --json 2>/dev/null); then
    break
  fi
  info=
  sleep 1
done
[[ -n "$info" ]] || { echo 'The shared Nix daemon for this store is not running.' >&2; exit 1; }
# Same Nix as this client (identical image), and never a trusted connection.
[[ "$info" == *"\"version\":\"${version##* }\""* && "$info" == *'"trusted":false'* ]] || {
  printf 'Unexpected shared daemon: %s\n' "$info" >&2
  exit 1
}
printf '%s\n' "$info"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf 'NIX_REMOTE=daemon\nNIX_SHARED_DAEMON=1\n' >> "$GITHUB_ENV"
fi
