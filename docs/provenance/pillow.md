# Pillow : vérification de licence

**Date** : 7 novembre 2026. **Décision** : autorisée par l'utilisateur, après cette vérification. **Version** : 13.0.0, épinglée dans `uv.lock`.

**Usage** : contrôler et réencoder les couvertures téléversées (`core/media.py`). Seuls les décodeurs JPEG, PNG et WebP sont sollicités : `Image.open(..., formats=...)` refuse tout autre format.

## Résultat

| Élément | Résultat | Vérifié dans |
|---|---|---|
| Licence de Pillow | MIT-CMU (HPND), permissive | Métadonnées PyPI, fichier `LICENSE` du paquet |
| Dépendances Python | **Aucune** | `Requires-Dist` hors options facultatives |
| Compatibilité avec l'EUPL-1.2 | Licence permissive, sans obligation de réciprocité. **Ce n'est pas un avis juridique** | Lecture du texte |

## Bibliothèques embarquées

Paquet inspecté : `pillow-13.0.0-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl`, celui de l'image Docker du Mac mini. Son fichier `LICENSE` reproduit les textes de ces composants, sur 1 574 lignes. Le tableau les résume : **les textes du paquet font foi**.

| Composant dans `pillow.libs` | Licence, d'après ce fichier |
|---|---|
| libavif, et AOM, dav1d et libyuv qu'il utilise | Licences de type BSD |
| Brotli | MIT |
| FreeType | **Double licence : FreeType (FTL) ou GPLv2. L'option FTL s'applique ici.** Voir ci-dessous |
| HarfBuzz | « Old MIT » |
| LCMS2 | MIT |
| libjpeg-turbo | Licence IJG et autres textes permissifs |
| liblzma | Domaine public, d'après le résumé de XZ Utils reproduit dans ce fichier |
| libpng | PNG Reference Library License |
| libtiff | Licence permissive de libtiff |
| libwebp, sharpyuv | Licence de type BSD |
| OpenJPEG | BSD à 2 clauses |
| libXau, libxcb | Licences permissives de type MIT |
| Zstandard | Licence BSD |

## Points d'attention

| Point | Position |
|---|---|
| **FreeType** | L'option FTL demande de citer le projet dans la documentation d'un programme distribué. La mention figure dans `THIRD_PARTY_NOTICES.md`. biblyO ne dessine aucun texte avec Pillow |
| **Mentions GPL et LGPL dans le fichier** | L'option GPLv2 de FreeType, non retenue. Et le résumé de XZ Utils, qui place ces licences sur d'autres fichiers de son projet (`getopt_long` des outils en ligne de commande), pas sur `liblzma`. Aucune obligation n'en découle ici à ma lecture, **sous réserve d'un examen juridique avant toute publication** |
| Autres architectures | **Seul le paquet Linux arm64 a été inspecté**, celui de l'image du Mac mini. Inspection à refaire si l'image change d'architecture |
| Vulnérabilités | `make audit` contrôle Pillow à chaque lot. Le réencodage systématique et les plafonds limitent l'exposition des décodeurs |
