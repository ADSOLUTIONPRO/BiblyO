# Installer biblyO sur un Synology (DSM 7.2, Container Manager)

Ce guide s'adresse à une personne qui connaît DSM sans forcément connaître Docker. Il suffit de DSM : **aucun clone du
code, aucune compilation sur le NAS, aucun fichier d'image à transférer**. L'image officielle est téléchargée depuis
GitHub Container Registry par Container Manager.

> **Modèles compatibles** : NAS Synology à processeur Intel ou AMD (x86-64), avec Container Manager (DSM 7.2 ou
> suivant). L'image publiée est `linux/amd64`. Exemple : DS425+ (Intel Celeron J4125).
>
> Les libellés des menus sont ceux de DSM 7.2 en français ; ils peuvent varier légèrement selon la version.

## Avant de commencer : ce qu'il faut vérifier

Notez chaque valeur relevée : elle servira en cas de question.

| Vérification | Où, dans DSM | Ce qu'il faut |
|---|---|---|
| Modèle et processeur | Panneau de configuration → Centre d'infos → Général | Intel ou AMD (x86-64) |
| Version de DSM | Même écran | DSM 7.2 ou suivant |
| Mémoire libre | Moniteur de ressources → Mémoire | biblyO utilise environ 100 à 250 Mo |
| Espace libre | Gestionnaire de stockage | Quelques Go (image d'environ 400 Mo, base, couvertures, sauvegardes) |
| Container Manager installé | Centre de paquets | Installé et à jour |
| Ports déjà utilisés | Container Manager → Conteneur (colonne des ports), Panneau de configuration → Réseau, services DSM (5000, 5001…), proxy inversé | Choisir un port **libre constaté**, par exemple dans la plage 18000 à 18999. **Ne jamais prendre un port déjà publié par un autre conteneur** |
| Autres applications | Container Manager → Conteneur et Projet | Les noter : biblyO ne les touche pas et n'utilise aucun de leurs dossiers |

## Étape 1. Créer les dossiers de biblyO (File Station)

1. Ouvrez **File Station**.
2. Ouvrez le dossier partagé **docker** (créé par Container Manager lors de son installation). Il doit être sur un
   volume **local** du NAS : jamais un dossier monté depuis un autre serveur (SMB ou NFS), car la base SQLite exige un
   stockage local.
3. **Créer** → **Créer un dossier** : `biblyo`.
4. Dans `biblyo`, créez deux dossiers : `data` et `sauvegardes`.

Résultat : `/volume1/docker/biblyo/data` et `/volume1/docker/biblyo/sauvegardes` (le numéro de volume peut différer :
il est affiché dans les propriétés du dossier, clic droit → **Propriétés**, champ « Emplacement »).

- `data` recevra la base `biblyo.sqlite` et les couvertures (`data/media/`) ;
- `sauvegardes` recevra les archives de sauvegarde de biblyO.

## Étape 2. Donner ces dossiers à biblyO (une seule fois)

biblyO tourne sous un utilisateur sans privilège (numéro **10001**), qui doit pouvoir écrire dans ces deux dossiers et
dans eux seuls. DSM le permet par une tâche exécutée une fois :

1. **Panneau de configuration** → **Planificateur de tâches** → **Créer** → **Tâche planifiée** → **Script défini par
   l'utilisateur**.
2. Onglet **Général** : nom `biblyo-droits`, utilisateur **root**, décochez **Activé** (elle ne doit pas se répéter).
3. Onglet **Paramètres de tâche**, zone « Script défini par l'utilisateur » (adaptez `volume1` si besoin) :

   ```sh
   chown -R 10001:10001 /volume1/docker/biblyo/data /volume1/docker/biblyo/sauvegardes
   chmod 750 /volume1/docker/biblyo/data /volume1/docker/biblyo/sauvegardes
   ```

4. **OK**, sélectionnez la tâche, **Exécuter**, confirmez. Puis vous pouvez la supprimer.

Ce script ne touche que les deux dossiers de biblyO.

## Étape 3. Préparer le fichier de variables

1. Sur votre ordinateur, téléchargez `.env.example` depuis la page de la version (GitHub → *Releases*).
2. Ouvrez-le dans un éditeur de texte et renseignez :
   - `BIBLYO_PORT` : le port libre choisi plus haut ;
   - `BIBLYO_DOSSIER` : `/volume1/docker/biblyo` (adaptez le volume) ;
   - `BIBLYO_BASE_URL` : `http://<adresse-locale-du-nas>:<port>`, par exemple `http://192.168.1.10:18080`.
3. Enregistrez-le sous le nom **`.env`** (avec le point, sans autre extension).
4. Dans File Station, ouvrez `docker/biblyo`, **Charger** → **Charger - Ignorer**, choisissez `.env`.

**Aucun mot de passe ni secret** ne va dans ce fichier : biblyO n'en a pas besoin (voir la fin du guide).

## Étape 4. Créer le projet dans Container Manager

1. Ouvrez **Container Manager** → **Projet** → **Créer**.
2. **Nom du projet** : `biblyo`.
3. **Chemin** : **Définir le chemin**, choisissez `docker/biblyo` (le dossier qui contient `.env`).
4. **Source** : **Créer docker-compose.yml**. Collez le contenu de `deploy/synology/compose.yaml` de la version (page
   *Releases* ou dépôt GitHub), sans le modifier.
5. **Suivant**. À l'étape « Paramètres du portail web », **n'activez rien** (le proxy inversé se règle plus tard, à part).
6. **Suivant**, cochez **Démarrer le projet une fois créé**, puis **Terminé**.

Container Manager télécharge l'image `ghcr.io/adsolutionpro/biblyo` (environ 100 Mo compressés) et démarre le
conteneur `biblyo`. Si une variable manque, il refuse en affichant son nom (par exemple « BIBLYO_PORT manquant ») :
corrigez `.env`, puis **Projet** → `biblyo` → **Action** → **Construire**.

À ce stade, le conteneur est **en marche mais « non sain »** : c'est normal, la base n'existe pas encore. biblyO ne
crée jamais sa base de lui-même au démarrage.

## Étape 5. Créer la base (migrations)

1. **Container Manager** → **Conteneur** → `biblyo` → **Détails**.
2. Onglet **Terminal** → **Créer** ▾ → **Lancer avec la commande** : `sh` → **OK**.
3. Dans le terminal qui s'ouvre, tapez :

   ```sh
   biblyo db upgrade
   ```

   Réponse attendue : « Base à jour, révision 0012. Intégrité et schéma contrôlés ». Puis :

   ```sh
   biblyo db status
   ```

   Réponse attendue : « Révision courante : 0012 », « Révision attendue : 0012 », « Intégrité : correcte ».

4. Après une minute, l'état du conteneur passe à **sain** (Container Manager → Conteneur).

## Étape 6. Premier administrateur

1. Dans le même terminal :

   ```sh
   biblyo install token
   ```

   Un **jeton d'installation** s'affiche. Il ne sert qu'une fois, ne vaut que tant qu'aucun compte n'existe, et ne
   doit être ni envoyé par courriel ni écrit dans un fichier.
2. Dans un navigateur du réseau local, ouvrez `http://<adresse-du-nas>:<port>/installation`.
3. Saisissez le jeton, le nom de la bibliothèque, votre nom, votre identifiant et un mot de passe **choisi sur place**
   (12 caractères au moins). Aucun compte n'est prédéfini.
4. L'installation se referme d'elle-même dès ce premier compte.

## Étape 7. Vérifier

- **Container Manager** → **Conteneur** : `biblyo` est **sain** ; l'onglet **Journal** (Détails → Journal) ne montre
  pas d'erreur.
- Dans biblyO : **Paramètres** → « Contrôle des réglages » ; le catalogue est **vide** (aucune donnée de démonstration).
- **Import & export** : l'application des imports est **désactivée** ; elle ne s'active que sur décision, par la
  commande `biblyo import application --activer` dans le terminal.
- **Redémarrage** : Container Manager → Conteneur → `biblyo` → **Action** → **Redémarrer** ; reconnectez-vous : tout
  est conservé (réglages, logo, couvertures).

## Étape 8. Accès en HTTPS (proxy inversé de DSM)

À faire **après** vérification du nom et du certificat, et jamais en ouvrant le port de biblyO sur Internet.

1. **Nom** : choisissez un nom (par exemple `biblyo.<votre-domaine>`) et vérifiez qu'il pointe vers le NAS. Qu'un nom
   « résolve » ne suffit pas : il doit aussi arriver au proxy inversé de DSM et être couvert par un certificat.
2. **Certificat** : Panneau de configuration → **Sécurité** → **Certificat** : un certificat valide doit couvrir ce nom
   (par exemple un certificat Let's Encrypt du domaine, ou un certificat joker `*.<domaine>`). Notez sa date
   d'expiration et vérifiez son renouvellement automatique.
3. **Règle de proxy** : Panneau de configuration → **Portail de connexion** → **Avancé** → **Proxy inversé** →
   **Créer** :
   - Description : `biblyo` ;
   - Source : protocole **HTTPS**, nom d'hôte : le nom choisi, port **443** ;
   - Destination : protocole **HTTP**, nom d'hôte **localhost**, port : `BIBLYO_PORT`.

   **Ne modifiez aucune des règles existantes** (celles des autres services restent telles quelles).
4. **Associer le certificat** : Sécurité → Certificat → **Paramètres** : choisissez le certificat pour le service
   `biblyo` qui vient d'apparaître.
5. **Mettre biblyO au courant** : dans `.env`, `BIBLYO_BASE_URL=https://<nom-choisi>` ; et `BIBLYO_TRUSTED_PROXIES` =
   l'adresse par laquelle le proxy joint le conteneur, c'est-à-dire la **passerelle** du réseau du projet (Container
   Manager → **Réseau** → `biblyo_default` → passerelle, par exemple `172.20.0.1`). Puis **Projet** → `biblyo` →
   **Action** → **Construire** (le conteneur est recréé avec les mêmes dossiers).
6. Ouvrez `https://<nom-choisi>` : la connexion est chiffrée, les cookies sont désormais « sécurisés ».

Pare-feu : le port `BIBLYO_PORT` ne doit pas être redirigé depuis la box Internet. Seul le 443 du proxy de DSM l'est,
s'il l'est déjà pour vos autres services.

## Sauvegardes et mises à jour

- Sauvegarder et restaurer : [`docs/SAUVEGARDE-RESTAURATION.md`](docs/SAUVEGARDE-RESTAURATION.md).
- Mettre à jour : [`docs/MISE-A-JOUR.md`](docs/MISE-A-JOUR.md).

## Les « secrets » de biblyO

biblyO n'a **aucun secret de configuration** à générer ni à garder :

| Élément | Comment il est produit | Où il vit |
|---|---|---|
| Jeton d'installation | `biblyo install token`, à usage unique | Base (empreinte seulement) ; caduc dès le premier compte |
| Mots de passe | Saisis par chaque personne | Base, hachés (Argon2id) ; jamais en clair |
| Sessions | Tirées au hasard à la connexion | Base (empreinte), cookie du navigateur |
| Variables `.env` | Port, dossier, adresse | Dossier du projet ; aucune n'est confidentielle |

## En cas de problème

- **Non sain après l'étape 5** : Conteneur → Détails → Journal ; dans le terminal, `biblyo db status`.
- **Erreur d'écriture dans `/data`** : l'étape 2 n'a pas été faite, ou sur d'autres dossiers.
- **Revenir en arrière** : Container Manager → Projet → `biblyo` → **Arrêter**, puis **Supprimer** (le projet
  seulement). Les dossiers `data` et `sauvegardes` restent intacts. Ne supprimez jamais les dossiers ni les images ou
  conteneurs d'autres projets.
