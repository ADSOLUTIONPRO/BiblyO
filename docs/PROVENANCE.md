# Provenance et licences de biblyO

## Code de biblyO

| Qui | Rôle | Droits |
|---|---|---|
| Adrien Zerbib, sous son nom commercial AD SOLUTION PRO ([`NOTICE`](../NOTICE)) | Conception, choix, arbitrages, relectures et validations de chaque lot ; édition et distribution | Titulaire, à titre personnel, des droits qu'il détient sur les contributions originales et protégeables de biblyO, qu'il met à disposition sous **EUPL-1.2**. AD SOLUTION PRO n'est ni une personne morale distincte ni un second titulaire |
| Claude Code (Anthropic) | Rédaction assistée du code, des tests et de la documentation, sur les instructions du titulaire | Développement assumé et documenté ; il n'est pas affirmé que chaque ligne produite ainsi fait l'objet d'un droit d'auteur exclusif |
| Compte GitHub ADSOLUTIONPRO | Publication du dépôt et de l'image | Compte de distribution d'AD SOLUTION PRO, pas titulaire des droits |
| Auteurs des composants tiers | Voir ci-dessous et [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) | Leurs propres licences ; ces composants n'appartiennent pas au titulaire de biblyO |

En-tête `SPDX-License-Identifier: EUPL-1.2` dans chaque fichier source (`scripts/check_spdx.py`) ; mention de titulaire
identique dans `NOTICE`, `README.md` et la page « À propos » (`scripts/check_titulaire.py`).

## Indépendance

biblyO ne reprend **aucun code** d'un autre logiciel de gestion de bibliothèque. Une application antérieure, utilisée
auparavant par la structure pour laquelle biblyO a été conçu, a seulement servi à recenser des besoins ; son code, ses
gabarits, ses traductions et ses images ont été écartés. `scripts/check_independence.py` refuse tout identifiant ou
fichier qui en viendrait, et `scripts/check_provenance.py` toute suite de code substantielle qui en viendrait, à
chaque contrôle ([audit d'indépendance](rapports/AUDIT-INDEPENDANCE-BIBLYO-V1.md)).

## Éléments repris

| Élément | Origine | Licence | Où |
|---|---|---|---|
| htmx 2.0.11 | npm `htmx.org` | 0BSD | `src/biblyo/static/vendor/` |
| Alpine.js 3.17.5 (build CSP) | npm `@alpinejs/csp` | MIT | `src/biblyo/static/vendor/` |
| Polices Bricolage Grotesque, Manrope, JetBrains Mono | npm `@fontsource-variable/*` | SIL OFL 1.1 | `src/biblyo/static/fonts/` |
| Logo, jetons de couleur, styles | Maquette produite pour le projet | Projet | `src/biblyo/static/` |
| Icônes (53) | Lucide 1.70.0, npm `lucide-static`, tracés non modifiés | ISC, et MIT pour la part issue de Feather ; voir [`provenance/icones.md`](provenance/icones.md) | `src/biblyo/static/icons/` |
| Dépendances Python | PyPI, verrouillées (`uv.lock`) | Permissives, et MPL-2.0 pour `certifi` | Image Docker |
| Pillow et ses bibliothèques embarquées | PyPI `pillow` | MIT-CMU, voir [`provenance/pillow.md`](provenance/pillow.md) | Image Docker |
| CLI Tailwind CSS v4.3.3 | Versions GitHub de Tailwind Labs, empreinte vérifiée | MIT | Construction seulement |
| Réponses BnF et Sudoc | Services publics, relevées le 11 novembre 2026 | Licence Ouverte de l'État | `tests/fixtures/sources/` |

Les empreintes des fichiers repris sont vérifiées par `scripts/check_vendor.py`.

## Ce que le dépôt ne contient pas

Aucune donnée de bibliothèque réelle (catalogue, lecteurs, prêts), aucune base, aucune sauvegarde, aucune image de
couverture réelle, aucun secret, aucune configuration propre à une installation. Les essais utilisent des données
inventées.
