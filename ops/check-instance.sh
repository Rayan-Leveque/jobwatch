#!/bin/bash
set -euo pipefail
slug=${1:?instance requise}
[[ "$slug" =~ ^[a-z0-9][a-z0-9_-]{0,22}$ ]] || exit 2
source "/etc/jobwatch/instances/$slug/service.env"
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$PORT/healthz" >/dev/null
/opt/jobwatch/current/.venv/bin/jw --instance "$slug" check
