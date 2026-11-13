#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Garde-fous Docker (08-DOCKER.md, § 5) : uniquement les ressources de biblyO original.

Refuse tout autre montage, toute surcharge de commande, tout renommage de volume, et toute référence aux anciens
mécanismes. Se lance aussi dans l'image, où PyYAML vient avec `uvicorn[standard]`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "biblyo-original"
SERVICE = "app"
CONTAINER = "biblyo-original-dev"
IMAGE = "biblyo-original:dev"
PORT = "18890:8000"
DATA_VOLUME = "biblyo_original_dev_data"
BACKUP_VOLUME = "biblyo_original_dev_backups"
VOLUME_MOUNTS = {f"{DATA_VOLUME}:/data", f"{BACKUP_VOLUME}:/backups"}
TAILWIND_EXCEPTION = "!.cache/tailwind/*-tailwindcss-linux-*"

#: Ressources existantes et anciens mécanismes, jamais référencés.
FORBIDDEN = (
    r"\bbiblyo_dev_data\b",
    r"\bbiblyo-dev\b",
    r"\bbiblyo:dev\b",
    r"\b18889\b",
    r"\b18888\b",
    r"\bshelf_data",
    r"/opt/shelf-",
    r"\bpatch_\w+\.py\b",
    r"\bbootstrap\.py\b",
    r"dangahagan/",
)
#: Un service qui contournerait les protections d'un conteneur.
DANGEROUS_KEYS = (
    "command",
    "entrypoint",
    "privileged",
    "network_mode",
    "pid",
    "ipc",
    "cap_add",
    "devices",
    "env_file",
    "volumes_from",
    "user",
    "extra_hosts",
)


def _check_service(service: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    expected = (("container_name", CONTAINER), ("image", IMAGE), ("read_only", True), ("ports", [PORT]))
    for key, value in expected:
        if service.get(key) != value:
            problems.append(f"{key} : attendu {value!r}, trouvé {service.get(key)!r}")
    mounts = service.get("volumes") or []
    if not all(isinstance(mount, str) for mount in mounts) or set(mounts) != VOLUME_MOUNTS:
        problems.append(f"volumes : seuls {sorted(VOLUME_MOUNTS)} sont permis, trouvé {mounts!r}")
    if service.get("tmpfs") != ["/tmp"]:  # noqa: S108
        problems.append("tmpfs : seul /tmp est attendu")
    if "no-new-privileges:true" not in (service.get("security_opt") or []):
        problems.append("security_opt : no-new-privileges:true est obligatoire")
    build = service.get("build")
    if not isinstance(build, dict) or build.get("context") != "." or set(build) - {"context", "args"}:
        problems.append("build : seul le contexte « . » et des arguments sont permis")
    elif (build.get("args") or {}).get("BIBLYO_COMMIT") != "${BIBLYO_COMMIT:-inconnu}":
        problems.append("build : l'argument BIBLYO_COMMIT doit transmettre le commit construit")
    if service.get("cap_drop") != ["ALL"]:
        problems.append("cap_drop : ALL est obligatoire")
    problems.extend(f"clé interdite dans le service : {key}" for key in DANGEROUS_KEYS if key in service)
    environment = service.get("environment") or {}
    if isinstance(environment, dict) and environment.get("BIBLYO_ENV") == "production":
        problems.append("BIBLYO_ENV : ce fichier sert au développement")
    return problems


def check_compose(text: str) -> list[str]:
    try:
        compose = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"compose.yml illisible : {exc}"]
    if not isinstance(compose, dict):
        return ["compose.yml vide ou invalide"]
    problems: list[str] = []
    if compose.get("name") != PROJECT:
        problems.append(f"name : attendu {PROJECT!r}")
    services = compose.get("services") or {}
    if set(services) != {SERVICE}:
        problems.append(f"services : seul {SERVICE!r} est attendu, trouvé {sorted(services)}")
    if SERVICE in services:
        problems.extend(_check_service(services[SERVICE]))
    volumes = compose.get("volumes") or {}
    if set(volumes) != {DATA_VOLUME, BACKUP_VOLUME}:
        problems.append(f"volumes déclarés : {sorted(volumes)}")
    for name in (DATA_VOLUME, BACKUP_VOLUME):
        declaration = volumes.get(name) or {}
        if declaration.get("name") != name:
            problems.append(f"volume {name} : son nom doit rester {name!r}, sinon la base repartirait de zéro")
        if declaration.get("external"):
            problems.append(f"volume {name} : ne doit pas être externe")
    return problems


def check_dockerfile(text: str) -> list[str]:
    problems: list[str] = []
    final_stage = text[text.rindex("FROM ") :] if "FROM " in text else ""
    users = re.findall(r"^USER\s+(\S+)", final_stage, re.M)
    if not users or users[-1] in ("root", "0"):
        problems.append("Dockerfile : l'image doit s'exécuter sous un utilisateur non privilégié")
    if not re.search(r'^CMD \["biblyo", "serve"', final_stage, re.M):
        problems.append("Dockerfile : la commande doit rester `biblyo serve`")
    if re.search(r"^ENTRYPOINT", final_stage, re.M):
        problems.append("Dockerfile : pas de ENTRYPOINT, qui pourrait migrer au démarrage")
    if re.search(r"^(?:CMD|ENTRYPOINT).*(?:db.*upgrade|alembic)", text, re.M):
        problems.append("Dockerfile : aucune migration au démarrage")
    if re.search(r"^FROM\s+\S+:latest\b|^COPY --from=\S+:latest\b", text, re.M):
        problems.append("Dockerfile : pas de version « latest »")
    if not re.search(r"^HEALTHCHECK", text, re.M):
        problems.append("Dockerfile : HEALTHCHECK obligatoire")
    css_stage = text[text.find(" AS css") : text.find(" AS runtime")]
    if not re.search(r"^ENV BIBLYO_CSS_HORS_LIGNE=1$", css_stage, re.M):
        problems.append("Dockerfile : la feuille de style se construit hors ligne (BIBLYO_CSS_HORS_LIGNE=1)")
    if not re.search(r"^COPY \.cache/tailwind/ ", css_stage, re.M):
        problems.append("Dockerfile : le binaire de Tailwind vient de .cache/tailwind, jamais d'un téléchargement")
    if re.search(r"^RUN .*(?:curl|wget|urlretrieve|https?://)", text, re.M):
        problems.append("Dockerfile : aucun téléchargement explicite pendant la construction")
    if not re.search(r"^LABEL org\.opencontainers\.image\.revision=\$BIBLYO_COMMIT$", final_stage, re.M):
        problems.append("Dockerfile : l'image doit porter le commit dont elle est construite")
    return problems


def check_forbidden(text: str, name: str) -> list[str]:
    return [f"{name} : référence interdite {pattern}" for pattern in FORBIDDEN if re.search(pattern, text)]


PRODUCTION = Path("deploy/synology/compose.yaml")
PRODUCTION_REQUIRED = ("BIBLYO_PORT", "BIBLYO_BASE_URL", "BIBLYO_DOSSIER")
PRODUCTION_IMAGE = re.compile(r"^ghcr\.io/adsolutionpro/biblyo:\d+\.\d+\.\d+(?:@sha256:[0-9a-f]{64})?$")
PRODUCTION_VOLUMES = [
    "${BIBLYO_DOSSIER:?BIBLYO_DOSSIER manquant}/data:/data",
    "${BIBLYO_DOSSIER:?BIBLYO_DOSSIER manquant}/sauvegardes:/backups",
]


def check_production_compose(text: str) -> list[str]:
    """Le fichier de Container Manager : version fixe de l'image publiée, variables obligatoires, conteneur
    verrouillé, seuls les deux dossiers de biblyO montés, journaux bornés."""
    problems: list[str] = []
    try:
        compose = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{PRODUCTION} illisible : {exc}"]
    services = (compose or {}).get("services") or {}
    if set(services) != {"biblyo"}:
        return [f"{PRODUCTION} : seul le service 'biblyo' est attendu"]
    service = services["biblyo"]
    for key in DANGEROUS_KEYS:
        if key in service:
            problems.append(f"{PRODUCTION} : clé interdite {key}")
    if not PRODUCTION_IMAGE.match(str(service.get("image", ""))):
        problems.append(f"{PRODUCTION} : image ghcr.io/adsolutionpro/biblyo à version fixe attendue")
    if service.get("platform") != "linux/amd64":
        problems.append(f"{PRODUCTION} : plateforme linux/amd64 attendue (DS425+)")
    if service.get("read_only") is not True or "ALL" not in (service.get("cap_drop") or []):
        problems.append(f"{PRODUCTION} : conteneur en lecture seule, sans aucune capacité")
    if "no-new-privileges:true" not in (service.get("security_opt") or []):
        problems.append(f"{PRODUCTION} : no-new-privileges attendu")
    environment = service.get("environment") or {}
    if environment.get("BIBLYO_ENV") != "production":
        problems.append(f"{PRODUCTION} : BIBLYO_ENV doit valoir production")
    if service.get("volumes") != PRODUCTION_VOLUMES:
        problems.append(f"{PRODUCTION} : seuls les dossiers data et sauvegardes de biblyO sont montés")
    if not str((service.get("ports") or [""])[0]).endswith(":8000"):
        problems.append(f"{PRODUCTION} : le port interne de l'application est 8000")
    if (service.get("logging") or {}).get("options", {}).get("max-size") is None:
        problems.append(f"{PRODUCTION} : journaux bornés (max-size) attendus")
    for variable in PRODUCTION_REQUIRED:
        if f"${{{variable}:?" not in text:
            problems.append(f"{PRODUCTION} : {variable} doit être obligatoire (${{{variable}:?…}})")
    if re.search(r":latest\b", text):
        problems.append(f"{PRODUCTION} : pas d'étiquette « latest »")
    return problems


def check(root: Path = ROOT) -> list[str]:
    compose = (root / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    ignored = (root / ".dockerignore").read_text(encoding="utf-8")
    production = (root / PRODUCTION).read_text(encoding="utf-8") if (root / PRODUCTION).exists() else ""
    problems = [*check_compose(compose), *check_dockerfile(dockerfile)]
    if not production:
        problems.append(f"{PRODUCTION} : gabarit de production absent")
    else:
        active_production = "\n".join(x for x in production.splitlines() if not x.lstrip().startswith("#"))
        problems += check_production_compose(production) + check_forbidden(active_production, str(PRODUCTION))
    # Le commentaire de compose.yml nomme ce qu'il ne touche pas : seules les lignes actives comptent.
    active = "\n".join(line for line in compose.splitlines() if not line.lstrip().startswith("#"))
    problems += check_forbidden(active, "compose.yml") + check_forbidden(dockerfile, "Dockerfile")
    for required in (".env", "*.sqlite", "docs", "tests"):
        if required not in ignored.splitlines():
            problems.append(f".dockerignore : {required} doit être exclu de l'image")
    exceptions = [line for line in ignored.splitlines() if line.startswith("!")]
    if exceptions != [TAILWIND_EXCEPTION]:
        problems.append(f".dockerignore : seule l'exception {TAILWIND_EXCEPTION} est permise, trouvé {exceptions}")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(
        f"Docker : projet {PROJECT}, port 18890, volumes {DATA_VOLUME} et {BACKUP_VOLUME}. Aucune ressource existante."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
