#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Recette de production : parcours réel d'une installation vierge, par HTTP, comme une personne le ferait.

Lancé **dans le conteneur de recette** (`docker exec -i <conteneur> python - < scripts/recette_production.py`), il
parle au serveur en service (`http://127.0.0.1:8000`) avec les seules bibliothèques de l'image. Aucune donnée réelle :
le classeur importé est fabriqué ici (titres inventés, inventaires courts, articles inversés, collections, états).

Variables : RECETTE_PHASE (`parcours` ou `apres-redemarrage`), RECETTE_JETON (jeton d'installation),
RECETTE_MOT_DE_PASSE (mot de passe de l'administrateur de recette, jamais écrit). Sortie : une ligne JSON par
contrôle (`{"etape", "ok", "detail"}`), puis un résumé. Code de sortie 1 au premier écart.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from typing import Any

import httpx
from openpyxl import Workbook, load_workbook
from PIL import Image

BASE = os.environ.get("RECETTE_BASE", "http://127.0.0.1:8000")
PHASE = os.environ.get("RECETTE_PHASE", "parcours")
PASSWORD = os.environ.get("RECETTE_MOT_DE_PASSE", "")
LIBRARY = "Bibliothèque de recette"
RESULTS: list[dict[str, Any]] = []
HEADERS = ["Titre", "Tome", "Sous-Titre", "Auteur", "Edition", "Etat", "Cote", "Numéro d'inventaire", "Collection"]


def check(step: str, ok: bool, detail: Any = "") -> None:
    RESULTS.append({"etape": step, "ok": bool(ok), "detail": str(detail)[:300]})
    print(json.dumps(RESULTS[-1], ensure_ascii=False), flush=True)


def csrf(html: str) -> str:
    found = re.search(r'name="_csrf" value="([^"]+)"', html)
    return found.group(1) if found else ""


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)


def login(browser: httpx.Client, username: str, password: str) -> bool:
    page = browser.get("/connexion")
    response = browser.post(
        "/connexion", data={"identifiant": username, "mot_de_passe": password, "_csrf": csrf(page.text)}
    )
    return response.status_code in (302, 303)


def post(browser: httpx.Client, path: str, data: dict[str, Any] | list[tuple[str, Any]], files: Any = None,
         page: str = "/") -> httpx.Response:  # fmt: skip
    token = csrf(browser.get(page).text)
    items = list(data.items()) if isinstance(data, dict) else list(data)
    grouped: dict[str, list[Any]] = {}
    for key, value in [*items, ("_csrf", token)]:
        grouped.setdefault(key, []).append(value)
    return browser.post(path, data=grouped, files=files, headers={"X-CSRF-Token": token})


def workbook() -> bytes:
    """Classeur fabriqué de 30 lignes : formes relevées par l'audit, aucune donnée réelle."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Feuil1"
    sheet.append(HEADERS)
    for n in range(1, 31):
        series = "Marins (Les)" if n <= 10 else ("agent 99 (L')" if n <= 20 else "Recueil")
        sheet.append([
            series, (n - 1) % 10 + 1, f"Album {n}", "Dupré, Jean" if n % 3 else "Ana Mer & Léo Sel", "DARGOT",
            ("satisfaisant", "usé", "leg. usure")[n % 3], f"REC/{n:02d}", n, "Hors série" if n == 5 else None,
        ])  # fmt: skip
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (96, 96), (11, 26, 30)).save(out, "PNG")
    return out.getvalue()


def counts(browser: httpx.Client) -> dict[str, int]:
    response = browser.get("/imports/export/catalogue.xlsx")
    book = load_workbook(io.BytesIO(response.content), read_only=True)
    found = {name: max(0, book[name].max_row - 1) for name in ("oeuvres", "editions", "exemplaires", "series")}
    statuses: dict[str, int] = {}
    rows = list(book["exemplaires"].iter_rows(values_only=True))
    index = rows[0].index("statut")
    for row in rows[1:]:
        statuses[row[index]] = statuses.get(row[index], 0) + 1
    found.update({f"statut_{k}": v for k, v in statuses.items()})
    book.close()
    return found


def inventories(browser: httpx.Client) -> tuple[str, str]:
    """Un inventaire « En place » sans code-barres, et un autre encore « En préparation »."""
    book = load_workbook(io.BytesIO(browser.get("/imports/export/catalogue.xlsx").content), read_only=True)
    rows = list(book["exemplaires"].iter_rows(values_only=True))
    head = rows[0]
    ready = next(r for r in rows[1:] if r[head.index("statut")] == "available" and not r[head.index("code_barres")])
    waiting = next(r for r in rows[1:] if r[head.index("statut")] == "in_processing")
    book.close()
    return str(ready[head.index("numero_inventaire")]), str(waiting[head.index("numero_inventaire")])


def lend(browser: httpx.Client, code: str) -> httpx.Response:
    """Au comptoir : la carte du lecteur, puis le numéro tapé au clavier, puis la confirmation."""
    scan = post(browser, "/comptoir/scan", {"code": "CARTE-RECETTE"}, page="/comptoir")
    hidden = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', scan.text)
    scan = post(browser, "/comptoir/scan", [*[(k, v) for k, v in hidden if k != "_csrf"], ("code", code)])
    hidden = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', scan.text)
    return post(browser, "/comptoir/valider", [(k, v) for k, v in hidden if k != "_csrf"], page="/comptoir")


def phase_parcours() -> None:
    token = os.environ["RECETTE_JETON"]
    anonymous = client()
    health = anonymous.get("/sante").json()
    check("Santé avant installation", health.get("schema") == "ok", health)
    page = anonymous.get("/installation")
    check("Page d'installation ouverte (aucun compte)", page.status_code == 200, page.status_code)
    response = anonymous.post(
        "/installation",
        data={"jeton": token, "bibliotheque": LIBRARY, "nom": "Administration de recette",
              "identifiant": "admin.recette", "mot_de_passe": PASSWORD, "confirmation": PASSWORD,
              "_csrf": csrf(page.text)},
    )  # fmt: skip
    check("Premier administrateur créé", response.status_code in (302, 303), response.status_code)
    check("Installation refermée", anonymous.get("/installation").status_code != 200)
    admin = client()
    check("Connexion de l'administrateur", login(admin, "admin.recette", PASSWORD))
    empty = counts(admin)
    check("Aucune donnée de démonstration", empty["exemplaires"] == 0 and empty["oeuvres"] == 0, empty)

    # Paramètres
    settings = admin.get("/parametres")
    advanced = admin.get("/parametres/avance")  # 1.1.0 : rubriques ; 1.0.0 : une seule page
    controls = advanced if advanced.status_code == 200 else settings
    check("Paramètres accessibles", settings.status_code == 200 and "Contrôle des réglages" in controls.text)
    saved = post(admin, "/parametres/identite", {"nom": LIBRARY, "courriel": "recette@exemple.invalid",
                 "telephone": "00 00 00 00 00", "adresse": "Adresse de recette"}, page="/parametres")  # fmt: skip
    check("Identité enregistrée", saved.status_code == 303, saved.status_code)
    logo = post(admin, "/parametres/logo", {}, files={"logo": ("logo.png", png(), "image/png")}, page="/parametres")
    check(
        "Logo enregistré et servi", logo.status_code == 303 and anonymous.get("/identite/logo.webp").status_code == 200
    )
    rubric = admin.get("/parametres/enrichissement")  # 1.1.0 ; 1.0.0 : /admin/sources
    sources = (rubric if rubric.status_code == 200 else admin.get("/admin/sources")).text
    check(
        "Open Library inactive sans adresse de contact",
        "Open Library" in sources and ("éteinte" in sources or "désactivée" in sources),
    )

    # Comptes
    for username, role in (
        ("biblio.recette", "librarian"),
        ("benevole.recette", "volunteer"),
        ("lecture.recette", "viewer"),
    ):
        created = post(admin, "/admin/utilisateurs/nouveau", [("identifiant", username), ("nom", username),
                       ("roles", role)], page="/admin/utilisateurs/nouveau")  # fmt: skip
        check(f"Compte {role} créé", created.status_code in (200, 303) and "mot de passe" in created.text.lower(),
              created.status_code)  # fmt: skip

    # Emplacements : salle, meuble et deux étagères
    room = post(admin, "/emplacements/nouveau", {"type": "room", "nom": "Salle de recette"}, page="/emplacements")
    room_id = room.headers.get("location", "").rsplit("/", 1)[-1]
    furniture = post(admin, "/emplacements/nouveau", {"type": "furniture", "parent": room_id, "nom": "Meuble R",
                     "code": "MR", "niveaux": "2", "capacite_niveaux": "20"}, page="/emplacements")  # fmt: skip
    check("Salle, meuble et étagères créés", room.status_code == 303 and furniture.status_code == 303,
          (room.status_code, furniture.status_code))  # fmt: skip
    places = admin.get("/emplacements/deplacer-groupe?exemplaire=1").text
    shelf = re.search(r'<option value="(\d+)"[^>]*>[^<]*Niveau 1', places) or re.search(
        r'<option value="(\d+)"[^>]*>[^<]*Meuble R[^<]*/', places
    )

    # Import d'un classeur fabriqué
    off = admin.post("/imports", files={"principal": ("recette.xlsx", workbook(), "application/octet-stream")},
                     data={"_csrf": csrf(admin.get("/imports").text)})  # fmt: skip
    deposit = off.headers.get("location", "")
    check("Classeur de recette déposé", off.status_code == 303 and deposit.startswith("/imports/"), off.status_code)
    simulation = admin.get(f"{deposit}/simulation").text
    plan = re.search(r'name="plan" value="([0-9a-f]{64})"', simulation)
    check("Simulation prête, application activée sur cette instance", plan is not None)
    applied = post(admin, f"{deposit}/appliquer", {"plan": plan.group(1) if plan else "", "confirmer": "1"},
                   page=f"{deposit}/simulation")  # fmt: skip
    batch = applied.headers.get("location", "")
    check("Import appliqué", applied.status_code == 303 and "/imports/lots/" in batch, applied.status_code)
    # Couverture : un fichier du volume de données, servi, puis gardé au redémarrage et dans la sauvegarde
    cover = post(admin, "/catalogue/editions/1/couverture", {}, files={"couverture": ("c.png", png(), "image/png")},
                 page="/catalogue/notices/1")  # fmt: skip
    media = re.search(r"/medias/[0-9a-f]{64}(?:-v)?\.webp", admin.get("/catalogue/notices/1").text)
    check("Couverture enregistrée et servie", cover.status_code == 303 and media is not None
          and admin.get(media.group(0)).status_code == 200, cover.status_code)  # fmt: skip
    report = admin.get(batch).text
    check("Réconciliation conforme", "écart" not in report.split("Réconciliation")[1].split("</table>")[0])
    after = counts(admin)
    check("30 exemplaires, en préparation, sans place", after["exemplaires"] == 30
          and after.get("statut_in_processing") == 30, after)  # fmt: skip
    original = admin.get(f"{batch}/format-origine.xlsx")
    source = load_workbook(io.BytesIO(workbook()), read_only=True)
    back = load_workbook(io.BytesIO(original.content), read_only=True)
    same = [list(a) for a in source["Feuil1"].iter_rows(values_only=True)] == [
        list(b) for b in back["Feuil1"].iter_rows(values_only=True)
    ]
    check("Aller-retour au format d'origine identique", same)
    again = post(admin, f"{deposit}/appliquer", {"plan": plan.group(1) if plan else "", "confirmer": "1"})
    check("Second import refusé", again.status_code in (404, 409, 410), again.status_code)

    # Mise en rayon par lot, sans fausse disponibilité, puis présence confirmée
    listing = admin.get("/emplacements/non-ranges?origine=importes").text
    ids = re.findall(r'name="exemplaire" value="(\d+)"', listing)[:10]
    check("Exemplaires importés non rangés retrouvés", len(ids) == 10 and "30 exemplaire(s)" in listing, len(ids))
    shelf_id = shelf.group(1) if shelf else ""
    preview = admin.get("/emplacements/deplacer-groupe", params=[("exemplaire", i) for i in ids] + [("lieu", shelf_id)])
    versions = re.findall(r'name="v_(\d+)" value="(\d+)"', preview.text)
    version_place = re.search(r'name="version_lieu" value="(\d+)"', preview.text)
    fields = [("lieu", shelf_id), ("rang", ""), *[("exemplaire", i) for i in ids]]
    fields += [(f"v_{k}", v) for k, v in versions]
    fields.append(("version_lieu", version_place.group(1) if version_place else ""))
    moved = post(admin, "/emplacements/deplacer-groupe", fields)
    state = counts(admin)
    check("Rangés en lot sans être vérifiés : toujours en préparation", moved.status_code == 303
          and state.get("statut_in_processing") == 30, (moved.status_code, state))  # fmt: skip
    presence = admin.get("/emplacements/verifier", params=[("exemplaire", i) for i in ids])
    pv = re.findall(r'name="v_(\d+)" value="(\d+)"', presence.text)
    confirmed = post(
        admin, "/emplacements/verifier", [*[("exemplaire", i) for i in ids], *[(f"v_{k}", v) for k, v in pv]]
    )
    state = counts(admin)
    check(
        "Présence confirmée : 10 en place", confirmed.status_code == 303 and state.get("statut_available") == 10, state
    )

    # Prêt et retour au comptoir, par le numéro d'inventaire, sans code-barres
    registered = post(admin, "/lecteurs/nouveau", {"etape": "enregistrer", "categorie": "adult", "prenom": "Camille",
                      "nom": "Recette", "courriel": "lecteur@exemple.invalid", "telephone": "0600000000",
                      "telephone_territoire": "metropole", "carte": "CARTE-RECETTE", "foyer_mode": "aucun",
                      "homonymes_ok": "1"}, page="/lecteurs/nouveau")  # fmt: skip
    check("Lecteur de recette inscrit", registered.status_code == 303, registered.status_code)
    ready, waiting = inventories(admin)
    lent = lend(admin, waiting)
    check("Prêt refusé pour un exemplaire encore en préparation", "Prêt enregistré" not in lent.text, waiting)
    lent = lend(admin, ready)
    check(f"Prêt par l'inventaire « {ready} », sans code-barres", lent.status_code == 200
          and "Prêt enregistré" in lent.text, lent.status_code)  # fmt: skip
    returned = post(admin, "/comptoir/retours/scan", {"code": ready}, page="/comptoir/retours")
    check(f"Retour par l'inventaire « {ready} »", returned.status_code == 200 and "None" not in returned.text,
          returned.status_code)  # fmt: skip

    # Permissions et sécurité
    check("Anonyme renvoyé vers la connexion", client().get("/parametres").status_code in (302, 303))
    check("Logo public, page de connexion publique", client().get("/connexion").status_code == 200)
    headers = admin.get("/").headers
    check("En-têtes de sécurité", "content-security-policy" in headers and "x-content-type-options" in headers,
          sorted(k for k in headers if k.startswith(("content-security", "x-", "referrer"))))  # fmt: skip

    # Sauvegarde depuis l'écran
    backup = post(admin, "/parametres/sauvegardes", {}, page="/parametres")
    check("Sauvegarde créée et vérifiée depuis l'écran", backup.status_code == 303, backup.status_code)


def phase_after_restart() -> None:
    admin = client()
    check("Connexion après redémarrage", login(admin, "admin.recette", PASSWORD))
    state = counts(admin)
    check(
        "Données intactes après redémarrage", state["exemplaires"] == 30 and state.get("statut_available") == 10, state
    )
    check("Nom de la bibliothèque conservé", LIBRARY in admin.get("/").text)
    check("Logo conservé", client().get("/identite/logo.webp").status_code == 200)
    media = re.search(r"/medias/[0-9a-f]{64}(?:-v)?\.webp", admin.get("/catalogue/notices/1").text)
    check(
        "Couverture conservée (fichier du volume)", media is not None and admin.get(media.group(0)).status_code == 200
    )


def main() -> int:
    try:
        phase_after_restart() if PHASE == "apres-redemarrage" else phase_parcours()
    except Exception as exc:
        check("Erreur inattendue", False, f"{type(exc).__name__}: {exc}")
    failed = [r for r in RESULTS if not r["ok"]]
    print(json.dumps({"resume": f"{len(RESULTS) - len(failed)}/{len(RESULTS)} conformes", "ecarts": failed},
                     ensure_ascii=False))  # fmt: skip
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
