#!/bin/bash
# ea-service-run.sh: the macOS stand-in for systemd's EnvironmentFile=.
#
# launchd runs the EA's owner-side services (the chat bot, the mail mediator)
# through this launcher, installed root-owned at
# /usr/local/libexec/ea-service-run.sh by deploy/macos/install-services.sh.
# It loads the owner-private environment files, then execs the service.
#
#   ea-service-run.sh [--env FILE]... -- PROGRAM [ARG]...
#
# Each --env file is read as KEY=VALUE lines, the subset systemd accepts:
# blank lines and '#'/';' comments skipped, one pair of matching outer quotes
# stripped. Nothing is evaluated as shell. A missing file is skipped, as
# `EnvironmentFile=-` does. A present file must be a regular file owned by
# this process's user and writable by no one else, in a directory that is
# the same; otherwise the service does not start. ea-bot can write neither.
set -euo pipefail

die() { echo "ea-service-run: $*" >&2; exit 78; }   # 78: EX_CONFIG

owner_only() {   # $1: path; true when owned by us and not group/world-writable
    [ -O "$1" ] && [ ! -L "$1" ] && [ $(( 0$(stat -f %Lp "$1") & 022 )) -eq 0 ]
}

load_env() {
    local f="$1" line key val
    [ -e "$f" ] || [ -L "$f" ] || return 0
    [ -f "$f" ] || die "$f is not a regular file"
    owner_only "$f" || die "$f must be owned by $(id -un) and writable by no one else"
    owner_only "$(dirname "$f")" || die "$(dirname "$f") must be owned by $(id -un) and writable by no one else"
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in ''|'#'*|';'*) continue ;; esac
        key="${line%%=*}"
        [ "$key" != "$line" ] || continue
        key="${key%"${key##*[![:space:]]}"}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        val="${line#*=}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        if [ "${#val}" -ge 2 ]; then
            case "$val" in
                \"*\") val="${val:1:${#val}-2}" ;;
                \'*\') val="${val:1:${#val}-2}" ;;
            esac
        fi
        export "$key=$val"
    done < "$f"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --env) [ $# -ge 2 ] || die "--env needs a file"; load_env "$2"; shift 2 ;;
        --) shift; break ;;
        *) die "unexpected argument: $1" ;;
    esac
done
[ $# -gt 0 ] || die "no program given"
exec "$@"
