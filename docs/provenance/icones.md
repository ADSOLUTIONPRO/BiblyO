# Provenance des icônes

**Origine établie : les 53 icônes de `static/icons/sprite.svg` sont des icônes Lucide 1.70.0, non modifiées**, sous
licence ISC ; celles que Lucide a reprises de Feather Icons restent sous licence MIT. Le texte complet des deux licences
est dans `static/icons/licenses/lucide-LICENSE.txt` (fichier `LICENSE` du paquet, à l'identique).

## Comment le sprite est produit

- `static/icons/sources.json` fixe le paquet (`lucide-static` 1.70.0), son adresse sur le registre npm, son empreinte
  sha512, et la correspondance entre chaque nom employé par biblyO et l'icône Lucide retenue.
- `scripts/build_icons.py` télécharge le paquet, refuse toute autre empreinte, et écrit chaque symbole avec le contenu du
  fichier SVG de Lucide, tel quel. Seuls les blancs de mise en page entre balises sont retirés.
- `scripts/build_icons.py --check` (CI) reconstruit le sprite et échoue s'il diffère d'un seul caractère.
  `tests/unit/test_icons.py` vérifie hors réseau les symboles déclarés, la mention de licence, et que chaque icône
  appelée par un gabarit existe.

L'épaisseur du trait (1,75) et la couleur sont réglées par la feuille de style (`.ic`), sans toucher aux tracés.

## Correspondance

| biblyO | Lucide | biblyO | Lucide | biblyO | Lucide |
|---|---|---|---|---|---|
| `alert` | `triangle-alert` | `gear` | `settings` | `pin` | `map-pin` |
| `archive` | `archive` | `grid` | `layout-grid` | `plus` | `plus` |
| `bell` | `bell` | `heart` | `heart` | `print` | `printer` |
| `book` | `book` | `home` | `house` | `ret` | `undo-2` |
| `building` | `building` | `info` | `info` | `scan` | `scan-barcode` |
| `check` | `check` | `inventory` | `clipboard-check` | `search` | `search` |
| `chev` | `chevron-right` | `io` | `arrow-down-up` | `series` | `library` |
| `chevd` | `chevron-down` | `key` | `key-round` | `shelf` | `library-big` |
| `clock` | `clock` | `keyboard` | `keyboard` | `shield` | `shield` |
| `copy` | `copy` | `list` | `list` | `star` | `star` |
| `download` | `download` | `loans` | `arrow-right-left` | `stats` | `chart-column` |
| `edit` | `pen-line` | `locate` | `locate` | `sun` | `sun` |
| `enrich` | `sparkles` | `menu` | `menu` | `tag` | `tag` |
| `eye` | `eye` | `merge` | `merge` | `trash` | `trash-2` |
| `eyeoff` | `eye-off` | `moon` | `moon` | `undo` | `undo` |
| `filter` | `funnel` | `more` | `ellipsis` | `upload` | `upload` |
| `open` | `book-open` | `out` | `corner-down-right` | `users` | `users` |
| `x` | `x` | `zoom` | `zoom-in` | | |

## Historique : le sprite de la maquette, remplacé

Jusqu'à la version préparée le 12 novembre 2026, le sprite reprenait les 53 tracés de la maquette d'interface produite
pour le projet, dont l'origine n'était pas documentée. Une mesure de recouvrement (7 novembre 2026) les rapprochait de
Feather Icons 4.29.2 et de Lucide 1.70.0 : sept formes non triviales à 0,94 ou plus de Feather (`shield` identique
caractère pour caractère, `edit`, `bell`, `alert`, `heart`, `moon`, `gear`), deux de Lucide (`eye`, `locate`), sept
formes élémentaires, 37 sous 0,85. Une ressemblance n'établissant pas une origine, et une origine inconnue ne pouvant
être ni attribuée ni revendiquée, **ces tracés ont tous été retirés** (audit d'indépendance de la version 1.0.0,
[`AUDIT-INDEPENDANCE-BIBLYO-V1.md`](../rapports/AUDIT-INDEPENDANCE-BIBLYO-V1.md)). Aucun n'a été retouché pour
masquer une ressemblance : ils sont remplacés par des icônes dont l'auteur et la licence sont connus.

## Autres images

| Fichier | Nature | Provenance |
|---|---|---|
| `static/icons/logo.svg` et macro `logo` (`templates/components/icons.html`) | Monogramme : carré arrondi, barre, anneau | Maquette produite pour le projet (voir le rapport d'audit, section 9). Sans ressemblance avec le logo de l'application antérieure (livre ouvert sur fond violet) |
| Anneaux, barres de remplissage, mini-plans (`templates/components/layout.html`, `shelves.html`) | Formes géométriques calculées | Code de biblyO |
| `tests/fixtures/couverture-essai.jpg` | Aplat de couleur 120 × 180, sans motif | Produit pour les essais |

Aucune autre image, photo, couverture ou illustration n'est versionnée.
