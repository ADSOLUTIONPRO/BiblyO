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

`static/icons/sprite.svg` reprend les 53 tracés de la maquette d'interface produite pour le projet. Une mesure
([`docs/provenance/icones.md`](docs/provenance/icones.md)) montre des ressemblances, surtout avec Feather Icons. Les
licences des deux jeux les plus proches sont jointes **par précaution** :

| Projet comparé | Version comparée | Licence | Texte de licence |
|---|---|---|---|
| Feather Icons | 4.29.2 | MIT | `static/icons/licenses/feather-LICENSE.txt` |
| Lucide | 1.70.0 | ISC, et MIT pour la part issue de Feather | `static/icons/licenses/lucide-LICENSE.txt` |

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

## Données de test

`tests/fixtures/sources/` : réponses de la BnF et du Sudoc (Abes), sous Licence Ouverte de l'État, avec mention de la
source et de la date (voir son `README.md`). Aucune image de couverture réelle n'est versionnée.
