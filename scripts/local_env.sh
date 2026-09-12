#!/usr/bin/env bash
# Load local.env (git-ignored machine settings) into the environment.
# Sourced by the start/stop scripts and by the launchers that run inside
# tmux; not run directly.
#
# Deliberately sets no shell options: this file is sourced, so `set -u`
# here would quietly change the caller.

: "${REPO_ROOT:=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

if [ -f "$REPO_ROOT/local.env" ]; then
    # Export on load: tmux hands a new window the tmux server's
    # environment, not the caller's, so values meant for a service have to
    # be in the environment of whatever sources this file.
    set -a
    # shellcheck source=../local.env
    . "$REPO_ROOT/local.env"
    set +a
fi
