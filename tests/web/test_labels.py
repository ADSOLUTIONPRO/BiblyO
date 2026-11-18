# SPDX-License-Identifier: EUPL-1.2
"""Pages « Codes-barres et étiquettes » (lot L8) : génération globale, sélection, un livre, assistant d'impression,
feuille entamée, lots (reprise, réimpression, confirmation), réglages, et PDF relus code par code."""

from __future__ import annotations

import re
import sqlite3

from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import CopyInput

from .. import pdf_reader
from . import test_import_shelving as shelving

database = shelving.database  # base avec l'import activé
imported = shelving.imported  # 23 exemplaires importés sans code-barres, comme ceux du Club


def _rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _fingerprint(html: str) -> str:
    match = re.search(r'name="empreinte" value="([0-9a-f]+)"', html)
    assert match, "pas d'empreinte dans la simulation"
    return match.group(1)


def _generate_all(browser) -> None:
    page = browser.get("/etiquettes/generer").text
    done = browser.post("/etiquettes/generer", data={"empreinte": _fingerprint(page), "portee": "tous"})
    assert done.status_code == 303 and done.headers["location"] == "/etiquettes?genere=1"


def test_permissions(client_as, imported):
    assert client_as("viewer").get("/etiquettes").status_code == 403
    volunteer = client_as("volunteer")  # holdings.edit, sans labels.print
    page = volunteer.get("/etiquettes")
    assert page.status_code == 200 and "Générer tous les codes manquants" in page.text
    assert "Derniers lots" not in page.text
    assert volunteer.get("/etiquettes/imprimer?code=avec").status_code == 403
    assert volunteer.get("/etiquettes/feuille-test.pdf").status_code == 403
    assert client_as("librarian").get("/etiquettes/imprimer?code=avec").status_code == 200


def test_generation_globale_simulee_puis_confirmee(client_as, settings, imported):
    browser = client_as("librarian")
    overview = browser.get("/etiquettes").text
    assert "Sans code-barres" in overview and ">23<" in overview
    sequence = _rows(settings, "SELECT * FROM sequences")
    page = browser.get("/etiquettes/generer")
    assert page.status_code == 200 and "23 code(s) à attribuer" in page.text and "EX-000001" in page.text
    assert _rows(settings, "SELECT * FROM sequences") == sequence  # ouvrir la simulation n'attribue rien
    assert _rows(settings, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL") == [(0,)]
    _generate_all(browser)
    after = browser.get("/etiquettes?genere=1").text
    assert "Les codes-barres manquants ont été attribués. Vous pouvez maintenant imprimer les étiquettes." in after
    assert _rows(settings, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL") == [(23,)]


def test_simulation_perimee_refusee(client_as, settings, imported):
    browser = client_as("librarian")
    fingerprint = _fingerprint(browser.get("/etiquettes/generer").text)
    copy_id = sorted(imported.values())[0]
    holdings.update_copy(shelving_db(settings), copy_id, CopyInput(barcode="MAIN-7", inventory_number=""), None, None)
    refused = browser.post("/etiquettes/generer", data={"empreinte": fingerprint, "portee": "tous"})
    assert refused.status_code == 409 and "relancez la simulation" in refused.text
    assert _rows(settings, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL") == [(1,)]


def shelving_db(settings):
    from biblyo.core.db import Database

    return Database(settings.database_path)


def test_selection_puis_generation(client_as, settings, imported):
    browser = client_as("librarian")
    page = browser.get("/etiquettes/selection", params={"q": "Marins", "code": "sans"})
    assert page.status_code == 200 and "3 exemplaire(s)" in page.text
    simulated = browser.post("/etiquettes/selection/generer", data={"q": "Marins", "code": "sans"})
    assert simulated.status_code == 200 and "3 code(s) à attribuer" in simulated.text
    done = browser.post(
        "/etiquettes/generer",
        data={"empreinte": _fingerprint(simulated.text), "portee": "selection", "q": "Marins", "code": "sans"},
    )
    assert done.status_code == 303
    assert _rows(settings, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL") == [(3,)]


def test_un_livre_depuis_sa_fiche(client_as, settings, imported):
    browser = client_as("librarian")
    copy_id = imported["9"]
    sheet = browser.get(f"/exemplaires/{copy_id}").text
    assert "Pas encore de code-barres" in sheet and "Générer son code-barres" in sheet
    version = _rows(settings, "SELECT version FROM copies WHERE id = ?", copy_id)[0][0]
    done = browser.post(f"/etiquettes/exemplaire/{copy_id}/code", data={"version": str(version)})
    assert done.status_code == 303
    code = _rows(settings, "SELECT barcode, inventory_number FROM copies WHERE id = ?", copy_id)[0]
    assert code == ("EX-000001", "9")
    sheet = browser.get(f"/exemplaires/{copy_id}").text
    assert 'class="code-svg"' in sheet and "Imprimer son étiquette" in sheet and "Code attribué : EX-000001" in sheet
    stale = browser.post(f"/etiquettes/exemplaire/{copy_id}/code", data={"version": str(version)})
    assert stale.status_code == 409  # version périmée : rien n'est écrasé


def test_assistant_feuille_entamee_et_pdf(client_as, settings, imported):
    browser = client_as("librarian")
    _generate_all(browser)
    step2 = browser.get("/etiquettes/imprimer", params={"code": "a-etiqueter"})
    assert step2.status_code == 200 and "Quelle planche ?" in step2.text and "23 livre(s) choisi(s)" in step2.text
    choice = {"code": "a-etiqueter", "ordre": "inventaire", "planche": "avery-l7160", "modele": "standard"}
    preview = browser.post("/etiquettes/imprimer", data={**choice, "etape": "3", "depart": "8"})
    assert preview.status_code == 200 and 'class="sheet-svg"' in preview.text
    assert preview.text.count('class="lslot lslot-occupied"') == 7
    assert "Imprimez à 100 %" in preview.text
    assert _rows(settings, "SELECT COUNT(*) FROM label_batches") == [(0,)]  # un aperçu n'enregistre rien
    created = browser.post("/etiquettes/lots", data={**choice, "occupee": [str(n) for n in range(1, 8)]})
    assert created.status_code == 303
    batch_id = int(created.headers["location"].split("/")[3].split("?")[0])
    items = _rows(settings, "SELECT rank, copy_id, barcode, sheet_number, position FROM label_batch_items "
                            "WHERE batch_id = ? ORDER BY rank", batch_id)  # fmt: skip
    assert len(items) == 23 and items[0][3:] == (1, 8) and items[13][3:] == (1, 21) and items[14][3:] == (2, 1)
    page = browser.get(f"/etiquettes/lots/{batch_id}").text
    assert "Préparé, à confirmer" in page and "Télécharger le PDF" in page
    pdf = browser.get(f"/etiquettes/lots/{batch_id}/etiquettes.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    read = pdf_reader.pages(pdf.content)
    printed = [code.text for page in read for code in page.codes]
    assert printed == [item[2] for item in items]
    for _rank, copy_id, barcode, _sheet, _position in items:  # chaque code imprimé est celui de son exemplaire
        assert _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id) == [(barcode,)]
    assert all((round(p.width, 3), round(p.height, 3)) == (210, 297) for p in read)


def test_reprise_reimpression_et_confirmation(client_as, settings, imported):
    browser = client_as("librarian")
    _generate_all(browser)
    choice = {"code": "a-etiqueter", "ordre": "inventaire", "planche": "avery-l7651", "modele": "compact"}
    created = browser.post("/etiquettes/lots", data=choice)
    batch_id = int(created.headers["location"].split("/")[3].split("?")[0])
    resumed = browser.post(f"/etiquettes/lots/{batch_id}/reprendre", data={"a_partir": "20"})
    assert resumed.status_code == 200 and "4 livre(s) choisi(s)" in resumed.text
    assert 'name="origine" value="resume"' in resumed.text and f'name="parent" value="{batch_id}"' in resumed.text
    found = re.findall(r'<input type="hidden" name="(exemplaire|origine|parent)" value="([^"]+)">', resumed.text)
    values: dict[str, list[str]] = {}
    for name, value in found:
        values.setdefault(name, []).append(value)
    values = {name: items if name == "exemplaire" else items[:1] for name, items in values.items()}
    second = browser.post(
        "/etiquettes/lots", data={**values, "planche": "avery-l7651", "modele": "compact", "occupee": "1"}
    )
    assert second.status_code == 303
    rows = _rows(settings, "SELECT origin, parent_id, total, start_position FROM label_batches ORDER BY id")
    assert rows == [("selection", None, 23, 1), ("resume", batch_id, 4, 2)]
    first_codes = _rows(settings, "SELECT barcode FROM label_batch_items WHERE batch_id = ? AND rank >= 20 "
                                  "ORDER BY rank", batch_id)  # fmt: skip
    second_codes = _rows(settings, "SELECT barcode FROM label_batch_items WHERE batch_id = ? ORDER BY rank",
                         batch_id + 1)  # fmt: skip
    assert first_codes == second_codes  # mêmes codes, aucun régénéré
    assert _rows(settings, "SELECT last_value FROM sequences WHERE name = 'copy_barcode'") == [(23,)]
    confirmed = browser.post(f"/etiquettes/lots/{batch_id}/etat", data={"etat": "confirmed"})
    assert confirmed.status_code == 303
    assert _rows(settings, "SELECT status FROM label_batches WHERE id = ?", batch_id) == [("confirmed",)]
    again = browser.post(f"/etiquettes/lots/{batch_id}/etat", data={"etat": "abandoned"})
    assert again.status_code == 400  # un lot marqué ne change plus
    assert "0 exemplaire(s)" in browser.get("/etiquettes/selection", params={"code": "a-etiqueter"}).text


def test_un_code_change_empeche_de_refaire_le_lot(client_as, settings, imported):
    browser = client_as("librarian")
    _generate_all(browser)
    created = browser.post("/etiquettes/lots", data={"code": "a-etiqueter", "planche": "avery-l7160"})
    batch_id = int(created.headers["location"].split("/")[3].split("?")[0])
    copy_id = _rows(settings, "SELECT copy_id FROM label_batch_items WHERE batch_id = ? AND rank = 1", batch_id)[0][0]
    holdings.update_copy(shelving_db(settings), copy_id, CopyInput(barcode="AUTRE-1", inventory_number="AUTRE-1"),
                         None, None)  # fmt: skip
    refused = browser.get(f"/etiquettes/lots/{batch_id}/etiquettes.pdf")
    assert refused.status_code == 409 and "a changé" in refused.text
    assert "code changé depuis" in browser.get(f"/etiquettes/lots/{batch_id}").text


def test_aucun_pdf_sans_code(client_as, settings, imported):
    browser = client_as("librarian")
    refused = browser.post("/etiquettes/imprimer", data={"code": "sans", "etape": "3", "planche": "avery-l7160"})
    assert refused.status_code == 400 and "générez d’abord les codes" in refused.text
    created = browser.post("/etiquettes/lots", data={"code": "sans", "planche": "avery-l7160"})
    assert created.status_code == 400
    assert _rows(settings, "SELECT COUNT(*) FROM label_batches") == [(0,)]
    assert _rows(settings, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL") == [(0,)]


def test_reglages_feuille_test_et_prefixe(client_as, settings, imported):
    browser = client_as("librarian")
    page = browser.get("/etiquettes/reglages")
    assert page.status_code == 200 and "D’où viennent ces cotes ?" in page.text and "EX-000001" in page.text
    custom = {"planche": "personnalise", "colonnes": "3", "lignes": "8", "largeur": "70", "marge_gauche": "0"}
    bad = browser.post(
        "/etiquettes/reglages", data={**custom, "modele": "standard", "hauteur": "40", "marge_haut": "4,5"}
    )
    assert bad.status_code == 400 and "dépassent" in bad.text
    offsets = {"decalage_horizontal": "0,5", "decalage_vertical": "-1"}
    saved = browser.post(
        "/etiquettes/reglages", data={**custom, **offsets, "modele": "detaille", "hauteur": "36", "marge_haut": "4,5"}
    )
    assert saved.status_code == 303
    test_pdf = browser.get("/etiquettes/feuille-test.pdf")
    (sheet,) = pdf_reader.pages(test_pdf.content)
    assert "BIBLYO-TEST" in [code.text for code in sheet.codes]
    assert {text for _x, _y, text in sheet.texts if text.isdigit() and len(text) <= 2} == {str(n) for n in range(1, 25)}
    refused = browser.post("/etiquettes/reglages/verifiee", data={"planche": "avery-l7651", "calibration": "1"})
    assert refused.status_code == 400 and "Cochez les deux cases" in refused.text
    proofs = {"planche": "avery-l7651", "calibration": "1", "douchette": "1"}
    assert browser.post("/etiquettes/reglages/verifiee", data=proofs).status_code == 303
    assert "Certifiée sur une planche le" in browser.get("/etiquettes/reglages").text
    refused = browser.post("/etiquettes/prefixe", data={"prefixe": "CSN", "actuel": "EX-"})
    assert refused.status_code == 400 and "Cochez la case" in refused.text
    changed = browser.post("/etiquettes/prefixe", data={"prefixe": "CSN", "actuel": "EX-", "confirmer": "1"})
    assert changed.status_code == 303
    assert "CSN000001" in browser.get("/etiquettes/reglages").text
    assert client_as("volunteer").post("/etiquettes/prefixe", data={"prefixe": "AB", "actuel": "CSN",
                                                                    "confirmer": "1"}).status_code == 403  # fmt: skip
