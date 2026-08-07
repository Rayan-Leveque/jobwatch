# jobwatch

Observateur d'offres d'emploi auto-hébergé. Il collecte les offres d'emploi via les API des
job boards, les déduplique dans une base SQLite locale, les met en correspondance avec vos
recherches enregistrées, envoie un digest des nouveaux matchs par notification, et vous permet
de suivre vos candidatures depuis la ligne de commande ou via un tableau de bord web local.

Flux : **collecter -> dédupliquer -> matcher -> notifier -> suivre**.

Pas de fonctionnalités LLM intégrées, pas de cloud, pas de traçage. Vos données restent dans un seul
fichier SQLite sur votre machine.

## Démarrage rapide

```bash
git clone <ce repo> && cd jobwatch
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/jw init                # crée config.yaml + une base de données vide
# éditez config.yaml : décommentez et remplissez les blocs sources et notify, puis :
.venv/bin/jw run                # collecter, matcher, notifier
.venv/bin/jw serve              # tableau de bord web local : http://127.0.0.1:8000
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

### Import des artefacts et résumés

La v0.4 importe les artefacts produits par votre veille quotidienne : offres web, fit LLM,
candidatures, échéances, documents et résumés factuels.

```bash
.venv/bin/jw ingest-daily --api-json offres.json --config config.yaml            # plancher API
.venv/bin/jw ingest-daily --digest digest.md --config config.yaml                # offres web + fit LLM
.venv/bin/jw import-md /chemin/vers/suivi_candidatures.md --config config.yaml
.venv/bin/jw import-summaries /chemin/vers/resumes.md --config config.yaml
```

`jw ingest-daily` exige au moins l'un de `--api-json` ou `--digest`. Le JSON API est le plancher
de collecte ; le digest Markdown apporte les offres web et le fit LLM (`high`, `medium`, `low`).
Les offres sont dédupliquées par URL et associées à une recherche (`--search-name`, défaut
`veille-importee`). `jw import-md` migre le suivi des candidatures (offres, candidatures,
échéances, documents) depuis un tracker Markdown (défaut `--search-name suivi-importe`). Les deux
imports sont atomiques et idempotents : relancer les mêmes artefacts ne crée aucun doublon et ne
rétrograde jamais un état existant.

`jw import-summaries` attend des sections `## URL` suivies d'au moins un bullet `- ...`. Chaque URL
doit correspondre exactement à une offre déjà présente : si l'une manque, tout l'import est annulé
et aucune offre fantôme n'est créée. Un nouvel import identique ne modifie rien ; si les bullets
d'une section changent, ils remplacent intégralement le résumé stocké en conservant leur ordre.

### Migration progressive

Le cron ai-job-search continue sa collecte HTTP et sa recherche large Claude. Le bridge appelle
jobwatch en mode dégradable via `JOBWATCH_DIR` (`~/jobwatch` par défaut) et `JOBWATCH_CONFIG`
(`~/.config/jobwatch/config.yaml` par défaut). Le bridge n'envoie aucune notification
supplémentaire et son échec ne casse pas le cron historique. Une fois la parité vérifiée,
SQLite/jobwatch devient la source de vérité.

## Tableau de bord local

`jw serve` sert un tableau de bord en lecture seule qui relit la base SQLite à chaque
chargement de page. La section `Priorité haute` regroupe les matchs high avant `Nouveaux matchs`
et `Vus`; les cartes high disposant d'un résumé affichent un bloc `En bref`, repliable en cliquant
sur la carte ou au clavier :

```bash
.venv/bin/jw serve                        # http://127.0.0.1:8000 (défaut)
.venv/bin/jw serve --port 9000            # autre port
.venv/bin/jw serve --host 0.0.0.0         # accessible depuis d'autres machines
```

`--host 0.0.0.0` rend le tableau de bord accessible à toutes les machines joignables sur
votre réseau. Le tableau de bord reste en lecture seule, mais il expose vos offres et
candidatures : réfléchissez à qui y a accès. Préférez l'accès local (`127.0.0.1`, le défaut)
ou une adresse privée, et ne le publiez pas tel quel sur Internet.

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
| `offer_summary` | Résumé factuel unique associé à une offre existante |
| `summary_bullet` | Bullets d'un résumé avec leur position explicite |
| `search` | Recherches enregistrées (mots-clés, localisations, contrat) |
| `match` | Paire offre/recherche avec état et statut de notification |
| `application` | Votre candidature pour une offre |
| `event` | Historique d'une candidature (applied, interview, rejected, offer, ...) |
| `document` | Fichiers CV et lettre de motivation attachés à une candidature |

## Feuille de route

- v0.2 : tableau de bord local en lecture seule (`jw serve`).
- v0.3 : import des artefacts quotidiens (`jw ingest-daily`) et du suivi Markdown (`jw import-md`), fit LLM, échéances et documents.
- v0.4 : résumés high stockés dans SQLite et affichés dans le tableau de bord.

## Licence

MIT. Copyright 2026 Rayan Leveque.
