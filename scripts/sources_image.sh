#!/usr/bin/env bash
# SPDX-License-Identifier: EUPL-1.2
# Sources correspondantes des paquets Debian d'une image biblyO (obligations GPL et LGPL, notamment GPL-2.0 art. 3 et
# GPL-3.0 art. 6) : chaque paquet source, à la version exacte installée, téléchargé depuis l'archive Debian, ou à défaut
# depuis snapshot.debian.org. Échoue si une seule source manque : une image sans ses sources n'est pas publiée.
#
# Usage : scripts/sources_image.sh <image> <dossier de sortie>
# Produit : <dossier>/paquets-source.txt, les fichiers sources (.dsc, .orig.tar.*, .debian.tar.*…), SHA256SUMS.
set -euo pipefail

IMAGE="${1:?image attendue}"
OUT="${2:?dossier de sortie attendu}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

docker run --rm --network none --entrypoint dpkg-query "$IMAGE" -W -f '${source:Package} ${source:Version}\n' \
  | sort -u > "$OUT/paquets-source.txt"
echo "$(wc -l < "$OUT/paquets-source.txt") paquets source à réunir"

# L'image elle-même sert d'outil (même version de Debian, apt présent) ; conteneur jetable, root seulement ici.
docker run --rm --user 0 --entrypoint bash -v "$OUT:/out" "$IMAGE" -euo pipefail -c '
  cle="$(sed -n "s/^Signed-By: //p" /etc/apt/sources.list.d/debian.sources | head -1)"
  [ -n "$cle" ] || { echo "clé de l archive Debian introuvable" >&2; exit 1; }
  cat > /etc/apt/sources.list.d/sources.sources <<EOF
Types: deb-src
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: $cle

Types: deb-src
URIs: http://deb.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: $cle
EOF
  apt-get -qq -o APT::Sandbox::User=root update
  cd /out
  manquants=""
  while read -r paquet version; do
    if ! apt-get -qq -o APT::Sandbox::User=root source --download-only "$paquet=$version" >/dev/null 2>&1; then
      manquants="$manquants $paquet=$version"
    fi
  done < paquets-source.txt
  echo "$manquants" > manquants.txt
'

# Repli : versions retirées de l'archive courante, prises sur snapshot.debian.org (empreintes SHA-1 vérifiées).
if [ -s "$OUT/manquants.txt" ] && [ -n "$(tr -d ' \n' < "$OUT/manquants.txt")" ]; then
  python3 - "$OUT" <<'PY'
import hashlib, json, pathlib, sys, urllib.request
out = pathlib.Path(sys.argv[1])
missing = (out / "manquants.txt").read_text().split()
failed = []
for item in missing:
    package, version = item.split("=", 1)
    try:
        with urllib.request.urlopen(f"https://snapshot.debian.org/mr/package/{package}/{version}/srcfiles?fileinfo=1", timeout=60) as r:
            data = json.load(r)
        for entry in data["result"]:
            sha1 = entry["hash"]
            name = data["fileinfo"][sha1][0]["name"]
            with urllib.request.urlopen(f"https://snapshot.debian.org/file/{sha1}", timeout=300) as r:
                body = r.read()
            if hashlib.sha1(body).hexdigest() != sha1:
                raise ValueError(f"empreinte inattendue pour {name}")
            (out / name).write_bytes(body)
        print(f"repli snapshot.debian.org : {item}")
    except Exception as error:  # noqa: BLE001 — toute erreur fait échouer la collecte, avec son motif
        failed.append(f"{item} ({error})")
(out / "manquants.txt").write_text("\n".join(failed))
PY
fi

if [ -s "$OUT/manquants.txt" ] && [ -n "$(tr -d ' \n' < "$OUT/manquants.txt")" ]; then
  echo "Sources introuvables :" >&2; cat "$OUT/manquants.txt" >&2; exit 1
fi
rm -f "$OUT/manquants.txt"

# Chaque paquet source doit avoir son .dsc à la bonne version (époque retirée du nom de fichier).
while read -r paquet version; do
  fichier="${paquet}_${version#*:}.dsc"
  [ -f "$OUT/$fichier" ] || { echo "Absent : $fichier" >&2; exit 1; }
done < "$OUT/paquets-source.txt"

(cd "$OUT" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | sort | xargs sha256sum > SHA256SUMS)
echo "Sources réunies : $(wc -l < "$OUT/paquets-source.txt") paquets, $(du -sh "$OUT" | cut -f1)"
