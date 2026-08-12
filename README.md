# jobwatch

Observateur d'offres d'emploi auto-hébergé. Il collecte les offres d'emploi via France Travail,
SmartRecruiters, l'API invitée LinkedIn et l'index public WTTJ, les déduplique dans une base
SQLite locale, les met en correspondance avec vos
recherches enregistrées, envoie un digest des nouveaux matchs par notification, et vous permet
de suivre vos candidatures depuis la ligne de commande ou via un tableau de bord web local.

Flux : **collecter -> dédupliquer -> matcher -> notifier -> suivre**.

Pas de cloud, pas de traçage : le tableau de bord, SQLite et les documents gérés restent 100%
locaux dans le dossier de l'instance sur votre machine. Trois fonctions LLM restent optionnelles
et inertes sans configuration explicite : `research` complète les collecteurs directs par une
recherche web large, `jw enrich` extrait et résume les annonces collectées, et le tableau de bord
peut rédiger des lettres de motivation. Les appels passent par un binaire OpenCode ou Codex local,
lancé en bac à sable : Codex ignore la configuration utilisateur et tourne sans outil local,
OpenCode voit chacun de ses outils refusé nommément (seule `research` rouvre le web). `jw enrich`
accepte aussi Pi comme runner, exécuté sans outil et sans session persistante.

## Démarrage rapide

```bash
git clone <ce repo> && cd jobwatch
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/jw init                # crée config.yaml + une base de données vide
# éditez config.yaml : décommentez et remplissez les blocs sources et notify, puis :
.venv/bin/jw run                # collecter, matcher, notifier
.venv/bin/jw enrich             # récupère et résume les offres collectées (bloc enrich requis)
.venv/bin/jw serve              # tableau de bord web local : http://127.0.0.1:8000
.venv/bin/jw list               # affiche les nouveaux matchs
.venv/bin/jw apply 1 --note "cv envoyé"
.venv/bin/jw log 1 interview -m "entretien téléphonique"
.venv/bin/jw apps               # candidatures avec leur statut actuel
.venv/bin/jw bugs               # signalements envoyés depuis le dashboard
```

`jw init` refuse d'écraser un `config.yaml` existant. `jw init --db PATH` écrit ce chemin
dans la ligne `db:` de la config générée au lieu du défaut `~/.local/share/jobwatch/jobwatch.db`
(utile pour un environnement de test isolé). Toutes les commandes acceptent
`--config PATH` (par défaut `./config.yaml`, avec repli sur `~/.config/jobwatch/config.yaml`).

### Instances isolées

Pour héberger plusieurs personnes avec des données complètement séparées, utilisez une instance
nommée. Chaque instance possède sa configuration, sa base SQLite et son dossier de documents :

```bash
.venv/bin/jw --instance rayan init
.venv/bin/jw --instance rayan run
.venv/bin/jw --instance rayan serve --port 8765

.venv/bin/jw --instance alice init
.venv/bin/jw --instance alice account invite alice@example.com
.venv/bin/jw --instance alice serve --port 8766
```

Les configurations vivent sous `~/.config/jobwatch/instances/<nom>/config.yaml` et toutes les
données sous `~/.local/share/jobwatch/instances/<nom>/`. Les variables XDG sont respectées.
`JOBWATCH_INSTANCE=alice` est équivalent à `--instance alice`, notamment pour cron ou un service.
Un `--config PATH` explicite reste prioritaire.
`account invite` active l'authentification de l'instance et produit un chemin d'invitation
propriétaire valable 48 heures. Ouvrez ce chemin sur le serveur de l'instance pour choisir le mot
de passe. Une instance n'accepte qu'une seule adresse propriétaire : offres, documents et
candidatures y sont communs, donc une deuxième personne a besoin de sa propre `--instance`.
Tant que l'invitation n'a pas été acceptée, la relancer avec une autre adresse remplace la
précédente ; une fois le compte créé, l'adresse ne change plus. Les pages, documents et actions deviennent alors inaccessibles sans session. Le cookie
est réservé à HTTPS par défaut. Pour un serveur HTTP strictement local ou privé, lancez
explicitement `jw --instance alice serve --no-secure-cookie`; ne publiez jamais ce mode sur Internet.

À la première connexion d'une instance nommée, jobwatch ouvre un parcours de démarrage : importez
un ou plusieurs CV PDF pour obtenir des catégories proposées ensemble, ou créez-les manuellement.
Dans le parcours CV, une étape optionnelle propose ensuite de partager une lettre de motivation
(de préférence au format `.tex`) comme exemple de style personnel (`document_library`, type
`letter_example`) ; sans envoi, la génération de lettres retombe sur un modèle générique fourni
avec jobwatch. Avant confirmation, chaque catégorie peut être renommée, ajoutée ou supprimée et
ses mots-clés modifiés. La confirmation enregistre les catégories comme recherches SQLite actives
et relance le matching ; le lien « Modifier mes catégories » reste ensuite disponible depuis le
tableau de bord. Le bloc `draft` doit être configuré pour l'analyse des CV par IA ; le parcours
manuel reste toujours disponible. Chaque fichier CV est vérifié côté serveur comme un PDF et
limité à 10 Mio ; l'exemple de lettre est limité à 10 Mio et doit porter l'extension `.tex`.

### Cron

Exécutez `jw run` chaque jour via cron ; enchaînez `jw enrich` si le bloc `enrich` est configuré :

```
0 7 * * * cd ~/jobwatch && .venv/bin/jw run && .venv/bin/jw enrich
```

### Import des artefacts et résumés

La v0.4 importe les artefacts produits par votre veille quotidienne : offres web, fit LLM,
candidatures, échéances, documents et résumés factuels.

```bash
.venv/bin/jw ingest-daily --api-json offres.json --config config.yaml            # plancher API
.venv/bin/jw ingest-daily --digest digest.md --config config.yaml                # offres web + fit LLM
.venv/bin/jw import-md /chemin/vers/suivi_candidatures.md --config config.yaml
.venv/bin/jw import-summaries /chemin/vers/resumes.md --config config.yaml
.venv/bin/jw migrate-storage --source-root /ancien/workspace --config config.yaml
```

`jw ingest-daily` exige au moins l'un de `--api-json` ou `--digest`. Le JSON API est le plancher
de collecte ; le digest Markdown apporte les offres web et le fit LLM (`high`, `medium`, `low`).
Les offres sont dédupliquées par URL et associées à une recherche (`--search-name`, défaut
`veille-importee`). `jw import-md` migre le suivi des candidatures (offres, candidatures,
échéances, documents) depuis un tracker Markdown (défaut `--search-name suivi-importe`). Les deux
imports sont atomiques et idempotents : relancer les mêmes artefacts ne crée aucun doublon et ne
rétrograde jamais un état existant.

`jw migrate-storage` copie dans le dossier `documents/` de l'instance les CV et lettres encore
référencés par un chemin externe. `--source-root` sert à résoudre les chemins relatifs provenant
d'un ancien tracker. Les références SQLite sont réécrites vers les copies gérées par jobwatch ;
la commande est idempotente et signale chaque fichier source introuvable sans modifier sa ligne.

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

## Enrichissement des offres

`jw enrich` traite les offres **actives** - celles qu'au moins un match `new`, `seen` ou
`later` ou une candidature rend visibles au tableau de bord, quelle que soit leur source
(collecteurs jobwatch comme imports `jw ingest-daily`). La corbeille et les offres sans match
ne coûtent aucun token. Pour chaque offre active sans texte stocké ou sans champs structurés :

1. Récupère la page de l'offre (`url`) en HTTP simple si le texte manque (une offre dont le
   texte est déjà en base est résumée sans aucun accès réseau).
2. Extrait le contenu utile en privilégiant un objet `schema.org/JobPosting`, puis
   `trafilatura`, avec repli sur le Markdown brut si un marqueur important comme le salaire,
   l'expérience ou le télétravail disparaît.
3. Si le fetch HTTP échoue, ou si le Markdown obtenu est vide/trop court pour être une vraie
   annonce, retente via Playwright (Chromium headless) et convertit la page rendue.
4. Stocke le texte retenu dans `offer_content`, avec son statut, sa méthode de récupération,
   sa méthode d'extraction et une copie compressée du HTML brut pour permettre un retraitement.
5. Génère un résumé structuré via le LLM configuré (runner `opencode`, `codex` ou `pi`, en
   subprocess, jusqu'à `concurrency` appels simultanés) : quatre
   champs fixes - Expérience souhaitée, Salaire, Télétravail, Stack, valeur « non précisé »
   quand l'annonce ne dit rien (table `summary_field`) - suivis de puces mission
   (`offer_summary`/`summary_bullet`, `source = 'auto'`). Les puces d'un résumé `manual`
   existant (importé via `jw import-summaries`) ne sont jamais écrasées ; les champs fixes,
   eux, s'ajoutent à tout résumé qui n'en a pas encore. Les citations renvoyées sont conservées
   uniquement lorsqu'elles existent textuellement dans l'annonce extraite.
6. Patiente 1 à 2 secondes entre deux fetchs web pour ne pas marteler les sites tiers.

Un échec (réseau ou LLM) est consigné en avertissement et n'interrompt jamais le traitement des
offres suivantes. Un fetch en échec conserve son nombre de tentatives et sa cause : les erreurs
temporaires sont retentées après 24 heures, avec un plafond de trois tentatives, tandis que les
réponses HTTP 404 et 410 sont terminales. Les anciennes lignes `failed` sans cause enregistrée
reçoivent un retry immédiat afin de migrer le corpus existant vers cette politique bornée. Le
panneau « En bref » du tableau de bord et des cartes de tri affiche les champs étiquetés en tête
puis les puces, pour toute carte ayant un résumé.

`jw enrich` nécessite le bloc `enrich` de `config.yaml` (voir la référence de configuration
ci-dessous) ; sans lui, la commande refuse proprement avec un message clair, sans réseau ni erreur
inattendue.

Playwright nécessite l'installation ponctuelle de son navigateur Chromium :

```bash
.venv/bin/playwright install chromium
```

## Tableau de bord local

`jw serve` sert un tableau de bord qui relit la base SQLite à chaque chargement de page.
Une instance historique sans profil conserve les deux onglets étanches par piste métier : `Ingénieur IA`
sur `/` et `Chef de projet / PO` sur `/po`. Toute offre ou candidature dont le titre
contient « chef de projet », « chef de produit », « product owner » ou « product manager »
n'apparaît que dans l'onglet `Chef de projet / PO` ; l'onglet `Ingénieur IA` montre tout le
reste. Une instance nommée dont le profil est confirmé affiche à la place un flux unifié de toutes
ses catégories ; chaque carte indique la recherche correspondante et le tableau de bord propose
« Modifier mes catégories ». Chaque vue porte ses propres sections et compteurs.
La section `Priorité haute` regroupe les matchs high avant `Nouveaux matchs` et `Vus`. Sous les
actions, une carte réunit ses contenus dans un lecteur à trois onglets : « En bref », « Annonce »
et, lorsque `draft` est configuré, « Lettre ». Une seule vue peut être ouverte à la fois. La
génération, le suivi et la régénération d'une lettre vivent dans l'onglet « Lettre », tandis que
« Plus tard », « Candidater » et « Écarter » restent des décisions séparées.

Chaque carte des sections `Priorité haute`, `Nouveaux matchs`, `Vus` et `À candidater` propose
ses actions dans cet ordre : « Plus tard » (passe le match en `state='later'`, section
`À candidater`), « Candidater » et « Écarter »
(passe le match en `state='discarded'` avec
`discarded_at` horodaté, section `Corbeille`). « Candidater » déplie un petit formulaire avec deux menus
déroulants optionnels - CV et lettre de motivation - peuplés depuis une bibliothèque de
documents réutilisables (table `document_library`) ; à côté de chaque menu, un bouton œil
ouvre le document sélectionné dans un nouvel onglet (`GET /documents/<id>`, désactivé quand
« Aucun » est choisi) et un bouton flèche vers le bas ouvre le sélecteur de fichiers natif
(le glisser-déposer fonctionne aussi), envoie le fichier en base64 vers `POST /documents`, et
sélectionne automatiquement la nouvelle entrée dans le menu. Aucune sélection n'est obligatoire, comme avant. La soumission enregistre en une
seule action la candidature (même logique que `jw apply` : ligne `application`, événement
`applied`, match en `state='applied'`) plus une ligne `document` par champ rempli (`cv` ou
`cover_letter`), en résolvant l'entrée de bibliothèque choisie vers son chemin sur disque. Les
fichiers uploadés sont limités à 10 Mio et stockés sous `<db>/../documents/`, préfixés d'un identifiant aléatoire ;
seul le nom de base du fichier client est utilisé, ce qui empêche toute traversée de chemin.
Ces actions appellent le serveur
en JavaScript (`fetch` POST) et retirent la carte de son emplacement sans recharger la page ;
pour « Plus tard » et « Écarter », un bouton « Annuler » apparaît quelques secondes à
la place de la carte retirée pour revenir à l'état précédent. La section `Corbeille` n'affiche
que les matchs écartés depuis moins de 30 jours (filtre d'affichage pur, réévalué à chaque
chargement) : passé ce délai, la ligne disparaît du tableau de bord mais n'est jamais supprimée
de la base.

```bash
.venv/bin/jw serve                        # http://127.0.0.1:8000 (défaut)
.venv/bin/jw serve --port 9000            # autre port
.venv/bin/jw serve --host 0.0.0.0         # accessible depuis d'autres machines
```

`--host 0.0.0.0` rend le tableau de bord accessible à toutes les machines joignables sur
votre réseau. Une installation historique sans compte conserve son comportement local ouvert.
Pour protéger une instance nommée, créez son invitation avec `account invite` : toutes les routes,
y compris les documents et les actions qui mutent SQLite, exigent alors une session, et les POST
exigent aussi le jeton CSRF de cette session. Les mots de passe font au moins 8 caractères, les
sessions expirent après 24 heures et cinq échecs de connexion bloquent la paire email/adresse
pendant 15 minutes. Préférez HTTPS avec le cookie sécurisé par défaut. `--no-secure-cookie` existe
uniquement pour un accès HTTP local ou sur un réseau privé chiffré comme Tailscale.

Le bouton « Signaler un bug », disponible sur le tableau de bord et dans le swipe, ouvre un
formulaire destiné aux utilisateurs de l'application. Le message, la page courante et le
navigateur sont stockés dans la base SQLite de l'instance, sans créer de compte ni d'issue sur
un service tiers. L'administrateur de l'instance les consulte avec `jw bugs`.

## Tri des offres (swipe)

Quand un onglet contient de nouvelles offres, un popup d'accueil « N nouvelles offres -
C'est le moment de swiper. » s'affiche à l'arrivée sur le tableau de bord (une fois par
session de navigation et par piste ; « Plus tard », un clic hors du panneau ou Échap le
ferment) et un bouton discret avec badge dans la barre du haut reste disponible en
permanence. Les deux mènent à une interface de tri une-par-une (`/swipe` pour Ingénieur IA, `/po/swipe` pour Chef de
projet / PO) : une carte plein écran par offre (titre, société, icône de lien externe dans
l'en-tête, métadonnées, résumé, annonce complète dépliable), les offres `fit high` d'abord.
Faire défiler l'annonce complète dépliée reste un défilement vertical natif ; seul un geste
horizontal déclenche le swipe. Flèche droite, bouton ✓ ou glisser
vers la droite : l'offre part dans `À candidater` ; flèche gauche, bouton ✕ ou glisser vers la
gauche : elle part dans la `Corbeille`. Flèche haut ou bouton ↩ annule le dernier geste et
remet la carte sur le paquet.

À la fin du paquet, un écran bilan récapitule la session et, si le bloc `draft` est configuré,
propose de générer d'un coup les lettres de motivation de **toutes** les offres « À candidater »
de la piste qui n'en ont pas encore (les échecs précédents sont réessayés, les lettres
existantes ne sont pas régénérées) avec un CV choisi pour tout le lot. Les jobs partent en file
(`status='queued'`, deux générations simultanées au plus) et la génération continue côté serveur
même si la page est fermée. Le bilan est une page de sortie : une fois le lot lancé, l'avancement
continue en arrière-plan, l'interface revient automatiquement au tableau de bord et le suivi se
fait dans un badge de la barre du haut (anneau de progression, puis panneau « x prête(s) ·
z échec(s) » au clic), présent aussi bien sur le tableau de bord que sur `/swipe`, et rechargé
depuis le serveur à chaque chargement de page. Ouvrir `/swipe` avec un paquet vide mène
directement à ce bilan, ce qui permet de lancer la génération groupée à tout moment.

## Génération de lettre de motivation

Quand le bloc `draft` de `config.yaml` est renseigné, chaque carte (hors `Corbeille`) affiche un
onglet « Lettre ». Il contient le bouton « Générer la lettre » et un mini-formulaire : un menu CV (bibliothèque de documents,
dernier choix mémorisé par onglet côté client) et un champ consigne optionnel. La soumission
lance un job en arrière-plan (table `draft_job`) :

1. Le texte de l'offre est lu depuis `offer_content`, ou récupéré à la demande avec la mécanique
   de `jw enrich` (HTTP puis Playwright). Si la page est irrécupérable, la lettre est générée à
   partir du titre, de la société et du résumé, avec un avertissement affiché sur la carte.
2. Le LLM (bloc `draft` : runner `opencode` ou `codex`, comme pour `enrich`) reçoit l'offre, le texte du CV choisi (extrait via
   `pdftotext` pour un PDF) et des lettres exemples `.tex`, puis rédige le document LaTeX complet,
   sans image, daté du jour, en encadrant le corps de la lettre (de la formule d'ouverture à la
   formule de clôture, hors date, en-tête et signature) par les marqueurs de commentaire LaTeX
   `% JOBWATCH:BODY_START` / `% JOBWATCH:BODY_END`. Les lettres exemples sont résolues dans cet
   ordre : `examples` de la piste métier dans `config.yaml` si présent, sinon les lettres
   `letter_example` de la bibliothèque de documents (import onboarding ou upload manuel), sinon
   le modèle générique fourni avec jobwatch.
3. Le `.tex` est compilé avec `lualatex` ; en cas d'erreur, le log est renvoyé au LLM pour
   réparation (deux tentatives), sinon l'erreur est affichée avec un lien vers le `.tex`.
4. Le PDF est rendu page par page en PNG (`pdftoppm`) pour l'aperçu intégré au tableau de bord
   (fiable sur iOS, contrairement à l'iframe PDF), avec liens vers le PDF et le `.tex`.
5. Le PDF rejoint la bibliothèque de documents comme lettre de motivation « LM Société - Poste » :
   le formulaire Candidater la propose immédiatement, sans recharger la page.

Pendant la génération (une à trois minutes), la carte affiche un indicateur animé ; l'état est
persistant en base et sondé par le client, il survit donc au rechargement de la page comme au
verrouillage du téléphone. Régénérer avec une consigne (« plus court », « insiste sur le
RAG »...) transmet la lettre précédente au modèle et remplace la même entrée de bibliothèque.
`lualatex` (TeX Live), `pdftotext` et `pdftoppm` (poppler-utils) doivent être installés.

Le bouton « Modifier le texte » de l'onglet « Lettre » ouvre un éditeur qui n'expose que le corps
de la lettre en texte brut (le texte entre les marqueurs ci-dessus) : jamais le `.tex` complet, ni
la date, l'en-tête destinataire/société ou la signature, qui restent dérivés par le LLM.
L'enregistrement échappe les caractères spéciaux LaTeX du texte édité, le réinjecte entre les
marqueurs du `.tex` existant et recompile directement (`lualatex` + rendu PNG), sans nouvel appel
LLM. La contrainte d'une page tient toujours : un hand-edit qui déborde sur une deuxième page, ou
qui échoue à la compilation, est signalé à l'utilisateur sans rien écraser - la lettre précédente
reste intacte et l'utilisateur ajuste son texte puis réessaie, plutôt que de retomber sur la
boucle de réparation LLM (réservée aux brouillons générés). Ce flux d'édition directe et la
régénération avec consigne sont deux leviers de révision indépendants et cohabitent sans se
remplacer.

## Référence de configuration

`config.yaml` (une copie de `config.example.yaml`) comporte sept sections.

| Clé | Description |
| --- | --- |
| `db` | Chemin vers la base SQLite. `~` est développé. Les répertoires sont créés automatiquement. |
| `searches` | Liste des recherches enregistrées. Chaque recherche a : `name` (identifiant unique), `include` (mots-clés, au moins un, correspondance insensible à la casse sur le titre), `exclude` (mots-clés, aucun), `locations` (correspondance par sous-chaîne sur la localisation de l'offre ; vide = n'importe où), `contract` (optionnel : `permanent`, `fixed_term`, `internship`, `other`). |
| `sources` | Les job boards à surveiller. `france_travail` nécessite `client_id`, `client_secret`, `keywords` et éventuellement `department`. `smartrecruiters` prend une liste de slugs de sociétés. `linkedin` prend une liste de couples `keywords`/`location` et une fenêtre `hours`. `wttj` prend ses requêtes, pays, villes internationales, fenêtre `hours` et les identifiants publics de l'index Algolia utilisé par le site. |
| `notify` | Canaux de notification. `ntfy` publie sur `https://ntfy.sh/<topic>`. `smtp` envoie via `host`, `port`, `user`, `password`, `to`. Les deux sont optionnels ; vous pouvez en utiliser un, les deux ou aucun. |
| `research` | Recherche web large facultative après les collecteurs directs : runner `codex` ou `opencode`, modèle, fenêtre `recency_days`, plafond `max_results` (appliqué après validation et déduplication) et instructions de profil. C'est le seul runner à qui `websearch` et `webfetch` restent autorisés. Les catégories confirmées dans SQLite sont utilisées en priorité. |
| `enrich` | Configuration de `jw enrich` : `runner` (`opencode`, défaut, `codex` ou `pi`), le binaire correspondant (`opencode_bin`/`codex_bin`/`pi_bin`), `model` (ex. `opencode/deepseek-v4-flash-free`, `gpt-5.6-luna` ou `openai-codex/gpt-5.6-luna` avec Pi), `variant` optionnel (effort de raisonnement) et `concurrency` (appels LLM simultanés, défaut 4 ; les fetchs web restent séquentiels). Pi est exécuté sans outils et sans session persistante. |
| `draft` | Génération de lettre de motivation depuis le tableau de bord : `runner` (`opencode` ou `codex`), le binaire correspondant (`opencode_bin`/`codex_bin`), `model` (modèle de rédaction fort, ex. `gpt-5.6-luna`), `variant` optionnel (effort de raisonnement), plus `examples`, un mapping piste (`engineer`, `project`) vers une liste de chemins de lettres `.tex` servant d'exemples de format et de ton. Si `examples` ne couvre pas la piste, jobwatch utilise les lettres `letter_example` de la bibliothèque de documents, puis un modèle générique fourni avec le projet. |

Le filtre `locations` est une correspondance par sous-chaîne sur la localisation de l'offre :
une offre située à « Puteaux » ou « Levallois-Perret » ne matche PAS une recherche avec
`locations: ["Paris"]`. Listez explicitement les communes voulues dans `locations`, ou laissez
la liste vide pour accepter n'importe quelle localisation.

Dans `config.example.yaml`, les blocs `sources`, `notify`, `research`, `enrich` et `draft` sont vides
(`{}`) : décommentez-les et remplissez-les pour activer la collecte, les notifications,
l'enrichissement, la recherche large et la génération de lettres. Avec la config d'exemple non modifiée,
`jw init && jw run` ne fait aucun appel réseau et ne publie rien ; `jw enrich` refuse de
s'exécuter tant que `enrich` n'est pas rempli, et l'onglet « Lettre » n'apparaît pas tant
que `draft` n'est pas rempli.

Les recherches de `config.yaml` sont synchronisées dans la base à chaque `jw run` : les nouvelles
sont insérées, les modifiées mises à jour, les supprimées désactivées (les matchs existants sont
conservés). Les catégories confirmées dans le tableau de bord sont gérées à part et ne peuvent
jamais reprendre une recherche de `config.yaml` ni celle d'un autre compte : retirer une catégorie
l'archive (`search.archived_at`) sous un nom suffixé « (archivée N) », ce qui retire ses matchs du
tableau de bord et du digest sans rien effacer et libère son nom ; renommer une catégorie garde en
revanche sa recherche, donc le tri déjà fait.

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
les matchs sont stockés avec un état (`new`, `seen`, `later`, `applied`, `discarded`). Une candidature est
créée depuis un match, et son statut actuel est le dernier événement de son journal d'événements.

| Table | Rôle |
| --- | --- |
| `source` | Sources de job boards configurées et leur dernière exécution |
| `company` | Sociétés (dédupliquées par nom) |
| `offer` | Offres d'emploi (dédupliquées par URL et société+titre) |
| `offer_content` | Texte utile de l'annonce, méthode d'extraction, HTML brut compressé et suivi borné des échecs de fetch, récupérés par `jw enrich` |
| `offer_summary` | Résumé factuel unique associé à une offre existante (`source` : `manual` ou `auto`) |
| `summary_field` | Champs structurés d'un résumé (expérience, salaire, télétravail, stack) et citation vérifiée optionnelle |
| `summary_bullet` | Bullets d'un résumé avec leur position explicite |
| `search` | Recherches enregistrées (mots-clés, localisations, contrat) ; `archived_at` marque une catégorie retirée, masquée du tableau de bord et du digest |
| `match` | Paire offre/recherche avec état et statut de notification |
| `application` | Votre candidature pour une offre |
| `event` | Historique d'une candidature (applied, interview, rejected, offer, ...) |
| `document` | Fichiers CV et lettre de motivation attachés à une candidature |
| `document_library` | Bibliothèque de documents réutilisables du tableau de bord : `cv`, `cover_letter` et `letter_example` (exemple de style pour la génération de lettres) |
| `draft_job` | Jobs de génération de lettre de motivation (état, fichiers produits, avertissements) |
| `workspace`, `account`, `membership` | Instance, compte propriétaire unique et droits, préparant le passage au multi-utilisateur |
| `account_invite`, `web_session` | Invitations à durée limitée et sessions web opaques |
| `instance_setting`, `login_throttle` | Réglages de l'instance (dont `auth_required`) et compteur d'échecs de connexion par email/adresse |
| `candidate_profile`, `candidate_profile_document`, `career_intent` | Profil d'onboarding, CV analysés et catégories métier confirmées d'un compte |

## Feuille de route

- v0.2 : tableau de bord local en lecture seule (`jw serve`).
- v0.3 : import des artefacts quotidiens (`jw ingest-daily`) et du suivi Markdown (`jw import-md`), fit LLM, échéances et documents.
- v0.4 : résumés high stockés dans SQLite et affichés dans le tableau de bord.
- v0.5 : `jw enrich` récupère le texte complet des annonces collectées et en génère un résumé via LLM.

## Licence

MIT. Copyright 2026 Rayan Leveque.
