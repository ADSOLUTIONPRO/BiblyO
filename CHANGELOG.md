# Notes de version

## 1.2.0

**Codes-barres, étiquettes A4 et scanner universel.** Pour un bénévole : générer en une fois les codes-barres des
livres qui n'en ont pas, imprimer leurs étiquettes sur des planches A4 prédécoupées (même entamées), les coller, puis
scanner n'importe quel livre avec la douchette pour l'ouvrir, le prêter, le rendre, le ranger ou l'enrichir.

Révision du schéma : **0014** (deux tables nouvelles pour les lots d'impression et un rôle d'appoint « Impression des
étiquettes », attribué à personne ; aucune table existante modifiée).
La mise à jour se fait à la main (`biblyo db upgrade`), après une sauvegarde vérifiée prise automatiquement.

### Codes-barres

- Page « Codes-barres et étiquettes », depuis le Catalogue et la rubrique « Catalogue et affichage » des paramètres :
  exemplaires avec et sans code, étiquettes restant à imprimer.
- « Générer tous les codes manquants » : une simulation d'abord (rien n'est enregistré, la numérotation n'avance pas),
  puis une seule opération confirmée, même pour des milliers de livres, dans l'ordre des numéros d'inventaire.
  « Retiré » et « Perdu » sont laissés de côté.
- « Générer pour ma sélection » (recherche, série, tomes, cote, emplacement) et « Générer pour ce livre », depuis la
  fiche de l'exemplaire.
- Toujours la même numérotation : un code existant n'est jamais remplacé, un code retiré ou porté par une carte n'est
  jamais réemployé, l'inventaire, la cote et l'ISBN ne changent jamais ; chaque attribution est journalisée.
- Préfixe des prochains codes réglable explicitement, sans retour à zéro : à l'écran ou par `biblyo codes etat` puis
  `biblyo codes prefixe CSN [--appliquer]` (`CSN000001`…) ; `CSN-` est refusé, et une ancienne étiquette `CSN-…` de la
  reconstruction n'est jamais prise pour un code `CSN…`.
- La génération en masse (tous les codes manquants, une sélection, un lot depuis « Non rangés ») est réservée à la
  gestion du catalogue ; un livre seul reçoit son code depuis sa fiche.

### Étiquettes A4

- PDF produit par biblyO, sans service extérieur, au format A4 réel, qui demande une impression à 100 %.
- Planches Avery **L7160** (21, format principal des BD) et **L7651** (65, format compact), puis L7159 et L7654 et un
  gabarit personnalisé marqués « calibration nécessaire » ; une planche est certifiée après la feuille test imprimée à
  100 % et la lecture de ses codes à la douchette ; modèles compact, standard, détaillé ou personnalisé (logo, nom,
  titre, série et tome, cote, inventaire, emplacement ; seul le code est obligatoire). Code 128 lisible : module de
  0,25 mm au moins, zones blanches de 10 modules, 6 mm de barres au moins.
- Assistant en trois étapes : choisir les livres, choisir la planche (et les cases déjà utilisées d'une feuille
  entamée), vérifier l'aperçu A4 et une étiquette agrandie, puis préparer le PDF.
- Feuille test (règles de 100 mm, codes d'essai) et calibration pour un décalage de l'imprimante.
- Lots d'impression gardés : refaire un PDF à l'identique, reprendre une impression interrompue, réimprimer sans
  régénérer aucun code ; une étiquette n'est tenue pour collée que si la personne le confirme.
- Imprimer demande la permission « Imprimer des étiquettes » : rôle bibliothécaire, et nouveau rôle d'appoint
  « Impression des étiquettes », à cocher en plus du rôle d'un bénévole choisi (le rôle « Bénévole » n'est pas élargi).

### Scanner universel

- Bouton « Scanner » permanent : une fenêtre de scan sans quitter la page. Sur un poste déclaré « poste de prêt de la
  bibliothèque » (désactivé par défaut), une lecture de douchette hors de tout champ l'ouvre aussi, jamais pendant la
  saisie d'un formulaire ou d'un mot de passe. Une douchette n'identifie personne : tout reste fait au nom du compte
  connecté.
- Fiche immédiate : couverture, titre, auteurs, série et tome, édition, codes, disponibilité réelle, emplacement, et
  seulement les actions permises et utiles : prêter, enregistrer le retour, ouvrir ou modifier la fiche, enrichir,
  vérifier la présence ou changer le statut, ranger, imprimer l'étiquette.
- Un ISBN scanné mène aux éditions du catalogue et à leurs exemplaires, à choisir ; absent du catalogue, une recherche
  à la BnF et au Sudoc est proposée (jamais lancée seule) et la notice peut être créée avec l'ISBN prérempli.
- Modes de travail : recherche, comptoir, retours, inventaire (vérifie la présence sans rien écrire), rangement,
  enrichissement.
- « Configurer ma douchette » : codes d'essai (Code 128, ISBN-13, code inconnu), touche finale et vitesse mesurées,
  diagnostic « Douchette prête », « Code non reconnu », « Suffixe non détecté » ou « Code inconnu » (y compris une
  douchette en clavier américain sur un poste français), et une liste de mise en service en huit points.

### Comptoir et retours

- Un ISBN ne désigne plus jamais seul un exemplaire : même s'il n'y en a qu'un, il se confirme d'un clic.
- Une douchette réglée en Tab fonctionne comme en Entrée ; le même code lu deux fois de suite n'est compté qu'une fois.
- Un code déformé par une douchette en clavier américain est signalé, avec le code probable, sans être appliqué.
- Les raccourcis d'une lettre (P, R, N, G…) attendent un instant et se taisent pendant une lecture de douchette.

### Image

- Correctif de sécurité Debian appliqué à l'image de base épinglée : `zlib1g` en version
  `1:1.3.dfsg+really1.3.1-1+deb13u1` (trixie-security, CVE-2026-85091), exactement, et aucun autre paquet changé. Une
  règle des contrôles refuse désormais tout paquet Debian installé sans version exacte ou sans retrait des index d'apt.

### Points d'attention

- Après la mise à jour, lancer `biblyo db upgrade` : le service reste « non sain » tant que la base n'est pas migrée.
- Retour à la 1.1.0 possible en restaurant la sauvegarde « pré-migration » prise par `biblyo db upgrade`.
- Avant la première impression sur une planche, imprimer la feuille test sur papier ordinaire à 100 % et la poser sur
  la planche : les marges et espacements viennent de bases publiques, pas encore d'une mesure sur de vraies planches.
- Avant la première génération : `biblyo codes etat`, puis le préfixe retenu (`biblyo codes prefixe CSN`).
- Mise en service de la douchette et des étiquettes : guide `DOUCHETTE-ET-ETIQUETTES.md`, joint à la version.

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
