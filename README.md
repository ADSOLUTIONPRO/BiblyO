# biblyO

Gestion de bibliothèque auto-hébergée, en français, pensée pour les petites bibliothèques associatives : catalogue à
trois niveaux (œuvre, édition, exemplaire), séries et cotes, rangement sur des étagères réelles, lecteurs et familles,
prêts et retours au comptoir, enrichissement depuis la BnF et le Sudoc, import d'un catalogue Excel avec simulation,
exports complets.

- **Installation sur un Synology** (Container Manager, sans ligne de commande) : [`INSTALLATION-SYNOLOGY.md`](INSTALLATION-SYNOLOGY.md)
- **Mise à jour** : [`docs/MISE-A-JOUR.md`](docs/MISE-A-JOUR.md)
- **Sauvegarde et restauration** : [`docs/SAUVEGARDE-RESTAURATION.md`](docs/SAUVEGARDE-RESTAURATION.md)
- **Notes de version** : [`CHANGELOG.md`](CHANGELOG.md)
- **Sécurité** : [`SECURITY.md`](SECURITY.md)

## Image Docker

```sh
docker pull ghcr.io/adsolutionpro/biblyo:1.0.0
```

Image `linux/amd64`, construite par GitHub Actions depuis l'étiquette de version, après les tests et une recette
complète d'installation. Chaque version a son numéro et son empreinte (digest), publiés dans les notes de version.
Aucune étiquette `latest` n'est à utiliser en production.

L'image ne contient aucune donnée, aucun compte et aucun secret. La base se crée à la main (`biblyo db upgrade`), le
premier administrateur par un jeton à usage unique (`biblyo install token`, puis la page `/installation`).

## Développement

Prérequis : Python 3.12 et [uv](https://docs.astral.sh/uv/).

```sh
make setup                                  # dépendances verrouillées (uv.lock)
python scripts/build_css.py --telecharger macos-arm64   # ou linux-x64… : CLI Tailwind, empreinte vérifiée
make css                                    # feuille de style
make check                                  # contrôles et tests
BIBLYO_ENV=development BIBLYO_DATA_DIR=./data BIBLYO_BACKUP_DIR=./sauvegardes uv run biblyo db upgrade
BIBLYO_ENV=development BIBLYO_DATA_DIR=./data BIBLYO_BACKUP_DIR=./sauvegardes uv run biblyo serve
```

`compose.yml` sert au développement local ; la production utilise `deploy/synology/compose.yaml`.

## Licence et droits

```
Copyright © 2026 [NOM CIVIL DU TITULAIRE]
Licensed under the EUPL-1.2
```

- **Code original de biblyO** : propriété de son titulaire, nommé ci-dessus, et mis à disposition sous la licence
  **EUPL-1.2** ([`LICENSE`](LICENSE), texte officiel ; la version française a la même valeur que les autres). La
  licence permet d'utiliser, de modifier, de redistribuer et de vendre biblyO, en laissant le code sous EUPL-1.2 ; elle
  ne transfère pas la propriété. Détail : [`NOTICE`](NOTICE).
- **Développement assisté par IA** : biblyO a été écrit avec Claude Code, sur les instructions du titulaire, qui l'a
  conçu, arbitré, relu et validé. Il n'est pas affirmé que chaque ligne produite ainsi fait l'objet d'un droit d'auteur
  exclusif ([audit d'indépendance](docs/rapports/AUDIT-INDEPENDANCE-BIBLYO-V1.md), section 9).
- **Composants tiers** (bibliothèques, polices, icônes Lucide, dépendances Python, paquets de l'image Docker, données
  de test) : propriété de leurs auteurs, sous leurs propres licences, jointes ;
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) et [`docs/PROVENANCE.md`](docs/PROVENANCE.md).
- **Distribution** : le compte GitHub ADSOLUTIONPRO publie le dépôt et l'image ; ce compte n'est pas le titulaire.
