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

## Licence

Code de biblyO : **EUPL-1.2** ([`LICENSE`](LICENSE)), version française de même valeur que les autres versions
linguistiques. Composants tiers, polices, icônes et données de test : [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
et [`docs/PROVENANCE.md`](docs/PROVENANCE.md).
