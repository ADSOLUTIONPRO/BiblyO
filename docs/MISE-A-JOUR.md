# Mettre biblyO à jour

Une mise à jour ne se fait jamais toute seule : c'est une décision, en cinq étapes, réversible.

## 1. Lire les notes de version

Page *Releases* du dépôt et `CHANGELOG.md` : nouvelle version, **révisions de schéma** apportées (migrations), points
d'attention. Notez la version actuelle (biblyO → **Paramètres** → Informations techniques, ou le `compose.yaml` du
projet).

## 2. Sauvegarder

biblyO → **Paramètres** → **Créer une sauvegarde maintenant** (ou `biblyo backup create` dans le terminal). Vérifiez-la
(bouton **Vérifier**). Copiez-la hors du NAS si l'Hyper Backup du jour n'est pas encore passé.

## 3. Changer de version

1. Container Manager → **Projet** → `biblyo` → **Action** → **Arrêter**.
2. **Projet** → `biblyo` → onglet **YAML** (ou **Action** → **Modifier**) : remplacez seulement le numéro de version de
   la ligne `image:`, par exemple `ghcr.io/adsolutionpro/biblyo:1.0.0` → `ghcr.io/adsolutionpro/biblyo:1.1.0`.
   Le `compose.yaml` publié avec la nouvelle version peut aussi être collé tel quel s'il change davantage : ses notes le
   disent. **Enregistrer**.
3. **Action** → **Construire** : Container Manager télécharge la nouvelle image et recrée le conteneur, avec les mêmes
   dossiers `data` et `sauvegardes`.

## 4. Migrer, explicitement

Le conteneur démarre « non sain » tant que la base n'est pas migrée. Terminal du conteneur (`sh`) :

```sh
biblyo db upgrade
biblyo db status
```

`biblyo db upgrade` prend d'abord une sauvegarde vérifiée (`pre-migration`), applique les révisions, puis contrôle
intégrité et schéma. Sur un échec, la base n'est pas modifiée.

## 5. Vérifier

État **sain** dans Container Manager, journal sans erreur, connexion, quelques écrans (catalogue, prêts, paramètres).

## En cas d'échec : revenir à la version précédente

1. Remettez l'ancien numéro de version dans le YAML, **Construire**.
2. Si la migration avait été appliquée, restaurez l'archive `pre-migration` de l'étape 4
   ([`SAUVEGARDE-RESTAURATION.md`](SAUVEGARDE-RESTAURATION.md)), puis **Redémarrer**.

## À ne jamais faire

- `docker compose down -v`, `docker system prune`, `docker volume prune`, ou toute suppression globale : ce sont les
  données de biblyO, et celles des autres applications du NAS.
- Supprimer les dossiers `data` ou `sauvegardes`.
- Utiliser l'étiquette `latest` : chaque installation désigne une version précise.
