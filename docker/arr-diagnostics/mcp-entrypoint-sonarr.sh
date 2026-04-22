#!/bin/sh
set -e
cd /opt/arr-diagnostics
exec python -m arr_diagnostics sonarr
