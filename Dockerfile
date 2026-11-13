# syntax=docker/dockerfile:1
# biblyO : image de l'application. Voir docs/conception/08-DOCKER.md.
# `biblyo serve` ne touche jamais au schéma : la base se met à jour par `biblyo db upgrade`, explicitement.
ARG PYTHON_VERSION=3.12
# Image de base figée par son empreinte (index multi-architecture) : une construction se refait à l'identique.
ARG PYTHON_DIGEST=sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# --- Étape 1 : feuille de style. Version et empreintes de Tailwind figées dans scripts/build_css.py ---
# Aucun téléchargement : le binaire épinglé est déposé à l'avance dans .cache/tailwind (build_css.py --telecharger),
# et son empreinte est vérifiée ici. S'il manque ou diffère, la construction s'arrête.
# Construite sur la plateforme de la machine qui construit : la feuille produite ne dépend pas de l'architecture de
# l'image, et l'image AMD64 du Synology se construit ainsi sur le Mac mini ARM64 (buildx), sans émuler Tailwind.
FROM --platform=$BUILDPLATFORM python:${PYTHON_VERSION}-slim@${PYTHON_DIGEST} AS css
ENV BIBLYO_CSS_HORS_LIGNE=1
WORKDIR /build
COPY .cache/tailwind/ .cache/tailwind/
COPY scripts/build_css.py scripts/build_css.py
COPY src/biblyo/static src/biblyo/static
COPY src/biblyo/templates src/biblyo/templates
COPY src/biblyo/modules src/biblyo/modules
RUN python scripts/build_css.py

# --- Étape 2 : exécution ---
FROM python:${PYTHON_VERSION}-slim@${PYTHON_DIGEST} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    BIBLYO_DATA_DIR=/data \
    BIBLYO_BACKUP_DIR=/backups \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1
RUN useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin biblyo \
 && mkdir -p /data /backups \
 && chown biblyo:biblyo /data /backups
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=css /build/src/biblyo/static/dist ./src/biblyo/static/dist
# uv note dans uv_cache.json l'heure de l'installation : retiré (et de RECORD), pour qu'une même version donne la même image.
RUN uv sync --frozen --no-dev && rm /usr/local/bin/uv \
 && info="$(echo .venv/lib/python3.12/site-packages/biblyo-*.dist-info)" \
 && rm "$info/uv_cache.json" && sed -i '/uv_cache.json/d' "$info/RECORD"
USER biblyo
EXPOSE 8000
# Commit exact dont l'image est construite : vérifié par scripts/docker_mise_a_jour.sh avant toute migration.
ARG BIBLYO_COMMIT=inconnu
LABEL org.opencontainers.image.revision=$BIBLYO_COMMIT
LABEL org.opencontainers.image.source="https://github.com/ADSOLUTIONPRO/biblyo" \
      org.opencontainers.image.title="biblyO" \
      org.opencontainers.image.description="Gestion de bibliothèque auto-hébergée, en français" \
      org.opencontainers.image.licenses="EUPL-1.2"
ENV BIBLYO_COMMIT=$BIBLYO_COMMIT
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/sante', timeout=4).status == 200 else 1)"]
CMD ["biblyo", "serve", "--host", "0.0.0.0", "--port", "8000"]
