# biblyO : cibles de développement. L'environnement virtuel se règle par UV_PROJECT_ENVIRONMENT
# (à placer hors d'un partage réseau).

# Un pipeline dans une recette échoue si l'une de ses commandes échoue, pas seulement la dernière.
# Les options sont dans SHELL : le make 3.81 fourni par macOS ignore .SHELLFLAGS.
SHELL := /bin/bash -eu -o pipefail

UV_RUN := uv run --frozen
PYTHONDONTWRITEBYTECODE := 1
export PYTHONDONTWRITEBYTECODE

.PHONY: setup test lint e2e e2e-navigateurs check cover css i18n audit fmt freeze-vendor hooks contract

setup:
	uv sync --frozen

test:
	$(UV_RUN) pytest tests -m "not e2e and not live" -n auto -p no:cacheprovider

cover:
	$(UV_RUN) pytest tests -m "not e2e and not live" -n auto -p no:cacheprovider --cov --cov-report=term

e2e:
	$(UV_RUN) python scripts/build_css.py
	$(UV_RUN) pytest tests/e2e -m e2e -p no:cacheprovider

# Parcours sur Chromium, WebKit et Firefox. Chaque moteur est lancé même si le précédent échoue ; le code final dit
# si l'un d'eux a échoué. Prérequis : `uv run --frozen playwright install chromium webkit firefox`.
e2e-navigateurs:
	$(UV_RUN) python scripts/build_css.py
	$(UV_RUN) python scripts/e2e_navigateurs.py

# Tests de contrat : appels réels aux sources bibliographiques, à la demande seulement, jamais dans les barrières.
contract:
	$(UV_RUN) pytest tests/contract -m live -p no:cacheprovider

audit:
	$(UV_RUN) pip-audit
	$(UV_RUN) pip-licenses --format=markdown --with-urls --output-file=docs/licences-python.md

lint:
	$(UV_RUN) ruff check src tests scripts
	$(UV_RUN) mypy
	$(UV_RUN) python scripts/check_independence.py
	$(UV_RUN) python scripts/check_spdx.py
	$(UV_RUN) python scripts/freeze_migrations.py --check
	$(UV_RUN) python scripts/check_templates.py
	$(UV_RUN) python scripts/check_design.py
	$(UV_RUN) python scripts/check_vendor.py
	$(UV_RUN) python scripts/check_i18n.py
	$(UV_RUN) python scripts/check_docker.py

fmt:
	$(UV_RUN) ruff check --fix src tests scripts

check: lint test

# Feuille de style : CLI autonome de Tailwind, version et empreinte figées dans le script.
css:
	$(UV_RUN) python scripts/build_css.py

# Catalogue des textes de l'interface : le français est la langue source (locales/messages.pot).
i18n:
	$(UV_RUN) python scripts/check_i18n.py --write

# À lancer après une mise à jour volontaire d'une bibliothèque ou d'une police.
freeze-vendor:
	$(UV_RUN) python scripts/check_vendor.py --write

# Installe le crochet qui refuse un commit dont la barrière échoue. À lancer une fois par clone.
hooks:
	install -m 755 scripts/hooks/pre-commit .git/hooks/pre-commit
	@echo "Crochet pre-commit installé."
