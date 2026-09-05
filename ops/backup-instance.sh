#!/bin/bash
# Exécuter en root. /etc/jobwatch/backup.env contient RESTIC_REPOSITORY et RESTIC_PASSWORD_FILE.
set -euo pipefail
umask 077
slug=${1:?instance requise}
[[ "$slug" =~ ^[a-z0-9][a-z0-9_-]{0,22}$ ]] || exit 2
export XDG_CONFIG_HOME=/etc XDG_DATA_HOME=/var/lib
exec 9>/run/lock/jobwatch-operations.lock
flock 9
systemctl stop "jobwatch-collect@$slug.timer"
restore_services() {
    systemctl start "jobwatch@$slug.service" "jobwatch-collect@$slug.timer"
}
trap restore_services EXIT
systemctl stop "jobwatch@$slug.service" "jobwatch-collect@$slug.service"
exec 8>"/var/lib/jobwatch/instances/$slug/maintenance.lock"
flock -x 8
snapshot="/var/backups/jobwatch/$slug/$(date -u +%Y%m%dT%H%M%SZ)"
/opt/jobwatch/current/.venv/bin/jw --instance "$slug" backup "$snapshot"
flock -u 8
restore_services
trap - EXIT
set -a
source /etc/jobwatch/backup.env
set +a
restic backup --tag "jobwatch-$slug" "$snapshot"
# Aucun effacement automatique. Configurer la rétention après une restauration testée.
