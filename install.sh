#!/bin/sh
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Install Python first: apt-get update && apt-get install python3' >&2; exit 1; }
exec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/manage.py" install "$@"
