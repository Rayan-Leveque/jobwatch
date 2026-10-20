#!/bin/sh
# Commande forcée de la clé SSH « mail-code » du poste d'envoi (authorized_keys) :
#   command="<ce script> <commande jw>",restrict ssh-ed25519 ...
# L'agent Codex tourne dans un bac à sable qui ne lit pas ~/.ssh : il ne dispose
# que de cette clé, qui ne peut rien faire d'autre que `jw mail-code --minutes N`.
set -eu
refuse() { echo "seul jw mail-code --minutes <entier> est autorisé" >&2; exit 1; }
case "${SSH_ORIGINAL_COMMAND:-}" in
  "mail-code --minutes "*) minutes=${SSH_ORIGINAL_COMMAND#mail-code --minutes } ;;
  *) refuse ;;
esac
case "$minutes" in ''|*[!0-9]*) refuse ;; esac
exec "$@" mail-code --minutes "$minutes"
