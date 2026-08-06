# jobwatch

Observateur d'offres d'emploi auto-hébergé. Il collecte les offres d'emploi via les API des
job boards, les déduplique dans une base SQLite locale, les met en correspondance avec vos
recherches enregistrées, envoie un digest des nouveaux matchs par notification, et vous permet
de suivre vos candidatures depuis la ligne de commande.

Flux : **collecter -> dédupliquer -> matcher -> notifier -> suivre**.

Pas de fonctionnalités LLM, pas de cloud, pas de traçage. Vos données restent dans un seul
fichier SQLite sur votre machine.

## Démarrage rapide

```bash
git clone <ce repo> && cd jobwatch
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/jw init                # crée config.yaml + une base de données vide
# éditez config.yaml : décommentez et remplissez les blocs sources et notify, puis :
.venv/bin/jw run                # collecter, matcher, notifier
.venv/bin/jw list               # affiche les nouveaux matchs
.venv/bin/jw apply 1 --note "cv envoyé"
.venv/bin/jw log 1 interview -m "entretien téléphonique"
.venv/bin/jw apps               # candidatures avec leur statut actuel
```

`jw init` refuse d'écraser un `config.yaml` existant. Toutes les commandes acceptent
`--config PATH` (par défaut `./config.yaml`, avec repli sur `~/.config/jobwatch/config.yaml`).

### Cron

Exécutez `jw run` chaque jour via cron :

```
0 7 * * * cd ~/jobwatch && .venv/bin/jw run
```

## Référence de configuration

`config.yaml` (une copie de `config.example.yaml`) comporte quatre sections.

| Clé | Description |
| --- | --- |
| `db` | Chemin vers la base SQLite. `~` est développé. Les répertoires sont créés automatiquement. |
| `searches` | Liste des recherches enregistrées. Chaque recherche a : `name` (identifiant unique), `include` (mots-clés, au moins un, correspondance insensible à la casse sur le titre), `exclude` (mots-clés, aucun), `locations` (correspondance par sous-chaîne sur la localisation de l'offre ; vide = n'importe où), `contract` (optionnel : `permanent`, `fixed_term`, `internship`, `other`). |
| `sources` | Les job boards à surveiller. `france_travail` nécessite `client_id`, `client_secret`, `keywords` (requête côté serveur) et éventuellement `department`. `smartrecruiters` prend une liste de slugs de sociétés. |
| `notify` | Canaux de notification. `ntfy` publie sur `https://ntfy.sh/<topic>`. `smtp` envoie via `host`, `port`, `user`, `password`, `to`. Les deux sont optionnels ; vous pouvez en utiliser un, les deux ou aucun. |

Le filtre `locations` est une correspondance par sous-chaîne sur la localisation de l'offre :
une offre située à « Puteaux » ou « Levallois-Perret » ne matche PAS une recherche avec
`locations: ["Paris"]`. Listez explicitement les communes voulues dans `locations`, ou laissez
la liste vide pour accepter n'importe quelle localisation.

Dans `config.example.yaml`, les blocs `sources` et `notify` sont vides (`{}`) : décommentez-les
et remplissez-les pour activer la collecte et les notifications. Avec la config d'exemple non
modifiée, `jw init && jw run` ne fait aucun appel réseau et ne publie rien.

Les recherches sont synchronisées dans la base à chaque `jw run` : les nouvelles sont insérées,
les modifiées mises à jour, les supprimées désactivées (les matchs existants sont conservés).

## Identifiants France Travail

1. Allez sur https://francetravail.io et créez un compte (ou utilisez votre compte France Travail).
2. Créez une application ("créer une application") pour l'API `api_offresdemploiv2`.
3. Notez le `client_id` et le `client_secret` et placez-les dans `config.yaml`.
4. Demandez le scope `api_offresdemploiv2` pour l'application.

jobwatch exécute ensuite automatiquement le flux OAuth2 client-credentials à chaque `jw run`.
Une source défaillante ou non configurée est consignée comme avertissement et n'interrompt jamais
l'exécution.

## Modèle de données

Les offres sont dédupliquées globalement par URL, et de plus ignorées quand la même société a déjà
une offre avec le même titre. Chaque offre est mise en correspondance avec chaque recherche active ;
les matchs sont stockés avec un état (`new`, `seen`, `applied`, `discarded`). Une candidature est
créée depuis un match, et son statut actuel est le dernier événement de son journal d'événements.

| Table | Rôle |
| --- | --- |
| `source` | Sources de job boards configurées et leur dernière exécution |
| `company` | Sociétés (dédupliquées par nom) |
| `offer` | Offres d'emploi (dédupliquées par URL et société+titre) |
| `search` | Recherches enregistrées (mots-clés, localisations, contrat) |
| `match` | Paire offre/recherche avec état et statut de notification |
| `application` | Votre candidature pour une offre |
| `event` | Historique d'une candidature (applied, interview, rejected, offer, ...) |
| `document` | Fichiers CV et lettre de motivation attachés à une candidature |

## Feuille de route

- v0.2 : tableau de bord (`jw serve`), export markdown des candidatures, plus de sources.
- v0.3 : résumés LLM optionnels et scoring de pertinence pour les offres collectées.

## Licence

MIT. Copyright 2026 Rayan Leveque.
