# Sauvegarder et restaurer biblyO

## Ce qu'une sauvegarde contient

Une archive `biblyo-<installation>-<date>-<motif>.tar.gz` dans le dossier `sauvegardes` : une copie cohérente de la base
(prise sans arrêter le service), toutes les couvertures (`media/`) et un manifeste (empreintes, révision du schéma,
comptes de chaque table). **Une archive n'est publiée qu'après vérification** : empreintes, intégrité de la base, clés
étrangères. Le logo de la bibliothèque est dans la base : il part avec elle.

## Sauvegarder

- **Depuis biblyO** (compte d'administration) : **Paramètres** → **Sauvegardes** → **Créer une sauvegarde
  maintenant**. Le bouton **Vérifier** de chaque archive la contrôle de nouveau.
- **Depuis le terminal du conteneur** (Container Manager → Conteneur → `biblyo` → Détails → Terminal → `sh`) :

  ```sh
  biblyo backup create
  biblyo backup list
  biblyo backup verify <nom-de-l-archive>
  ```

- **Avant chaque migration**, `biblyo db upgrade` prend lui-même une sauvegarde vérifiée (motif `pre-migration`).

## Copier hors du NAS

Une sauvegarde qui reste sur le NAS ne protège pas d'une panne du NAS. Avec **Hyper Backup** (Centre de paquets) :
**Créer** → **Tâche de sauvegarde de données** → destination hors du NAS (disque USB, autre NAS, nuage) → cochez le
dossier `docker/biblyo/sauvegardes` → planification quotidienne → rotation des versions. Ne cochez pas les dossiers
d'autres applications sans le vouloir.

## Restaurer

Une restauration **remplace** la base et les couvertures par celles de l'archive. L'état remplacé n'est jamais détruit :
il est déplacé dans `data/avant-restauration-<date>/`.

1. Choisissez un moment où personne n'utilise biblyO.
2. Container Manager → Conteneur → `biblyo` → Détails → **Terminal** → `sh` :

   ```sh
   biblyo backup list
   biblyo backup restore <nom-de-l-archive> --confirmer <nom-de-l-archive>
   ```

   (le nom est répété exprès : c'est la confirmation).
3. **Aussitôt après**, Container Manager → Conteneur → `biblyo` → **Action** → **Redémarrer** : le service rouvre la
   base restaurée.
4. Vérifiez : `biblyo db status` dans le terminal, puis connexion et contenu dans biblyO.

Une archive d'une version plus récente que l'application installée est refusée : installez d'abord cette version.

## Essayer une restauration sans toucher à l'instance

Pour vérifier qu'une archive se restaure, sans risque pour le service en marche, une personne à l'aise avec SSH peut la
restaurer dans un conteneur jetable, réseau coupé :

```sh
docker volume create biblyo_essai_restauration
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
  -v /volume1/docker/biblyo/sauvegardes:/backups:ro -v biblyo_essai_restauration:/data \
  ghcr.io/adsolutionpro/biblyo:0.1.0 \
  sh -c 'biblyo backup restore <archive> --confirmer <archive> && biblyo db check'
docker volume rm biblyo_essai_restauration
```

Ces commandes ne touchent qu'un volume créé pour l'essai, puis supprimé : jamais les dossiers de l'instance.
