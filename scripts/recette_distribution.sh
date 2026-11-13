#!/usr/bin/env bash
# SPDX-License-Identifier: EUPL-1.2
# Recette du paquet distribué : l'image donnée, déployée avec deploy/synology/compose.yaml tel quel, dans un dossier
# jetable, comme sur un Synology. Lancée par la publication (avant tout envoi vers GHCR) et à la main.
#
#   bash scripts/recette_distribution.sh <image> [port]
#
# Vérifie : installation vierge (503 avant migration, aucune base), migrations à la main jusqu'à la dernière révision,
# conteneur sain, import désactivé par défaut, jeton d'installation, parcours HTTP complet (scripts/recette_production.py),
# utilisateur et système de fichiers, sauvegarde puis restauration **en place** suivie d'un redémarrage (procédure de
# docs/SAUVEGARDE-RESTAURATION.md), persistance après redémarrage, restauration isolée, journaux sans erreur.
# Ne supprime que ce qu'il a créé : le projet de recette, son dossier jetable et son volume d'essai.
set -euo pipefail

IMAGE="${1:?image attendue, par exemple ghcr.io/adsolutionpro/biblyo:1.0.0}"
PORT="${2:-18080}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/biblyo-recette.XXXXXX")"
PROJECT="biblyo-recette-$$"
CONTAINER=biblyo
COMPOSE=(docker compose -p "$PROJECT" --project-directory "$WORK" -f "$WORK/compose.yaml")
TRIAL="${PROJECT}_restauration"

step() { echo; echo "== $*"; }
fail() { echo "ÉCART : $*"; exit 1; }
cleanup() {
  "${COMPOSE[@]}" down >/dev/null 2>&1 || true  # jamais -v : aucun volume nommé ici, seulement des dossiers
  docker volume rm "$TRIAL" >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || sudo rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT
in_container() { docker exec "$CONTAINER" "$@"; }
http_status() {
  in_container python -c "import urllib.request as u, urllib.error as e
try: print(u.urlopen('http://127.0.0.1:8000/sante', timeout=4).status)
except e.HTTPError as x: print(x.code)"
}
wait_running() {
  for _ in $(seq 1 90); do http_status >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}
wait_healthy() {
  for _ in $(seq 1 90); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null)" = "healthy" ] && return 0
    sleep 2
  done
  return 1
}
driver() { docker exec -i "$@" "$CONTAINER" python - < "$ROOT/scripts/recette_production.py"; }

step "Dossiers, comme sur le NAS : data et sauvegardes, propriétaire 10001"
mkdir -p "$WORK/data" "$WORK/sauvegardes"
if [ "$(id -u)" = 0 ]; then chown -R 10001:10001 "$WORK/data" "$WORK/sauvegardes"
elif sudo -n true 2>/dev/null; then sudo chown -R 10001:10001 "$WORK/data" "$WORK/sauvegardes"
else chmod 777 "$WORK/data" "$WORK/sauvegardes"  # Docker Desktop : propriétaire non transmis, droits ouverts à l'essai
fi
sed "s#image: ghcr.io/adsolutionpro/biblyo:[0-9.]*#image: ${IMAGE}#" "$ROOT/deploy/synology/compose.yaml" > "$WORK/compose.yaml"
cat > "$WORK/.env" <<EOF
BIBLYO_PORT=$PORT
BIBLYO_DOSSIER=$WORK
BIBLYO_BASE_URL=http://127.0.0.1:$PORT
EOF
docker image inspect -f 'image : {{.Id}} ({{.Os}}/{{.Architecture}}), révision {{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE"
[ "$(docker image inspect -f '{{.Architecture}}' "$IMAGE")" = "amd64" ] || fail "image non amd64"

step "Installation vierge"
"${COMPOSE[@]}" up -d
wait_running || fail "le serveur ne répond pas"
[ "$(http_status)" = "503" ] || fail "avant migration, /sante devait répondre 503"
echo "conforme : 503 avant migration, aucune base créée d'elle-même"
if in_container biblyo db status >/dev/null 2>&1; then fail "une base existe déjà"; fi

step "Migrations, à la main"
in_container biblyo db upgrade
in_container biblyo db status
wait_healthy || fail "conteneur non sain après migration"
echo "conforme : sain après migration"
in_container biblyo import application | grep -q ": désactivée" || fail "l'import doit être désactivé par défaut"
echo "conforme : application des imports désactivée par défaut"

step "Parcours HTTP (import activé pour la seule recette)"
TOKEN="$(in_container biblyo install token | sed -n 's/^Jeton d’installation : //p')"
[ -n "$TOKEN" ] || fail "jeton d'installation"
in_container biblyo import application --activer
PASSWORD="Recette-$(openssl rand -hex 12)"
driver -e RECETTE_PHASE=parcours -e "RECETTE_JETON=$TOKEN" -e "RECETTE_MOT_DE_PASSE=$PASSWORD" | tee "$WORK/parcours.jsonl"
grep -q '"resume": "[0-9]*/[0-9]* conformes", "ecarts": \[\]' "$WORK/parcours.jsonl" || fail "parcours HTTP"

step "Sécurité du conteneur"
[ "$(in_container id -u)" = "10001" ] || fail "utilisateur"
if in_container sh -c 'touch /app/essai' 2>/dev/null; then fail "système de fichiers inscriptible"; fi
[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}' "$CONTAINER")" = "true [ALL]" ] || fail "verrouillage"
echo "conforme : utilisateur 10001, lecture seule, aucune capacité"

step "Sauvegarde, puis restauration en place (procédure documentée)"
in_container biblyo backup create
ARCHIVE="$(in_container sh -c 'ls -1t /backups/biblyo-*.tar.gz | head -1' | xargs basename)"
in_container biblyo backup verify "$ARCHIVE"
in_container biblyo import application --desactiver   # changement après la sauvegarde, que la restauration doit défaire
in_container biblyo backup restore "$ARCHIVE" --confirmer "$ARCHIVE"
docker restart "$CONTAINER" >/dev/null
wait_healthy || fail "non sain après restauration et redémarrage"
in_container biblyo import application | grep -q ": activée" || fail "la restauration n'a pas rendu l'état sauvegardé"
echo "conforme : état de la sauvegarde rétabli"

step "Persistance après redémarrage"
driver -e RECETTE_PHASE=apres-redemarrage -e "RECETTE_MOT_DE_PASSE=$PASSWORD" | tee "$WORK/apres.jsonl"
grep -q '"ecarts": \[\]' "$WORK/apres.jsonl" || fail "persistance"

step "Restauration isolée (réseau coupé, volume à part)"
docker volume create "$TRIAL" >/dev/null
docker run --rm --platform linux/amd64 --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -e BIBLYO_ENV=production -v "$WORK/sauvegardes:/backups:ro" -v "$TRIAL:/data" "$IMAGE" \
  sh -c "biblyo backup restore '$ARCHIVE' --confirmer '$ARCHIVE' && biblyo db check && python -c \"
import pathlib, sqlite3
c = sqlite3.connect('/data/biblyo.sqlite')
n = c.execute('SELECT COUNT(*) FROM copies').fetchone()[0]
f = len(list(pathlib.Path('/data/media').glob('*.webp')))
print(f'restaurée : {n} exemplaires, {f} fichier(s) de couverture'); assert n == 30 and f >= 2\""
echo "conforme : restauration isolée"

step "Journaux et ressources"
if docker logs "$CONTAINER" 2>&1 | grep -E "Traceback|level=ERROR"; then fail "erreur dans le journal"; fi
echo "conforme : journal sans erreur"
docker stats --no-stream --format "mémoire {{.MemUsage}}, processeur {{.CPUPerc}}" "$CONTAINER"

echo
echo "RECETTE CONFORME : $IMAGE"
