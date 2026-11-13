# Composants tiers

Inventaire tenu à jour à chaque version ; l'inventaire Python est vérifié par `make audit`.
Voir [`docs/PROVENANCE.md`](docs/PROVENANCE.md).

## Livrés avec l'application

Empreintes vérifiées par `scripts/check_vendor.py` (`static/vendor/checksums.sha256`). Intégrité du paquet contrôlée à
l'empreinte sha512 du registre npm lors de la reprise.

| Composant | Version | Licence | Origine | Fichiers | Texte de licence |
|---|---|---|---|---|---|
| htmx | 2.0.11 | 0BSD | npm `htmx.org` | `static/vendor/htmx.min.js` | `static/vendor/licenses/htmx-LICENSE.txt` |
| Alpine.js, build CSP | 3.17.5 | MIT | npm `@alpinejs/csp` | `static/vendor/alpine-csp.min.js` | `static/vendor/licenses/alpine-LICENSE.txt` |
| Bricolage Grotesque | 5.3.0 (Fontsource) | SIL OFL 1.1 | npm `@fontsource-variable/bricolage-grotesque` | `static/fonts/bricolage-grotesque-*` | `static/fonts/licenses/bricolage-grotesque-OFL.txt` |
| Manrope | 5.3.0 (Fontsource) | SIL OFL 1.1 | npm `@fontsource-variable/manrope` | `static/fonts/manrope-*` | `static/fonts/licenses/manrope-OFL.txt` |
| JetBrains Mono | 5.3.0 (Fontsource) | SIL OFL 1.1 | npm `@fontsource-variable/jetbrains-mono` | `static/fonts/jetbrains-mono-*` | `static/fonts/licenses/jetbrains-mono-OFL.txt` |

Polices : fichiers identiques, octet pour octet, à ceux des paquets Fontsource 5.3.0 (sous-ensembles latin et latin
étendu, non retouchés). Versions et titulaires inscrits dans les fichiers : Bricolage Grotesque 1.001, © 2022 The
Bricolage Grotesque Project Authors ; Manrope 4.504, © 2019 The Manrope Project Authors ; JetBrains Mono 2.211, © 2020
The JetBrains Mono Project Authors. Aucun nom de fonte réservé n'est déclaré. Elles ne sont pas vendues seules : elles
accompagnent le logiciel, comme l'OFL le permet.

## Traitement des images

| Composant | Version | Licence | Origine | Détail |
|---|---|---|---|---|
| Pillow | 13.0.0 | MIT-CMU | PyPI `pillow` | Bibliothèques embarquées et licences : [`docs/provenance/pillow.md`](docs/provenance/pillow.md). Textes complets dans le paquet installé |

Portions of this software are copyright © The FreeType Project (www.freetype.org). All rights reserved. FreeType est embarqué par Pillow et utilisé sous la FreeType License.

## Lecture des classeurs (import, lot L6)

| Composant | Version | Licence | Origine | Détail |
|---|---|---|---|---|
| openpyxl | 3.1.5 | MIT | PyPI `openpyxl` | Lecture seule des .xlsx ; son XML passe par defusedxml (déjà dépendance), sans entités externes |
| et-xmlfile | 2.0.0 | MIT | PyPI `et-xmlfile` | Dépendance d'openpyxl |

## Icônes

| Composant | Version | Licence | Origine | Fichiers | Texte de licence |
|---|---|---|---|---|---|
| Lucide (`lucide-static`) | 1.70.0 | ISC ; MIT (Cole Bemis, Feather Icons) pour les icônes dérivées de Feather | npm `lucide-static`, empreinte sha512 vérifiée | `static/icons/sprite.svg` (53 icônes, tracés non modifiés), produit par `scripts/build_icons.py` depuis `static/icons/sources.json` | `static/icons/licenses/lucide-LICENSE.txt` |

Correspondance et historique : [`docs/provenance/icones.md`](docs/provenance/icones.md).

## Outil de construction, non livré

| Composant | Version | Licence | Usage |
|---|---|---|---|
| CLI autonome de Tailwind CSS | v4.3.3 | MIT | `make css` et image Docker. Binaire vérifié par son empreinte (`scripts/build_css.py`). La feuille produite embarque le socle de Tailwind (MIT) |

## Outils de test, non livrés

| Composant | Licence | Remarque |
|---|---|---|
| axe-playwright-python 0.1.8 | MIT (fichier `LICENSE` du paquet, ses métadonnées ne la renseignent pas) | Embarque axe-core 4.12.1 (MPL 2.0). Sert seulement à `make e2e` |
| Playwright 1.52.0 | Apache-2.0 | Sert seulement à `make e2e` |

## Dépendances Python

Déclarées dans `pyproject.toml`, verrouillées dans `uv.lock`, toutes sous licence permissive (MIT, BSD, ISC, Apache-2.0, PSF)
ou MPL-2.0 (`certifi`). Inventaire complet produit par `make audit` (`docs/licences-python.md`). La CI publie l'inventaire
de chaque version.

## Image Docker

L'image publiée part de `python:3.12-slim` (Debian 13), figée par son empreinte dans le `Dockerfile`. Elle contient donc
l'interpréteur Python (licence PSF) et des paquets Debian sous leurs propres licences, dont certains sous GPL ou LGPL
(par exemple `bash`, `coreutils`, la bibliothèque C). Ces composants sont redistribués **sans modification**. Chaque
version publiée joint la liste exacte des paquets, avec leur paquet source et sa version (`composants-image.txt`), et
celle des paquets Python avec leur licence (`composants-python.txt`). **La source correspondante de chaque paquet
Debian, à la version exacte installée, est publiée avec l'image** : image `ghcr.io/adsolutionpro/biblyo:<version>-sources`
dans le même dépôt GHCR, et archive `sources-debian-<version>.tar` jointe à la version GitHub (réunies et vérifiées par
`scripts/sources_image.sh`, conservées aussi longtemps que l'image est distribuée). Le texte de chaque licence est dans
`/usr/share/doc/<paquet>/copyright` de l'image. Ces licences s'appliquent à ces composants, pas au code de biblyO.

## Données de test

`tests/fixtures/sources/` : réponses de la BnF et du Sudoc (Abes), sous Licence Ouverte de l'État, avec mention de la
source et de la date (voir son `README.md`). Aucune image de couverture réelle n'est versionnée.
