# Bêta privée de Jobwatch

Périmètre retenu pour quatre ou cinq personnes

- Veille LinkedIn et suivi des candidatures.
- Catégories individuelles, préréglage PO / MOA modifiable et localisation individuelle.
- Une instance et un compte système par personne, derrière un nom DNS HTTPS distinct.
- Aucun runner IA, aucune compilation LaTeX dans ces instances.

Ces fichiers préparent une installation séparée de la production historique de Rayan.
Ils ne modifient pas son service ni son timer de déploiement. Les scripts de ce dossier
nécessitent root. Les identifiants d'instance sont limités à 23 caractères dans ces
scripts pour rester compatibles avec les noms des comptes système.

## Préparer une version

Installer Python 3.11 ou supérieur, nginx, curl, util-linux, restic et un outil de
gestion des certificats TLS sur l'hôte. Placer un checkout du commit validé sous
`/opt/jobwatch/releases/<commit complet>`, créer sa `.venv`, puis installer le paquet.
Le code et la `.venv` appartiennent à root et ne sont pas modifiables par les comptes
Jobwatch. Le lien `/opt/jobwatch/current` pointe vers cette version.

Installer les fichiers `jobwatch*.service` et `jobwatch*.timer` de ce dossier sous
`/etc/systemd/system/`, puis lancer `systemctl daemon-reload`.

Les unités limitent la mémoire, les processus, le CPU et l'accès en écriture.
Le compte de chaque personne peut écrire seulement dans son dossier d'instance.
Ne pas copier les configurations IA, notifications ou secrets de Rayan.

## Créer une instance

Choisir un port local libre et une adresse email confirmée, puis exécuter par exemple

```bash
/opt/jobwatch/current/ops/provision.sh alice alice@example.com alice.jobs.example 8801
```

Le script refuse une instance existante, crée son compte système, sa configuration
et son invitation, puis active serveur, collecte et contrôle. Il génère un fichier
nginx à relire sous `/etc/jobwatch/instances/alice/nginx.conf` sans le publier.

Configurer le DNS, obtenir le certificat, charger `nginx-common.conf` une seule fois
dans le bloc `http`, puis charger le fichier de cette instance. Valider avec `nginx -t`
avant rechargement. Vérifier HTTPS avec un navigateur externe au Tailnet. Ne pas ouvrir
le port 8801, qui écoute seulement sur localhost. Le proxy interdit l'accès public
au contrôle de santé, limite les requêtes de connexion et masque les liens d'invitation
dans les journaux d'accès. Ne pas activer les logs nginx de niveau debug.

Le cookie reste Secure. Les noms DNS distincts séparent les cookies, contrairement
à deux ports d'un même hôte. La limite applicative utilise l'adresse du proxy, sans
faire confiance à un en-tête IP envoyé par le client. La limite nginx s'applique à
l'adresse réelle de connexion.

Transmettre manuellement le lien d'invitation HTTPS au destinataire, jamais dans un
journal public. Il expire après 48 heures. À l'inscription, la personne peut choisir
PO / MOA, supprimer une catégorie, changer ses mots-clés et saisir jusqu'à cinq villes
ou régions. Une localisation vide signifie France. Le télétravail complet ajoute une
requête France filtrée par LinkedIn. Les offres sans lieu connu restent visibles.

Le timer collecte toutes les six heures avec un décalage aléatoire de vingt minutes.
Aucune requête ne part avant la confirmation du profil. Pour la première invitation,
après confirmation du profil, l'opérateur peut lancer immédiatement

```bash
systemctl start jobwatch-collect@alice.service
```

Les erreurs HTTP LinkedIn rendent le service de collecte en échec, même si des résultats
partiels ont été conservés. LinkedIn peut limiter les accès ou changer son HTML.
Une réponse HTTP réussie avec zéro offre ne prouve pas que le collecteur reste compatible.

## Sauvegarder et restaurer

Préparer un dépôt restic sur une autre machine ou un stockage distant privé.
Initialiser ce dépôt et conserver sa clé de récupération hors de cet hôte.
Créer `/etc/jobwatch/backup.env`, lisible seulement par root, contenant
`RESTIC_REPOSITORY` et `RESTIC_PASSWORD_FILE`. Le fichier de mot de passe doit aussi
être réservé à root. Ne pas placer ses valeurs dans Git.

```bash
systemctl enable --now jobwatch-backup@alice.timer
systemctl start jobwatch-backup@alice.service
```

Le script arrête serveur et collecte pendant la copie, utilise l'API de sauvegarde
SQLite, vérifie son intégrité et copie les documents. Il redémarre ensuite le service
avant l'envoi chiffré par restic. Un échec de l'envoi reste un échec systemd. Les copies
locales sont conservées sous `/var/backups/jobwatch/`. Aucun nettoyage automatique
n'est activé. Suivre l'espace disque et définir une rétention après le premier essai
de restauration. Les sauvegardes sans `manifest.json` sont incomplètes.

Pour une sauvegarde manuelle, arrêter également le serveur et toute collecte avant
`jw --instance alice backup /chemin/neuf`. L'API SQLite seule ne garantit pas une copie
cohérente des fichiers si une application les modifie pendant l'opération.

Restaurer un snapshot restic dans un dossier temporaire, puis utiliser

```bash
jw restore /chemin/sauvegarde /chemin/restauration-neuve
jw --instance alice serve --config /chemin/restauration-neuve/config.yaml --port 8899
```

La restauration refuse un dossier existant et vérifie les hashes. Elle réécrit les
chemins des documents et révoque les sessions et invitations sauvegardées. Le mot de
passe du propriétaire reste utilisable. Vérifier les candidatures et ouvrir les CV
depuis cette instance avant de déclarer la sauvegarde opérationnelle. Ne pas lancer
sa collecte de test, ses notifications peuvent encore désigner le vrai destinataire.

## Déployer et contrôler

Préparer et tester la nouvelle version avant de modifier `current`, sauvegarder les
instances, puis lancer

```bash
/opt/jobwatch/current/ops/deploy.sh /opt/jobwatch/releases/COMMIT_COMPLET alice bob
```

Le script arrête les instances indiquées et leur collecte, change le lien de version,
puis vérifie `/healthz` sur chaque port. Un échec remet le lien précédent et redémarre
les instances. Ce retour arrière concerne le code, pas une migration de données
incompatible. Tester chaque migration sur une restauration avant le déploiement.
Les lettres IA restent hors de cette installation, aucun job LLM n'y est interrompu.

Le timer de contrôle vérifie HTTP, SQLite, l'authentification et une collecte réussie
depuis moins de trente heures pour un profil inscrit depuis plus de trente heures.
Surveiller `systemctl --failed` et les journaux de collecte, sauvegarde et contrôle.
Brancher une alerte opérateur sur ces échecs avant l'ouverture, le dépôt ne contient
aucun destinataire de notification préconfiguré.

## Récupérer un compte ou fermer son accès

Exécuter les commandes opérateur avec `XDG_CONFIG_HOME=/etc XDG_DATA_HOME=/var/lib`.
Pour récupérer le compte, vérifier l'identité du demandeur puis créer une nouvelle
invitation pour son adresse existante. L'acceptation change le mot de passe et révoque
les anciennes sessions.

```bash
jw --instance alice account invite alice@example.com
jw --instance alice account revoke
```

La seconde commande désactive immédiatement le compte et révoque sessions et invitations.
Une nouvelle invitation permet de réactiver le compte. Avant une suppression demandée,
arrêter et désactiver serveur et timers de cette personne, puis supprimer uniquement
ses dossiers de configuration et de données après vérification de leurs chemins.
Traiter aussi ses copies locales et ses snapshots restic selon la rétention annoncée.
La révocation seule ne supprime aucun document.

## Critères avant première invitation réelle

- HTTPS valide depuis un navigateur hors Tailnet, connexion et cookie Secure.
- Deux comptes de test ne peuvent accéder aux documents l'un de l'autre.
- Inscription mobile, choix PO / MOA, localisation, collecte réelle et candidature réussis.
- Aucun outil IA affiché ou appel IA déclenché dans la bêta.
- Sauvegarde distante récupérée et documents ouverts après restauration.
- Version défectueuse détectée et version précédente remise en service.
- Échec de collecte ou de sauvegarde visible par l'opérateur, espace disque suivi.

Les tests du dépôt couvrent le navigateur mobile et desktop, la collecte simulée
avec les paramètres du profil, la déduplication, les cookies entre instances,
la restauration documentaire et le retour de version avec services simulés.
Ils ne remplacent pas les vérifications DNS, TLS, restic distant et systemd sur l'hôte.
