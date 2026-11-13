# Procédure de publication de biblyO 1.0.0

À suivre seulement après le dernier feu vert du titulaire sur le commit de version et sur la mention de titulaire.
Chaque étape vérifie la précédente ; **si une étape échoue, on s'arrête**, on ne passe pas aux étapes qui en dépendent,
et on établit un diagnostic. Les valeurs entre chevrons se relèvent au moment de l'étape : elles ne sont pas connues à
l'avance et ne doivent pas être devinées.

## 0. Préalables

1. Mention du titulaire inscrite (`Copyright © 2026 Adrien Zerbib (AD SOLUTION PRO)`), contrôlée par
   `scripts/check_titulaire.py --publication`. Décision prise sur le nom « biblyO » (rapport d'audit, section 10).
2. Sur ce commit exact : `make check` réussi en local, puis CI « Contrôles » et répétition « Publication »
   (`gh workflow run publication.yml --ref main`) réussies sur GitHub.
3. `git status` propre, `main` local identique à `origin/main` :

   ```sh
   git fetch origin && git status -sb          # attendu : ## main...origin/main, rien à valider
   git rev-parse HEAD origin/main               # deux fois le même <SHA_VERSION>
   python3 scripts/check_titulaire.py --publication
   ```

## 1. Étiquette annotée

```sh
git tag -a v1.0.0 -m "biblyO 1.0.0" <SHA_VERSION>
git show v1.0.0 --stat | head -5             # vérifie le commit désigné
```

## 2. Envoi de l'étiquette

```sh
git push origin v1.0.0                       # jamais --force, jamais --tags
```

## 3 à 7. Flux « Publication » (automatique sur l'étiquette)

```sh
gh run list --workflow publication.yml --limit 1
gh run watch <ID_RUN> --exit-status
```

Le flux enchaîne, et s'arrête au premier échec : contrôles et parcours navigateur → refus si le titulaire n'est pas
nommé → image `linux/amd64` reproductible → Trivy (critiques corrigeables bloquantes) → recette complète du paquet
Synology (installation, migration, compte, prêt, sauvegarde, restauration, restauration isolée, redémarrage) → liste des
composants de l'image → **seulement alors** envoi vers GHCR (`1.0.0` et `sha-<12 caractères>`) → l'image retéléchargée
par son empreinte doit être celle qui a été éprouvée → version GitHub « biblyO 1.0.0 » avec les notes du `CHANGELOG`,
l'empreinte, `compose.yaml`, `.env.example`, `trivy.txt`, `composants-image.txt`, `composants-python.txt`,
`sources-debian-1.0.0.tar` ; l'image `1.0.0-sources` (sources des paquets Debian) part sur GHCR avec l'image.

Relever dans le journal et sur la page de la version :

```sh
gh release view v1.0.0                       # <URL_VERSION>, fichiers joints
gh release view v1.0.0 --json body --jq .body | grep -E "Empreinte|Référence"   # <DIGEST> sha256:…
```

## 8. Dépôt GitHub en public

Décision irréversible en pratique (le code peut être copié dès la première minute). Par l'interface : **Settings →
General → Danger Zone → Change visibility → Public**, ou :

```sh
gh repo edit ADSOLUTIONPRO/BiblyO --visibility public --accept-visibility-change-consequences
```

## 9. Paquet GHCR en public

Le paquet `biblyo` porte à la fois `1.0.0`, `sha-<12 caractères>` et `1.0.0-sources` : les sources deviennent
publiques en même temps que l'image, comme les licences GPL le demandent. Ne jamais supprimer `1.0.0-sources`
tant que `1.0.0` reste distribuée.

Interface : profil ADSOLUTIONPRO → **Packages** → `biblyo` → **Package settings** → **Change visibility → Public**.
Vérifier dans les mêmes réglages que le paquet est relié au dépôt `ADSOLUTIONPRO/BiblyO`.

## 10. Téléchargement sans authentification

Depuis une machine sans session GHCR (`docker logout ghcr.io` d'abord) :

```sh
docker logout ghcr.io
docker pull ghcr.io/adsolutionpro/biblyo:1.0.0
docker image inspect -f '{{index .RepoDigests 0}}' ghcr.io/adsolutionpro/biblyo:1.0.0   # doit finir par <DIGEST>
docker pull ghcr.io/adsolutionpro/biblyo@<DIGEST>
```

## 11. Démarrage depuis les artefacts publiés

Dans un dossier vide, avec les fichiers **téléchargés depuis la version** (pas ceux du dépôt local) :

```sh
gh release download v1.0.0 -p compose.yaml -p env.example   # fichier joint « .env.example »
cp env.example .env    # BIBLYO_PORT=18999, BIBLYO_DOSSIER=$PWD/essai, BIBLYO_BASE_URL=http://localhost:18999
mkdir -p essai/data essai/sauvegardes && sudo chown -R 10001:10001 essai   # ou équivalent
docker compose -p biblyo-essai-publication up -d
docker compose -p biblyo-essai-publication exec -T biblyo biblyo db upgrade
curl -fsS http://localhost:18999/sante
docker compose -p biblyo-essai-publication exec -T biblyo biblyo backup create
docker compose -p biblyo-essai-publication down          # jamais -v ; puis supprimer le dossier essai à la main
```

Le `compose.yaml` fixe `container_name: biblyo` : faire l'essai sur une machine où aucun conteneur `biblyo` n'existe.
Ne jamais faire cet essai sur le Synology de production, et ne jamais y lancer de nettoyage global.

## 12. Installation sur le Synology

`compose.yaml` de la version (identique à `deploy/synology/compose.yaml`), `.env.example`, et le guide
[`INSTALLATION-SYNOLOGY.md`](../../INSTALLATION-SYNOLOGY.md). L'installation sur le NAS de production est une étape
distincte, à autoriser à part.

## En cas d'échec

| Échec | Conduite |
|---|---|
| Avant l'envoi vers GHCR | Rien n'est publié. Supprimer l'étiquette distante (`git push origin :refs/tags/v1.0.0`) et locale, corriger sur `main`, reprendre à l'étape 0 |
| Après l'envoi, avant la version GitHub | L'image existe mais n'est pas annoncée : diagnostiquer, ne pas rendre le paquet public ; relancer le seul travail échoué si la cause est extérieure |
| Après publication | Ne jamais réutiliser `1.0.0` pour une autre image : corriger dans une version `1.0.1` |
