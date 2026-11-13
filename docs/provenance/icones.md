# Provenance des icônes

**Conclusion : l'origine des 53 tracés de la maquette n'est pas établie.** Ils ont été repris de `biblyo-maquette.html`, qui ne la documente
pas. La mesure ci-dessous montre des ressemblances. Une ressemblance n'établit pas une origine. Seule la personne ou l'outil qui a produit la
maquette pourrait le faire.

## Mesure du 7 novembre 2026

Chaque icône est tracée puis comparée à tous les fichiers de deux jeux libres : part des pixels tracés communs aux deux dessins (IoU, de 0 à 1).
L'outil est [`scripts/measure_icons.py`](../../scripts/measure_icons.py).

| Jeu comparé | Version | Licence | Icônes |
|---|---|---|---|
| Feather Icons | 4.29.2 | MIT | 287 |
| Lucide | 1.70.0 | ISC, et MIT pour la part issue de Feather | 2 322 |

| Recouvrement | Feather | Lucide | L'un ou l'autre |
|---|---|---|---|
| ≥ 0,95 | 10 | 4 | 10 |
| ≥ 0,85 | 14 | 10 | 16 |
| ≥ 0,70 | 22 | 18 | 28 |

Un seul tracé est **identique** caractère pour caractère : `shield`, à celui de Feather.

## Lecture

| Groupe | Icônes (recouvrement avec le jeu le plus proche) | Portée |
|---|---|---|
| Formes non triviales, très proches de Feather | `shield` (1,00), `edit` (1,00), `bell` (1,00), `alert` (0,99), `heart` (0,98), `moon` (0,98), `gear` (0,94) | Ressemblance forte. Une reprise de Feather, éventuellement arrondie, est **plausible, sans être prouvée** |
| Formes proches de Lucide | `eye` (0,87), `locate` (0,86) | Dessins courants, ressemblance modérée |
| Formes élémentaires | `plus`, `x`, `chev`, `chevd` (1,00) ; `out` (0,95) ; `menu` (0,90) ; `ret` (0,89) | Croix, chevrons, flèches : peu significatif, tout jeu d'icônes les dessine ainsi |
| Les 37 autres | Sous 0,85 | Aucune ressemblance marquée avec ces deux jeux. Cela ne prouve pas qu'elles sont originales |

## Conduite retenue

- Aucune provenance n'est affirmée dans le code, la documentation ou l'interface.
- Les mentions de licence de Feather (MIT) et de Lucide (ISC) restent jointes dans `static/icons/licenses/`, par précaution. Elles sont
  compatibles avec la licence envisagée pour biblyO.
- Si l'origine est un jour établie, le [registre](registre.md) et `THIRD_PARTY_NOTICES.md` seront corrigés.
