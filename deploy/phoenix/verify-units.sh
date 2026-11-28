#!/usr/bin/env bash
# Parse units against disposable executable stubs. Never start a service.
set -euo pipefail
units=$(realpath "$(dirname "$0")")
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
for input in "$units"/*.service "$units"/*.timer; do
    sed -e 's#^ExecStart=/opt/phoenix/current/venv/bin/python #ExecStart=/usr/bin/true #' \
        "$input" > "$work/${input##*/}"
done
systemd-analyze verify "$work"/*.service "$work"/*.timer
