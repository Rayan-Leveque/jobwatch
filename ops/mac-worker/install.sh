#!/bin/bash
# Installe le poste d'envoi jobwatch sur un Mac : vérifie les outils et l'accès
# SSH au serveur, installe une tâche launchd lancée toutes les 2 minutes, puis
# ouvre une fois le Chrome dédié pour s'y connecter. Relançable sans risque.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Library/Application Support/jobwatch-worker"
CONF="$HOME/.config/jobwatch-worker"
PLIST="$HOME/Library/LaunchAgents/cloud.jobwatch.worker.plist"
LABEL="cloud.jobwatch.worker"

need() { command -v "$1" >/dev/null 2>&1 || { echo "manquant : $1 - $2"; exit 1; }; }
need node "installe Node (https://nodejs.org ou brew install node)"
need claude "installe Claude Code (https://claude.com/claude-code) puis connecte-toi avec 'claude'"
if ! command -v playwright-cli >/dev/null 2>&1; then
  echo "installation de playwright-cli…"
  npm install -g @playwright/cli@0.1.22
fi

if [ ! -d /Applications/SwiftBar.app ] && command -v brew >/dev/null 2>&1; then
  echo "installation de SwiftBar (icône de barre des menus)…"
  brew install --cask swiftbar
fi

DEFAULT_SSH="ssh -p 2222 rayan@100.116.111.35"
read -rp "Accès SSH au VPS [$DEFAULT_SSH] : " SSH_CMD
SSH_CMD="${SSH_CMD:-$DEFAULT_SSH}"
# shellcheck disable=SC2086
$SSH_CMD -o BatchMode=yes -o ConnectTimeout=8 true \
  || { echo "SSH sans mot de passe impossible avec : $SSH_CMD"; exit 1; }

mkdir -p "$APP/menubar" "$CONF" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$CONF"
cp "$HERE/jobwatch-worker.mjs" "$HERE/jobwatch-menubar.mjs" "$APP/"
umask 077
cat >"$CONF/env" <<EOF
export JOBWATCH_SSH='$SSH_CMD'
export CLAUDE_BIN='$(command -v claude)'
export PLAYWRIGHT_CLI='$(command -v playwright-cli)'
export PATH='$(dirname "$(command -v node)"):$(dirname "$(command -v claude)"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
EOF
umask 022
# Le lanceur récupère la dernière version du worker et de l'icône sur le serveur :
# plus besoin de réinstaller après une mise à jour. Sans réseau, il garde la
# version locale.
cat >"$APP/run.sh" <<EOF
#!/bin/bash
. "$CONF/env"
for name in jobwatch-worker.mjs jobwatch-menubar.mjs; do
  \$JOBWATCH_SSH -o BatchMode=yes -o ConnectTimeout=8 \\
    "cat /home/rayan/jobwatch-prod/ops/mac-worker/\$name" >"$APP/.new.mjs" 2>/dev/null \\
    && node --check "$APP/.new.mjs" 2>/dev/null \\
    && mv "$APP/.new.mjs" "$APP/\$name"
done
rm -f "$APP/.new.mjs"
exec "$(command -v node)" "$APP/jobwatch-worker.mjs" "\$@"
EOF
chmod 700 "$APP/run.sh"

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP/run.sh</string></array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/jobwatch-worker.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/jobwatch-worker.log</string>
</dict>
</plist>
EOF
# Clé réservée à `jw mail-code` pour l'agent Codex, dont le bac à sable ne lit pas
# ~/.ssh : le serveur lui impose mail-code-only.sh, elle ne peut rien d'autre.
MAILCODE="$APP/mailcode"
if [ ! -f "$MAILCODE/id_ed25519" ]; then
  mkdir -p "$MAILCODE" && chmod 700 "$MAILCODE"
  ssh-keygen -q -t ed25519 -N "" -C jobwatch-mailcode -f "$MAILCODE/id_ed25519"
  # shellcheck disable=SC2086
  $SSH_CMD -o BatchMode=yes "cat >> ~/.ssh/authorized_keys" <<EOF
command="/home/rayan/jobwatch-prod/ops/mac-worker/mail-code-only.sh /home/rayan/jobwatch-prod/.venv/bin/jw --instance rayan",restrict $(cat "$MAILCODE/id_ed25519.pub")
EOF
fi
HOST_PORT=$(echo "$SSH_CMD" | sed -n 's/.*-p \([0-9]*\).*/\1/p')
HOST=$(echo "$SSH_CMD" | awk '{print $NF}' | sed 's/.*@//')
ssh-keyscan -p "${HOST_PORT:-22}" "$HOST" 2>/dev/null >"$MAILCODE/known_hosts"

# Plugin SwiftBar : SwiftBar n'a pas le PATH de launchd, d'où le chemin complet de node.
cat >"$APP/menubar/jobwatch.30s.sh" <<EOF
#!/bin/bash
exec "$(command -v node)" "$APP/jobwatch-menubar.mjs"
EOF
chmod 700 "$APP/menubar/jobwatch.30s.sh"
if [ -d /Applications/SwiftBar.app ]; then
  defaults write com.ameba.SwiftBar PluginDirectory "$APP/menubar"
  open -a SwiftBar
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Ouverture du Chrome dédié : connecte-toi à LinkedIn (via Google si besoin) et WTTJ,"
echo "puis ferme la fenêtre. À refaire seulement si une session expire :"
echo "  \"$APP/run.sh\" login"
"$APP/run.sh" login
echo
echo "Poste d'envoi installé. Journal : tail -f ~/Library/Logs/jobwatch-worker.log"
echo "Désinstaller : launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
