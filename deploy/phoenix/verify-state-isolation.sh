#!/usr/bin/env bash
# Probe Unix permissions as the role accounts. This does not start services or read secrets.
set -euo pipefail

check() {
    local expected=$1
    shift
    if "$@"; then
        [[ "$expected" == allow ]]
    else
        [[ "$expected" == deny ]]
    fi
}

if [[ $# == 1 && $1 == --fixture ]]; then
    [[ $EUID == 0 ]] || { echo 'Fixture requires root to switch to disposable numeric UIDs.' >&2; exit 77; }
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    chmod 0755 "$work"
    supervisor=(setpriv --reuid=62001 --regid=62001 --clear-groups -- /usr/bin/test)
    research=(setpriv --reuid=62002 --regid=62002 --clear-groups -- /usr/bin/test)
    install -d -o 62001 -g 62001 -m 0700 "$work/auth"
    install -d -o 62002 -g 62002 -m 0700 "$work/research"
    install -o 62001 -g 62001 -m 0600 /dev/null "$work/supervisor.env"
    for path in auth.sqlite auth.sqlite-wal auth.sqlite-shm routing.json; do
        install -o 62001 -g 62001 -m 0600 /dev/null "$work/auth/$path"
    done
    auth_db="$work/auth/auth.sqlite"
    research_dir="$work/research"
    supervisor_env="$work/supervisor.env"
    routing="$work/auth/routing.json"
elif [[ $# == 6 && $1 == --installed && $2 == --disposable ]]; then
    [[ $EUID == 0 ]] || { echo 'Installed probe requires root to run as the role accounts.' >&2; exit 77; }
    supervisor=(runuser -u phoenix-supervisor -- /usr/bin/test)
    research=(runuser -u phoenix-research -- /usr/bin/test)
    auth_db=$(realpath -e "$3")
    research_dir=$(realpath -e "$4")
    supervisor_env=$(realpath -e "$5")
    routing=$(realpath -e "$6")
else
    echo 'Usage: verify-state-isolation.sh --fixture | --installed --disposable AUTH_DB RESEARCH_DIR SUPERVISOR_ENV ROUTING_PROJECTION' >&2
    exit 64
fi

[[ -f "$auth_db" && -f "$supervisor_env" && -f "$routing" && -d "$research_dir" ]]
auth_dir=$(dirname "$auth_db")
routing_dir=$(dirname "$routing")
check allow "${research[@]}" -w "$research_dir"
check allow "${research[@]}" -x "$research_dir"
check allow "${supervisor[@]}" -r "$auth_db"
check allow "${supervisor[@]}" -w "$auth_db"
check allow "${supervisor[@]}" -w "$auth_dir"
check allow "${supervisor[@]}" -x "$auth_dir"
check allow "${supervisor[@]}" -w "$routing_dir"
check deny "${research[@]}" -r "$supervisor_env"
check deny "${research[@]}" -w "$supervisor_env"
check deny "${research[@]}" -w "$(dirname "$supervisor_env")"
protected_paths_are_read_only() {
    local path
    for path in "$auth_dir" "$auth_db" "$auth_db-wal" "$auth_db-shm" "$auth_db-journal" "$routing_dir" "$routing"; do
        if ! check deny "${research[@]}" -w "$path"; then
            echo "Research can write authorization state or its containing directory: $path" >&2
            return 1
        fi
    done
}

protected_paths_are_read_only
if [[ $1 == --fixture ]]; then
    # A shared-group writable directory permits replacing even a read-only database.
    chgrp 62002 "$auth_dir"
    chmod 0770 "$auth_dir"
    if protected_paths_are_read_only 2>/dev/null; then
        echo 'Probe accepted a writable authorization directory.' >&2
        exit 1
    fi
fi
echo 'POSIX role permission checks passed.'
