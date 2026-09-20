#!/bin/sh
set -eu
# The installation knows its own managed files even when this checkout is stale.
if [ -f /opt/netblackbox/manage.py ] && [ ! -L /opt/netblackbox/manage.py ]; then
    exec python3 /opt/netblackbox/manage.py uninstall "$@"
fi
exec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/manage.py" uninstall "$@"
