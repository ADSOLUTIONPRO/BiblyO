# ADR 0003 — Trois niveaux : œuvre, édition, exemplaire

**Statut** : accepté, décision D1 de l'utilisateur du 7 novembre 2026. Remplace la table unique `records` du dossier de conception initial.

## Contexte

Le modèle initial plaçait dans `records` à la fois l'œuvre et son édition : une seule ligne portait le titre, les auteurs, l'ISBN, l'éditeur et l'année. Deux éditions d'un même tome devenaient deux notices sans lien entre elles, et un ouvrage ne pouvait avoir qu'un éditeur et une année.

Une bibliothèque possède pourtant couramment plusieurs éditions d'une même œuvre, et plusieurs exemplaires d'une même édition. L'utilisateur demande que le modèle le représente dès L1, sans se limiter à un seul ISBN ou une seule année par œuvre.

## Décision

| Niveau | Table | Répond à | Porte |
|---|---|---|---|
| Œuvre ou tome | `works` | « Quel ouvrage ? » | Titre, auteurs, série et tome, section, catégories, genres, mots-clés, résumé, public |
| Édition | `editions` | « Quelle parution ? » | Identifiants, éditeur, collection, année, mention d'édition, langue, pages, couverture, traducteur |
| Exemplaire | `copies` | « Quel objet sur l'étagère ? » | Code-barres, statut. Le reste arrive en L2 |

- **La notice de l'interface est une œuvre** avec ses éditions. La grille et la liste montrent une carte par œuvre, avec son édition principale.
- **Une œuvre a au moins une édition, et une seule principale.** L'index partiel `ux_editions_primary` garantit le « une seule », le service garantit le « au moins une ».
- **Rien ne rapproche automatiquement deux ouvrages.** Deux œuvres peuvent porter le même titre, le même tome ou le même ISBN. Un doublon probable est signalé, jamais fusionné ni refusé.
- **La table `copies` existe dès L1 avec son seul noyau** : éditer, créer, retirer un exemplaire sans historique. La cote, l'emplacement, l'état et l'acquisition s'ajoutent au lot L2 par `ALTER TABLE ADD COLUMN`, et les prêts au lot L4 par une table distincte. Aucune donnée n'est réécrite.

## Conséquences

- Les filtres et la recherche portent sur l'œuvre mais regardent **toutes** ses éditions : une œuvre se trouve par l'ISBN, l'éditeur ou l'année de n'importe laquelle. L'index `works_search` est donc tenu par œuvre.
- La fiche gagne un onglet **Éditions**, absent de la maquette, qui signalait déjà ce besoin comme « à concevoir ».
- Une édition a sa propre version : deux personnes peuvent modifier en même temps l'œuvre et une édition sans se gêner.
- Les lots L5 (`enrichment_proposals`, `edition_sources`) et L6 (import) visent l'édition. Le document `03` est aligné.
- La saisie reste simple : créer une notice crée en une fois l'œuvre et sa première édition.

## Écartés

| Option | Raison |
|---|---|
| Garder `records` et relier les éditions par une clé de regroupement | Répète titre et auteurs sur chaque ligne, et laisse diverger les doublons |
| Modèle FRBR complet (œuvre, expression, manifestation, item) | Le niveau « expression » n'apporte rien à une bibliothèque associative. Traducteur et langue vont sur l'édition |
| Créer `copies` au complet dès L1 | Figerait des colonnes avant les règles de cote et d'emplacement de L2 |
