# Notes de version

## 1.1.0

**Enrichissement assisté, couvertures visibles, paramètres repensés.** Pour un bénévole : ouvrir un livre, cliquer sur
« Enrichir », voir les couvertures proposées, reconnaître son livre et compléter sa fiche en quelques clics, sans
risque de confondre une édition ou un tome.

Révision du schéma : **0013** (deux tables nouvelles pour l'enrichissement de la bibliothèque ; aucune table existante
modifiée). La mise à jour se fait à la main (`biblyo db upgrade`), après une sauvegarde vérifiée prise automatiquement.

### Enrichir un livre

- Un seul bouton, « Rechercher la couverture et les informations » : une seule recherche, à la BnF puis au Sudoc, tous
  niveaux compris (ISBN confirmé, titre et auteur, série et tome, titre et éditeur).
- Des cartes visuelles : la vraie couverture quand la source en a une, sinon « Pas d'image », jamais une illustration
  générique ni une vignette cassée ; une indication claire : « Même édition » (ISBN identique seulement), « Édition
  probable », « À vérifier », « Autre édition ». Le score reste dans « Détails techniques ».
- « Choisir cette proposition » : votre fiche et la proposition, information par information ; « Ajouter les
  informations manquantes » remplit seulement ce qui est vide, et demande « J'ai vérifié : c'est bien mon livre » hors
  même édition établie. L'ISBN, les auteurs, le résumé et la couverture ne sont jamais ajoutés d'office.
- « Ajouter seulement la couverture », « Ce n'est pas mon livre », « Voir les autres résultats ».
- Une couverture d'une autre édition est signalée « Autre édition — couverture à vérifier », jamais choisie seule ;
  jamais une intégrale ni un autre tome.
- Titres abrégés de bande dessinée reconnus (« Cerveau-choc » pour « Les Idées d'Achille Talon, cerveau-choc »),
  éditions concurrentes présentées côte à côte (« Du scénario à la réalisation », 1981 et 1990).
- Open Library, si une adresse de contact est renseignée : couvertures proposées une à une, jamais en lot.

### Enrichir la bibliothèque

- Bouton « Enrichir la bibliothèque » dans le catalogue : une recherche par édition (jamais par exemplaire), en
  arrière-plan, au rythme permis par chaque source ; pause, reprise, et reprise après un redémarrage.
- Tableau de travail : livres sans couverture, fiches incomplètes, correspondances fiables, couvertures annoncées, cas à
  vérifier, livres sans proposition.
- Validation par petits lots (25 au plus) des seules correspondances fiables ; rien n'est modifié sans validation, et
  chaque ajout s'annule depuis la fiche.

### Paramètres

- Sept rubriques : Ma bibliothèque, Catalogue et affichage, Enrichissement et couvertures, Prêts et lecteurs,
  Sauvegardes, Utilisateurs et permissions, Paramètres avancés ; chacune visible selon les droits du compte.
- Sources bibliographiques en cartes, avec « Tester » et un résultat en français courant ; cadences, délais et mémoire
  des réponses rangés dans « Réglages avancés ».

### Points d'attention

- Après la mise à jour, lancer `biblyo db upgrade` : le service reste « non sain » tant que la base n'est pas migrée.
- Retour à la 1.0.0 possible en restaurant la sauvegarde « pré-migration » prise par `biblyo db upgrade`.

## 1.0.0

**Première version stable de biblyO**, logiciel libre de gestion de bibliothèque auto-hébergé, en français, conçu pour
les petites bibliothèques associatives et installable sur un NAS Synology sans ligne de commande.

Code d'origine : `main@e3419c9` du dépôt de développement (privé), publié ici comme instantané audité, avec un
historique neuf. Révisions du schéma de la base : **0001 à 0012** ; une installation neuve les applique toutes par
`biblyo db upgrade`.

### Catalogue

- Notices à trois niveaux : œuvre, édition, exemplaire ; séries et tomes (y compris les tomes atypiques, gardés tels
  quels), cotes, collections, contributeurs et leurs rôles.
- Recherche plein texte, filtres, recherches enregistrées, contrôle des doublons.
- Couvertures téléversées, ou proposées par la BnF et Open Library, avec leur provenance, leurs conditions de
  réutilisation et un historique réversible.

### Rangement

- Sites, salles, meubles et étagères, plan de salle, capacité des étagères.
- Rangement à l'écran, par scan ou par lot, avec aperçu ; un exemplaire n'est « En place » (et prêtable) qu'après un
  rangement vérifié ; liste « À ranger » avec recherche et sélection en lot.
- Codes-barres attribués un par un ou en lot depuis la séquence, seulement où il en manque ; jamais réattribués.

### Lecteurs et prêts

- Lecteurs, familles et structures, responsables légaux des mineurs, cartes.
- Comptoir de prêt et de retour, par code-barres ou numéro d'inventaire ; renouvellements, retards, rappels ; règles
  par catégorie de lecteur.
- Confidentialité : coordonnées visibles selon les droits, export des données d'un lecteur.

### Enrichissement bibliographique

- BnF et Sudoc, avec propositions à accepter ou rejeter champ par champ, et ISBN proposés jamais tenus pour sûrs.
- Open Library désactivée tant qu'une adresse de contact n'est pas renseignée.

### Import et export

- Import d'un catalogue Excel (.xlsx) ou CSV : dépôt, correspondance des colonnes, **simulation intégrale sans
  écriture**, puis application en une seule transaction, après une sauvegarde vérifiée, réconciliée ligne par ligne.
  **Désactivée par défaut** sur toute instance.
- Export complet du catalogue (xlsx, CSV) et classeur au format d'origine d'un import, aller-retour vérifié cellule par
  cellule.

### Administration

- Paramètres : identité de la bibliothèque, logo, coordonnées, sauvegardes créées et vérifiées à l'écran, droits par
  rôle, contrôle de chaque réglage.
- Comptes et rôles (administration, bibliothécaire, bénévole, consultation), journal des opérations.
- Interface accessible (contrôles automatiques axe), thème clair et sombre, utilisable au téléphone.

### Installation et exploitation

- Image `ghcr.io/adsolutionpro/biblyo:1.0.0` (`linux/amd64`), construite de façon reproductible, analysée et éprouvée
  par une recette d'installation complète avant publication.
- `compose.yaml` prêt pour Synology Container Manager ; guides d'installation, de mise à jour, de sauvegarde et de
  restauration.
- Aucune migration ni donnée au démarrage, aucun compte prédéfini, aucun secret ; conteneur sans privilège, en lecture
  seule.

### Licence, droits et provenance

- Copyright © 2026 Adrien Zerbib (AD SOLUTION PRO). Logiciel libre sous licence **EUPL-1.2** (texte officiel dans
  `LICENSE`) ; répartition des droits dans `NOTICE`. Développé avec une assistance par IA (Claude Code), conçu, arbitré et validé par son titulaire.
- Audit d'indépendance (`docs/rapports/AUDIT-INDEPENDANCE-BIBLYO-V1.md`) : aucun code, gabarit, style ou texte repris
  d'une application antérieure ; contrôle automatique de reprise de code à chaque modification.
- Icônes Lucide 1.70.0 (ISC), polices Bricolage Grotesque, Manrope et JetBrains Mono (SIL OFL 1.1), htmx (0BSD),
  Alpine.js (MIT) : sous leurs propres licences (`THIRD_PARTY_NOTICES.md`). La liste des paquets Debian et Python de
  l'image est jointe à cette version, ainsi que la source exacte des paquets Debian (`sources-debian-1.0.0.tar`, et
  l'image `ghcr.io/adsolutionpro/biblyo:1.0.0-sources`).

### Points d'attention

- La base se crée et se migre à la main (`biblyo db upgrade`), depuis le terminal du conteneur.
- La restauration d'une sauvegarde se fait depuis ce terminal, suivie d'un redémarrage du conteneur.
- Image publiée pour les processeurs x86-64 (Intel, AMD).
