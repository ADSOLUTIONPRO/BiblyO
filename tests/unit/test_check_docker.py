# SPDX-License-Identifier: EUPL-1.2
"""Garde-fous Docker : passent sur le dépôt, échouent sur des configurations piégées."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_docker", ROOT / "scripts" / "check_docker.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

COMPOSE = (ROOT / "compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_repository_passes():
    assert lint.check() == []


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ('"18890:8000"', '"18889:8000"', "ports"),
        ('"18890:8000"', '"8000:8000"', "ports"),
        ("name: biblyo-original\n", "name: autre\n", "name"),
        ("container_name: biblyo-original-dev", "container_name: biblyo-dev", "container_name"),
        ("image: biblyo-original:dev", "image: biblyo:dev", "image"),
        ("- biblyo_original_dev_data:/data", "- biblyo_dev_data:/data", "volumes"),
        ("- biblyo_original_dev_data:/data", "- ./donnees:/data", "volumes"),
        (
            "- biblyo_original_dev_backups:/backups\n",
            "- biblyo_original_dev_backups:/backups\n      - /:/hote\n",
            "volumes",
        ),
        ("    name: biblyo_original_dev_data", "    name: biblyo_dev_data", "son nom doit rester"),
        ("    name: biblyo_original_dev_backups", "    name: sauvegardes", "son nom doit rester"),
        ("    read_only: true\n", "", "read_only"),
        ("    cap_drop:\n      - ALL\n", "", "cap_drop"),
        ("      - no-new-privileges:true\n", "      - seccomp:unconfined\n", "no-new-privileges"),
        ("      - /tmp\n", "      - /tmp\n      - /data\n", "tmpfs"),
        ('        BIBLYO_COMMIT: "${BIBLYO_COMMIT:-inconnu}"\n', "        AUTRE: x\n", "BIBLYO_COMMIT"),
        ("      context: .\n", "      context: ..\n", "build"),
        ("      context: .\n", "      context: .\n      additional_contexts:\n        x: /\n", "build"),
    ],
)
def test_trapped_compose_is_refused(old, new, fragment):
    assert old in COMPOSE
    problems = lint.check_compose(COMPOSE.replace(old, new))
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    "extra",
    [
        'command: ["biblyo", "db", "upgrade"]',
        'entrypoint: ["sh", "-c", "biblyo db upgrade && biblyo serve"]',
        "privileged: true",
        "network_mode: host",
        "cap_add: [NET_ADMIN]",
        "env_file: .env",
        "user: root",
    ],
)
def test_dangerous_service_key_is_refused(extra):
    trapped = COMPOSE.replace("    read_only: true\n", f"    read_only: true\n    {extra}\n")
    assert any("clé interdite" in problem for problem in lint.check_compose(trapped))


def test_second_service_is_refused():
    trapped = COMPOSE.replace(
        "volumes:\n  biblyo_original_dev_data:", "  autre:\n    image: x\n\nvolumes:\n  biblyo_original_dev_data:"
    )
    assert any("services" in problem for problem in lint.check_compose(trapped))


def test_external_volume_is_refused():
    trapped = COMPOSE.replace(
        "    name: biblyo_original_dev_data", "    name: biblyo_original_dev_data\n    external: true"
    )
    assert any("externe" in problem for problem in lint.check_compose(trapped))


def test_production_environment_is_refused_here():
    trapped = COMPOSE.replace("BIBLYO_ENV: development", "BIBLYO_ENV: production")
    assert any("BIBLYO_ENV" in problem for problem in lint.check_compose(trapped))


@pytest.mark.parametrize("text", ["", ": : :", "- une liste"])
def test_invalid_compose_is_reported(text):
    assert lint.check_compose(text)


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("USER biblyo\n", "", "non privilégié"),
        ("USER biblyo\n", "USER root\n", "non privilégié"),
        ('CMD ["biblyo", "serve"', 'CMD ["sh", "-c"', "biblyo serve"),
        ('CMD ["biblyo", "serve", "--host"', 'CMD ["biblyo", "db", "upgrade", "--host"', "biblyo serve"),
        ("EXPOSE 8000\n", 'EXPOSE 8000\nENTRYPOINT ["sh"]\n', "ENTRYPOINT"),
        ("ghcr.io/astral-sh/uv:0.13.7", "ghcr.io/astral-sh/uv:latest", "latest"),
        ("HEALTHCHECK", "# HEALTHCHECK", "HEALTHCHECK"),
        ("ENV BIBLYO_CSS_HORS_LIGNE=1\n", "", "hors ligne"),
        ("COPY .cache/tailwind/ .cache/tailwind/\n", "", ".cache/tailwind"),
        (
            "RUN python scripts/build_css.py",
            "RUN curl -fsSL https://github.com/x -o t && python scripts/build_css.py",
            "téléchargement",
        ),
        ("LABEL org.opencontainers.image.revision=$BIBLYO_COMMIT\n", "", "commit"),
    ],
)
def test_trapped_dockerfile_is_refused(old, new, fragment):
    assert old in DOCKERFILE
    problems = lint.check_dockerfile(DOCKERFILE.replace(old, new))
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    "reference",
    [
        "biblyo_dev_data",
        "biblyo-dev",
        "18889",
        "18888",
        "/opt/shelf-x",
        "patch_loans.py",
        "bootstrap.py",
        "dangahagan/x",
    ],
)
def test_old_resources_are_refused(reference):
    assert lint.check_forbidden(f"quelque chose {reference} ici", "fichier")


def test_real_files_do_not_mention_old_resources():
    active = "\n".join(line for line in COMPOSE.splitlines() if not line.lstrip().startswith("#"))
    assert lint.check_forbidden(active, "compose.yml") == []
    assert lint.check_forbidden(DOCKERFILE, "Dockerfile") == []


def test_commented_warning_does_not_hide_an_active_reference(tmp_path):
    for name in ("Dockerfile", ".dockerignore"):
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    trapped = COMPOSE.replace('BIBLYO_TRUSTED_PROXIES: ""', 'BIBLYO_TRUSTED_PROXIES: "biblyo-dev"')
    (tmp_path / "compose.yml").write_text(trapped, encoding="utf-8")
    assert any("référence interdite" in problem for problem in lint.check(tmp_path))


@pytest.mark.parametrize("required", [".env", "*.sqlite", "docs", "tests"])
def test_dockerignore_must_exclude_private_files(tmp_path, required):
    (tmp_path / "compose.yml").write_text(COMPOSE, encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    (tmp_path / ".dockerignore").write_text("\n".join(line for line in lines if line != required), encoding="utf-8")
    assert any(required in problem for problem in lint.check(tmp_path))


@pytest.mark.parametrize("extra", ["!.env", "!data", "!.cache"])
def test_dockerignore_admits_no_other_exception(tmp_path, extra):
    (tmp_path / "compose.yml").write_text(COMPOSE, encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    (tmp_path / ".dockerignore").write_text(ignored + extra + "\n", encoding="utf-8")
    assert any("seule l'exception" in problem for problem in lint.check(tmp_path))


# --- Fichier de Container Manager (deploy/synology/compose.yaml) ---------------------------------------------------
SYNOLOGY = (ROOT / "deploy" / "synology" / "compose.yaml").read_text(encoding="utf-8")


def test_the_synology_compose_passes():
    assert lint.check_production_compose(SYNOLOGY) == []


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("biblyo:0.1.0", "biblyo:latest", "version fixe"),
        ("platform: linux/amd64", "platform: linux/arm64", "linux/amd64"),
        ("BIBLYO_ENV: production", "BIBLYO_ENV: development", "production"),
        ("read_only: true", "read_only: false", "lecture seule"),
        ('"${BIBLYO_PORT:?BIBLYO_PORT manquant : port libre du NAS}:8000"', '"18080:8000"', "BIBLYO_PORT"),
        ("/sauvegardes:/backups", "/sauvegardes:/backups\n      - /volume1/docker/shelf:/autre", "seuls les dossiers"),
        ('max-size: "10m"', 'max-files: "10m"', "journaux"),
    ],
)
def test_a_trapped_synology_compose_is_refused(old, new, fragment):
    assert old in SYNOLOGY
    assert any(fragment in problem for problem in lint.check_production_compose(SYNOLOGY.replace(old, new)))


def test_a_privileged_synology_compose_is_refused():
    trapped = SYNOLOGY.replace("    restart: unless-stopped", "    restart: unless-stopped\n    privileged: true")
    assert any("privileged" in problem for problem in lint.check_production_compose(trapped))
