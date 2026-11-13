# Notes de version

## 0.1.0

Première version publiée. Révisions du schéma : **0001 à 0012** (une installation neuve les applique toutes par
`biblyo db upgrade`).

- **Catalogue** : œuvres, éditions, exemplaires ; séries, tomes et cotes ; recherche plein texte ; contrôle des
  doublons ; couvertures téléversées ou trouvées (BnF, Open Library sur demande), avec provenance et droits.
- **Rangement** : sites, salles, meubles et étagères, plan de salle, rangement par scan ou par lot ; un exemplaire n'est
  « En place » qu'après un rangement vérifié.
- **Lecteurs et prêts** : lecteurs, familles et structures, comptoir de prêt et de retour (par code-barres ou numéro
  d'inventaire), renouvellements, rappels, règles par catégorie.
- **Enrichissement** : BnF et Sudoc ; Open Library désactivée tant qu'une adresse de contact n'est pas renseignée.
- **Import d'un catalogue Excel ou CSV** : dépôt, correspondance des colonnes, simulation intégrale sans écriture,
  application en une transaction après sauvegarde vérifiée ; **désactivée par défaut** sur toute instance.
- **Exports** : catalogue complet (xlsx, CSV) et classeur au format d'origine d'un import.
- **Paramètres** : identité de la bibliothèque et logo, sauvegardes, droits par rôle, contrôle des réglages.
- **Distribution** : image `linux/amd64` sur GitHub Container Registry, `compose.yaml` pour Synology Container Manager.

Points d'attention : la base se crée et se migre à la main ; la restauration se fait depuis le terminal du conteneur,
suivie d'un redémarrage (voir `docs/SAUVEGARDE-RESTAURATION.md`).
