#!/bin/bash
# Version déjà installée et testée dans /opt/jobwatch/releases/<commit>.
# Le lien current change uniquement après préparation. Exécuter en root.
set -euo pipefail
release=${1:?répertoire de version requis}
shift
(( $# > 0 )) || exit 2
[[ "$release" =~ ^/opt/jobwatch/releases/[a-f0-9]{40}$ ]] || exit 2
test -x "$release/.venv/bin/jw"
for slug in "$@"; do
    [[ "$slug" =~ ^[a-z0-9][a-z0-9_-]{0,22}$ ]] || exit 2
done
exec 9>/run/lock/jobwatch-operations.lock
flock 9
previous=$(readlink -f /opt/jobwatch/current)
test -x "$previous/.venv/bin/jw"
rollback() {
    ln -s "$previous" /opt/jobwatch/current.rollback
    mv -Tf /opt/jobwatch/current.rollback /opt/jobwatch/current
    for instance in "$@"; do
        systemctl restart "jobwatch@$instance.service"
        systemctl start "jobwatch-collect@$instance.timer"
    done
}
trap 'rollback "$@"' ERR
for slug in "$@"; do
    systemctl stop "jobwatch-collect@$slug.timer" "jobwatch-collect@$slug.service" "jobwatch@$slug.service"
done
ln -s "$release" /opt/jobwatch/current.next
mv -Tf /opt/jobwatch/current.next /opt/jobwatch/current
for slug in "$@"; do
    systemctl start "jobwatch@$slug.service"
    source "/etc/jobwatch/instances/$slug/service.env"
    curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 \
        --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null
done
for slug in "$@"; do systemctl start "jobwatch-collect@$slug.timer"; done
trap - ERR
