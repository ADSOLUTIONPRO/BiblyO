# Audit de publication de biblyO 1.0.0 : sécurité et provenance

**Date** : 13 novembre 2026. **Objet** : vérifier, avant toute publication, que le dépôt de distribution ne contient que
ce qui peut l'être. Le dépôt est un **instantané audité** de la version validée (`main@e3419c9` du dépôt de développement, privé),
avec un **historique neuf** : aucun historique de développement n'est publié.

## 1. Ce qui a été examiné, ce qui a été écarté

| Catégorie | Constat | Décision |
|---|---|---|
| Code de biblyO (`src/`) | Écrit pour le projet, en-tête `SPDX-License-Identifier: EUPL-1.2` dans chaque fichier, contrôlé | Publié |
| Code d'une application antérieure | Jamais repris : écarté dès l'origine, refus automatique par `scripts/check_independence.py` (identifiants, fichiers historiques) | Aucun élément |
| Archives et adaptations de cette application | Hors du dépôt de distribution | Écartées |
| Historique de développement, rapports internes, notes de conception, captures | Contiennent des données d'une structure cliente et des détails d'installation privés | Écartés |
| Maquette d'interface d'origine | Contient des titres réels d'une bibliothèque | Écartée (les styles repris sont déjà dans `src/`) |
| Classeurs de données réelles, bases SQLite, sauvegardes, médias | Aucun dans l'instantané (contrôle par type de fichier) | — |
| Fichiers `.env` réels | Aucun ; seul `.env.example`, sans secret | Publié |
| Outils propres à l'installation de développement (déploiement, barrières locales) | Adresses et chemins propres à une machine | Écartés avec leurs essais |
| Données de test | Données inventées ; réponses BnF et Sudoc sous Licence Ouverte de l'État, source et date mentionnées ; une image d'essai unie, sans contenu protégé | Publiées |
| Exemples nommant une structure réelle | Un exemple de docstring et son essai | Remplacés par un nom neutre |
| Adresses réseau | Seules des adresses de documentation ou réservées dans les essais | Publiées |

## 2. Secrets

- `detect-secrets` (analyse de tous les fichiers publiés) : 8 signalements, **tous des faux positifs** : empreintes
  SHA-256 (migrations, binaire Tailwind), mots de passe de comptes d'essai inventés, adresses avec identifiants
  servant à vérifier qu'elles sont **refusées**, mot de passe tiré au hasard par le script de recette.
- Recherche des formes connues (jetons GitHub, clés AWS, clés privées, jetons Slack, clés d'API) : aucune.
- L'application n'a besoin d'aucun secret de configuration (voir `INSTALLATION-SYNOLOGY.md`).

## 3. Licences

| Élément | Licence | Compatibilité avec l'EUPL-1.2 |
|---|---|---|
| 47 dépendances Python d'exécution | MIT, BSD, ISC, Apache-2.0, PSF, MIT-CMU ; MPL-2.0 pour `certifi` | Compatibles (MPL-2.0 au niveau du fichier, sans modification) |
| htmx, Alpine.js | 0BSD, MIT | Compatibles |
| Polices | SIL OFL 1.1 | Compatibles, licences jointes |
| Icônes | Production du projet ; ressemblances avec Feather (MIT) et Lucide (ISC), licences jointes par précaution | Compatibles |
| Tailwind CSS (construction) | MIT | Compatible |

Inventaire détaillé : `THIRD_PARTY_NOTICES.md`, `docs/PROVENANCE.md`.

## 4. Image

Construite par GitHub Actions depuis l'étiquette de version : base `python:3.12-slim` et `uv` figés par empreinte,
dépendances verrouillées (`uv.lock`), date des fichiers fixée (`SOURCE_DATE_EPOCH`, horodatages réécrits) ; analyse
Trivy (vulnérabilités critiques corrigeables bloquantes) et `pip-audit` ; recette complète du paquet distribué
(`scripts/recette_distribution.sh`) **avant** tout envoi ; image retéléchargée par son empreinte et comparée à l'image
éprouvée. Aucun secret, aucune donnée, aucun compte dans les couches ; utilisateur 10001.

## 5. Recette du paquet (répétition locale de la publication)

Avant la mise en ligne, la chaîne de publication a été rejouée hors de GitHub, sur une machine ARM64 avec Docker,
pour l'image `linux/amd64` (émulée) construite exactement comme le fera GitHub Actions :

| Contrôle | Résultat |
|---|---|
| Reproductibilité | Deux constructions sans cache, même commit et même date : **même image** (`sha256:4f7da51d…`) ; obtenue en retirant le fichier d'horodatage d'installation de `uv` et les attestations de construction automatiques |
| Installation vierge avec `deploy/synology/compose.yaml` tel quel | 503 avant migration, aucune base créée d'elle-même |
| Migrations à la main | `biblyo db upgrade` : 0001 à 0012, intégrité et schéma contrôlés ; conteneur sain ensuite |
| Import | Désactivé par défaut (vérifié avant toute activation) |
| Parcours HTTP | 33/33 : installation, administrateur par jeton, paramètres, logo, comptes, emplacements, import d'un classeur fabriqué, réconciliation, aller-retour, second import refusé, couverture, rangement par lot sans fausse disponibilité, présence confirmée, prêt et retour par inventaire, permissions, en-têtes de sécurité, sauvegarde |
| Conteneur | Utilisateur 10001, système de fichiers en lecture seule, aucune capacité |
| Restauration en place | Sauvegarde, changement, restauration depuis le terminal du conteneur puis redémarrage : état sauvegardé rétabli, état remplacé conservé à côté |
| Persistance | 5/5 après redémarrage (données, nom, logo, couverture) |
| Restauration isolée | Réseau coupé, volume à part : base intègre, 30 exemplaires, 2 fichiers de couverture |
| Journal, ressources | Aucune erreur ; 187 Mio de mémoire au repos (mesure sous émulation) |

Sur GitHub Actions, la même recette s'exécute **avant** l'envoi vers GHCR, puis l'image retéléchargée par son
empreinte est comparée à l'image éprouvée.
