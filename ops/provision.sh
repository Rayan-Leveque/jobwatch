#!/bin/bash
# Prépare une instance veille/suivi. Exécuter en root après installation de la version.
set -euo pipefail
umask 077
slug=${1:?instance requise}
email=${2:?email requis}
domain=${3:?nom DNS requis}
port=${4:?port local requis}
[[ "$slug" =~ ^[a-z0-9][a-z0-9_-]{0,22}$ ]] || exit 2
[[ "$domain" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$domain" == *.* ]] || exit 2
[[ "$port" =~ ^[0-9]{4,5}$ ]] && (( 10#$port >= 1024 && 10#$port <= 65535 )) || exit 2
[[ "$email" == *@* && "$email" != *@ && "$email" != @* ]] || exit 2
(( EUID == 0 )) || { echo 'Exécuter en root.' >&2; exit 1; }
test -x /opt/jobwatch/current/.venv/bin/jw
test ! -e "/etc/jobwatch/instances/$slug"
test ! -e "/var/lib/jobwatch/instances/$slug"
if id "jobwatch-$slug" >/dev/null 2>&1; then exit 1; fi
useradd --system --user-group --no-create-home --home-dir "/var/lib/jobwatch/instances/$slug" \
    --shell /usr/sbin/nologin "jobwatch-$slug"
install -d -m 755 /etc/jobwatch /etc/jobwatch/instances \
    /var/lib/jobwatch /var/lib/jobwatch/instances
export XDG_CONFIG_HOME=/etc XDG_DATA_HOME=/var/lib
/opt/jobwatch/current/.venv/bin/jw --instance "$slug" init --beta
printf 'PORT=%s\n' "$port" >"/etc/jobwatch/instances/$slug/service.env"
/opt/jobwatch/current/.venv/bin/jw --instance "$slug" account invite "$email"
chown -R "jobwatch-$slug:jobwatch-$slug" "/var/lib/jobwatch/instances/$slug"
chown -R "root:jobwatch-$slug" "/etc/jobwatch/instances/$slug"
chmod 750 "/etc/jobwatch/instances/$slug"
chmod 640 "/etc/jobwatch/instances/$slug/config.yaml" "/etc/jobwatch/instances/$slug/service.env"
sed -e "s/alice.jobs.example/$domain/g" -e "s/8801/$port/g" \
    /opt/jobwatch/current/ops/nginx.conf >"/etc/jobwatch/instances/$slug/nginx.conf"
systemctl enable --now "jobwatch@$slug.service" "jobwatch-collect@$slug.timer" "jobwatch-check@$slug.timer"
printf 'Instance prête. Configurer le certificat et le proxy pour https://%s avant de transmettre le lien.\n' "$domain"
