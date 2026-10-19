#!/bin/bash
# Installe le poste d'envoi jobwatch sur un Mac : vérifie les outils, enregistre
# le jeton de l'extension Playwright et l'accès SSH au serveur, puis installe une
# tâche launchd lancée toutes les 2 minutes. Relançable sans risque.
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

echo
echo "1. Dans ton Chrome habituel, installe l'extension « Playwright MCP Bridge »"
echo "   (Chrome Web Store), ouvre-la et copie le jeton qu'elle affiche."
read -rsp "Jeton de l'extension (masqué) : " TOKEN; echo
[ -n "$TOKEN" ] || { echo "jeton vide"; exit 1; }

DEFAULT_SSH="ssh -p 2222 rayan@100.116.111.35"
read -rp "Accès SSH au VPS [$DEFAULT_SSH] : " SSH_CMD
SSH_CMD="${SSH_CMD:-$DEFAULT_SSH}"
# shellcheck disable=SC2086
$SSH_CMD -o BatchMode=yes -o ConnectTimeout=8 true \
  || { echo "SSH sans mot de passe impossible avec : $SSH_CMD"; exit 1; }

mkdir -p "$APP" "$CONF" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$CONF"
cp "$HERE/jobwatch-worker.mjs" "$APP/jobwatch-worker.mjs"
umask 077
cat >"$CONF/env" <<EOF
export JOBWATCH_SSH='$SSH_CMD'
export PLAYWRIGHT_MCP_EXTENSION_TOKEN='$TOKEN'
export CLAUDE_BIN='$(command -v claude)'
export PLAYWRIGHT_CLI='$(command -v playwright-cli)'
export PATH='$(dirname "$(command -v node)"):$(dirname "$(command -v claude)"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
EOF
umask 022
cat >"$APP/run.sh" <<EOF
#!/bin/bash
. "$CONF/env"
exec "$(command -v node)" "$APP/jobwatch-worker.mjs"
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
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Poste d'envoi installé. Journal : tail -f ~/Library/Logs/jobwatch-worker.log"
echo "Désinstaller : launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
