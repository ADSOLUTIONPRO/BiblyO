# Réponses enregistrées des sources bibliographiques

Jeux de réponses réelles de la BnF et du Sudoc, relevés le **11 novembre 2026** pendant les essais exploratoires du
lot L5 ([`16-PLAN-L5.md`](../../../docs/conception/16-PLAN-L5.md) § 3). Ils servent aux tests des connecteurs : aucun
test de la barrière n'appelle un service réel.

## Origine et conditions

| Dossier | Source | Conditions | Mention à conserver |
|---|---|---|---|
| `bnf/` | Catalogue général de la BnF, service SRU `https://catalogue.bnf.fr/api/SRU` | Licence Ouverte de l'État (Etalab 2.0), réutilisation libre avec mention de la source et de la date | « Bibliothèque nationale de France », zones `801$b` et `801$c` de chaque notice, date de relevé ci-dessus |
| `sudoc/` | Sudoc (Abes) : SRU `https://www.sudoc.abes.fr/cbs/sru/`, `isbn2ppn`, notice par PPN | Licence Ouverte de l'État, réutilisation libre avec mention de la source et de la date de récupération | « Abes », zones `801$b` et `801$c` de chaque notice, date de relevé ci-dessus |

Aucune image de couverture n'est conservée ici : leurs conditions de réutilisation ne sont pas encore établies
(décision D13 du plan).

## Ce qui a été retiré

Les réponses sont brutes, à trois exceptions près, toutes appliquées par un script et jamais à la main :

- **Sudoc** : toute zone portant un sous-champ `$5` est retirée. Ce sont les données d'exemplaire des bibliothèques
  du réseau (cotes, notes de don, numéros d'exemplaire) ; elles ne décrivent pas l'ouvrage, peuvent nommer des
  personnes, et le connecteur ne doit jamais les lire. La réponse d'origine en compte de 17 à 304 selon le fichier.
- **Sudoc** : le bloc `extraResponseData` (jeton de session du serveur) et l'identifiant de jeu de résultats sont
  vidés.
- **BnF** : `meme-titre-plusieurs-editeurs.xml` ne garde que 12 notices sur les 50 reçues (305 Ko à l'origine). Le
  nombre annoncé par `numberOfRecords` reste celui de la source.

Un test devra vérifier que le connecteur ignore les zones `$5` même quand elles sont présentes : il les ajoutera
lui-même à une notice, sans dépendre de ces fichiers.

## Contenu

| Fichier | Cas |
|---|---|
| `bnf/isbn-exact-petit-prince.xml` | ISBN connu, une notice, résumé et couverture signalés |
| `bnf/isbn10-zero-resultat.xml` | Même livre cherché par son ISBN-10 avec `bib.isbn adj` : zéro résultat alors que la notice existe |
| `bnf/isbn-inconnu.xml` | ISBN valide absent du catalogue |
| `bnf/isbn-deux-notices-elementaires.xml` | Un ISBN, deux notices élémentaires (statut `sparse`) : dans l'une, auteurs sans lien d'autorité ni code de fonction |
| `bnf/bd-asterix-unimarc.xml`, `-intermarc.xml`, `-dublincore.xml` | La même notice de bande dessinée dans les trois formats |
| `bnf/serie-plusieurs-editions.xml` | Un titre, de nombreuses éditions, et une notice d'ensemble de la série |
| `bnf/bd-tome-meme-titre.xml` | Un tome sous plusieurs éditions et collections |
| `bnf/manga-serie-notices-d-ensemble.xml` | Série longue : tomes, notices d'ensemble, notice sans code de fonction |
| `bnf/serie-tome-par-ensemble.xml` | Un tome retrouvé par la notice d'ensemble (`bib.ens2bib`) |
| `bnf/titre-auteur-330-resultats.xml` | Recherche par titre et auteur à 330 résultats |
| `bnf/meme-titre-plusieurs-editeurs.xml` | Même titre chez de nombreux éditeurs |
| `bnf/ancien-sans-isbn.xml` | Éditions de 1913 sans ISBN, mêlées à des rééditions |
| `bnf/accents-apostrophes.xml` | Titre avec apostrophe et accent ; documents sonores et vidéo mêlés aux livres |
| `bnf/diagnostic-requete-malformee.xml`, `diagnostic-index-inconnu.xml` | Erreurs de requête rendues en HTTP 200 |
| `bnf/explain.xml`, `sudoc/explain.xml` | Description des services : critères, schémas, limites |
| `sudoc/sru-isbn-petit-prince.xml` | ISBN connu ; zones dans le désordre |
| `sudoc/notice-par-ppn.xml` | La même notice par `https://www.sudoc.fr/<PPN>.xml` : indicateurs `#`, zones de contrôle en plus |
| `sudoc/isbn2ppn-un-ppn.xml`, `isbn2ppn-deux-ppn.xml`, `isbn2ppn-inconnu-http-404.xml` | Service `isbn2ppn` ; le dernier est le corps d'une réponse HTTP 404 |
| `sudoc/sru-isbn-inconnu.xml` | Aucun résultat : seul le diagnostic 61, sans valeur |
| `sudoc/sru-isbn-deux-notices.xml` | Un ISBN, deux tirages décrits séparément |
| `sudoc/sru-isbn-quatre-notices-deux-titres.xml` | Un ISBN porté par deux albums différents |
| `sudoc/sru-bd-tome-en-200h.xml` | Tome porté par le titre (`200$h`, `$i`), chiffres romains, éditions étrangères |
| `sudoc/sru-isbn-tome-titre-propre.xml` | Relevé le 12 novembre 2026, pendant la recette sur DEV : One Piece, tome 72, titré par le tome (`200$a` la série, `$h` « Tome [72] », `$i` « Les oubliés de Dressrosa ») ; 11 zones `$5` retirées |
| `sudoc/sru-titre-auteur.xml` | Recherche par titre et auteur : recueils et livres audio mêlés |
| `sudoc/sru-dublincore.xml` | Dublin Core du Sudoc : quatre éléments seulement |

## Relevé du 14 novembre 2026 (lot L7)

Cas réels de la production, pour l'assistant d'enrichissement : « Cerveau-choc » (Achille Talon, Greg, Dargaud) et
« Du scénario à la réalisation » (Duc, Glénat). Fichiers `bnf/l7-*.xml` et `sudoc/l7-*.xml` : réponses aux requêtes
exactes que fait le service pour chaque niveau de recherche (mêmes adresses, même nombre de notices demandées),
nettoyées par les mêmes règles que ci-dessus (zones `$5` du Sudoc retirées, jeton de session vidé). `l7/manifeste.json`
fait correspondre chaque adresse à son fichier.

`l7/*.json` : réponses d'Open Library (recherche d'œuvres, notices d'ISBN), métadonnées seulement, que l'Internet
Archive publie sans restriction ; **aucune image n'est conservée**, les tests les remplacent par des images générées.
