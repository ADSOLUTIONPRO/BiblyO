# Provenance et licences de biblyO

## Code de biblyO

Écrit pour ce projet, sous **EUPL-1.2** (en-tête `SPDX-License-Identifier: EUPL-1.2` dans chaque fichier source,
contrôlé par `scripts/check_spdx.py`). Développé avec une assistance par intelligence artificielle, relu, essayé et
validé par le responsable du projet.

## Indépendance

biblyO ne reprend **aucun code** d'un autre logiciel de gestion de bibliothèque. Une application antérieure, utilisée
auparavant par la structure pour laquelle biblyO a été conçu, a seulement servi à recenser des besoins ; son code, ses
gabarits, ses traductions et ses images ont été écartés. `scripts/check_independence.py` refuse tout identifiant ou
fichier qui en viendrait, à chaque contrôle.

## Éléments repris

| Élément | Origine | Licence | Où |
|---|---|---|---|
| htmx 2.0.11 | npm `htmx.org` | 0BSD | `src/biblyo/static/vendor/` |
| Alpine.js 3.17.5 (build CSP) | npm `@alpinejs/csp` | MIT | `src/biblyo/static/vendor/` |
| Polices Bricolage Grotesque, Manrope, JetBrains Mono | npm `@fontsource-variable/*` | SIL OFL 1.1 | `src/biblyo/static/fonts/` |
| Logo, jetons de couleur, styles | Maquette produite pour le projet | Projet | `src/biblyo/static/` |
| Icônes (53 tracés) | Maquette produite pour le projet ; ressemblances avec Feather (MIT) et Lucide (ISC), licences jointes par précaution | Voir [`provenance/icones.md`](provenance/icones.md) | `src/biblyo/static/icons/` |
| Dépendances Python | PyPI, verrouillées (`uv.lock`) | Permissives, et MPL-2.0 pour `certifi` | Image Docker |
| Pillow et ses bibliothèques embarquées | PyPI `pillow` | MIT-CMU, voir [`provenance/pillow.md`](provenance/pillow.md) | Image Docker |
| CLI Tailwind CSS v4.3.3 | Versions GitHub de Tailwind Labs, empreinte vérifiée | MIT | Construction seulement |
| Réponses BnF et Sudoc | Services publics, relevées le 11 novembre 2026 | Licence Ouverte de l'État | `tests/fixtures/sources/` |

Les empreintes des fichiers repris sont vérifiées par `scripts/check_vendor.py`.

## Ce que le dépôt ne contient pas

Aucune donnée de bibliothèque réelle (catalogue, lecteurs, prêts), aucune base, aucune sauvegarde, aucune image de
couverture réelle, aucun secret, aucune configuration propre à une installation. Les essais utilisent des données
inventées.
