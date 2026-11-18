# SPDX-License-Identifier: EUPL-1.2
"""Scanner universel (lot L8) : un code lu partout dans biblyO, identifié sans rien écrire.

Codes internes, inventaires, ISBN (jamais pris pour un exemplaire), cartes de lecteurs, codes inconnus ou déformés par
une douchette en QWERTY, ambiguïtés, actions selon les droits et l'état, modes (inventaire sans écriture), assistant de
la douchette, et le comptoir qui ne choisit plus d'exemplaire sur un ISBN."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue

from ..factories import make_notice
from .test_circulation_counter import _adult, _form, _scan, _state

ISBN = "9782203001190"


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def _book(settings, database, count=1, *, isbn=ISBN, title="Le Lotus bleu", series="Tintin", volume="5"):
    work = make_notice(settings, title, isbn=isbn, series=series, volume=volume, authors=("Hergé",))
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return work, ids


def _identify(browser, code="", **fields):
    response = browser.post("/scanner/identifier", data={"code": code, **fields}, headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    return response.text


def _snapshot(settings):
    return [
        _rows(settings, f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608
        for table in ("copies", "loans", "sequences", "label_batches", "audit_log")
    ]


def test_un_code_interne_ouvre_la_fiche_tout_de_suite(client_as, settings, database):
    work, (copy_id,) = _book(settings, database)
    before = _snapshot(settings)
    html = _identify(client_as("librarian"), "ex-000001")  # minuscules : comme une douchette en verrouillage majuscule
    assert "Le Lotus bleu" in html and "Hergé" in html and "Tintin" in html and "tome 5" in html
    assert "EX-000001" in html and "Disponible" in html and "Non rangé" in html
    assert f"/comptoir?exemplaire={copy_id}&amp;livre={work}" in html  # Prêter, mis en avant
    assert f"/catalogue/notices/{work}/enrichir" in html and f"/exemplaires/{copy_id}/deplacer" in html
    assert f"/etiquettes/imprimer?exemplaire={copy_id}" in html
    assert _snapshot(settings) == before  # identifier n'écrit rien


def test_les_actions_suivent_les_droits(client_as, settings, database):
    _work, (_copy_id,) = _book(settings, database)
    viewer = _identify(client_as("viewer"), "EX-000001")
    assert "Le Lotus bleu" in viewer and "/comptoir?exemplaire" not in viewer
    assert "/deplacer" not in viewer and "/enrichir" not in viewer and "/etiquettes/imprimer" not in viewer
    volunteer = _identify(client_as("volunteer"), "EX-000001")
    assert "/comptoir?exemplaire" in volunteer and "/deplacer" in volunteer
    assert "/etiquettes/imprimer" not in volunteer  # pas de labels.print
    assert client_as("viewer").get("/scanner").status_code == 200


def test_un_livre_en_pret_propose_le_retour(client_as, settings, database):
    work, (copy_id,) = _book(settings, database)
    _adult(database, card="CARTE-1")
    browser = client_as("librarian")
    html = _scan(browser, _scan(browser, None, "CARTE-1"), "EX-000001")
    assert browser.post("/comptoir/valider", data=_form(_state(html))).status_code == 200
    found = _identify(browser, "EX-000001")
    assert "Emprunté" in found and f"/comptoir/livre/{work}#retour-{copy_id}" in found
    assert f"/comptoir?exemplaire={copy_id}" not in found  # déjà sorti : pas de « Prêter »


def test_par_numero_d_inventaire(client_as, settings, database):
    _work, (copy_id,) = _book(settings, database)
    _execute(settings, "UPDATE copies SET barcode = NULL, inventory_number = '354' WHERE id = ?", copy_id)
    html = _identify(client_as("librarian"), "354")
    assert "Le Lotus bleu" in html and "Trouvé par son numéro d’inventaire" in html
    assert "Générer son code-barres" in html


def test_un_isbn_ne_designe_jamais_un_exemplaire(client_as, settings, database):
    _work, (copy_id,) = _book(settings, database)  # un seul exemplaire : il n'est pas choisi pour autant
    html = _identify(client_as("librarian"), "978-2-203-00119-0")
    assert "pas l’étiquette d’un exemplaire" in html and "Choisissez l’exemplaire" in html
    assert f'name="exemplaire" value="{copy_id}"' in html
    assert "/comptoir?exemplaire" not in html  # aucune action tant que l'exemplaire n'est pas choisi
    chosen = _identify(client_as("librarian"), exemplaire=str(copy_id))
    assert f"/comptoir?exemplaire={copy_id}" in chosen


def test_un_isbn_absent_propose_une_recherche_sans_la_lancer(client_as, settings, database):
    calls = []
    html = _identify(client_as("librarian"), "9782800152851")
    assert "pas au catalogue" in html and 'action="/sources"' in html and 'value="9782800152851"' in html
    assert "/catalogue/notices/nouvelle?isbn=9782800152851" in html
    assert calls == [] and _rows(settings, "SELECT COUNT(*) FROM enrichment_searches") == [(0,)]
    viewer = _identify(client_as("viewer"), "9782800152851")
    assert 'action="/sources"' not in viewer and "nouvelle?isbn" not in viewer
    page = client_as("librarian").get("/catalogue/notices/nouvelle?isbn=978-2-8001-5285-1").text
    assert 'value="9782800152851"' in page


def test_carte_de_lecteur_selon_les_droits(client_as, settings, database):
    patron = _adult(database, card="CARTE-7")
    html = _identify(client_as("volunteer"), "CARTE-7")
    assert "Carte de lecteur" in html and "Durand" in html and f"/comptoir?lecteur={patron}" in html
    hidden = _identify(client_as("viewer"), "CARTE-7")
    assert "Durand" not in hidden and "Code inconnu" in hidden


def test_code_inconnu_et_douchette_en_qwerty(client_as, settings, database):
    _book(settings, database)
    unknown = _identify(client_as("librarian"), "EX-999999")
    assert "Code inconnu" in unknown and "clavier américain" not in unknown
    mangled = _identify(client_as("librarian"), "EX)ààààà&")
    assert "Code inconnu" in mangled and "clavier américain" in mangled and "EX-000001" in mangled
    assert "Ce code existe" in mangled  # signalé, jamais appliqué tout seul
    assert "Le Lotus bleu" not in mangled


def test_ambiguite_entre_un_inventaire_et_un_isbn(client_as, settings, database):
    _work, (_first,) = _book(settings, database)
    _work2, (second,) = _book(settings, database, isbn="", title="Autre livre", series="", volume="")
    _execute(settings, "UPDATE copies SET inventory_number = ? WHERE id = ?", ISBN, second)
    html = _identify(client_as("librarian"), ISBN)
    assert "Plusieurs réponses" in html and "Autre livre" in html and "ISBN d’une édition" in html


def test_page_et_modes(client_as):
    page = client_as("librarian").get("/scanner").text
    for label in ("Recherche", "Comptoir de prêt", "Retours", "Inventaire", "Rangement", "Enrichissement"):
        assert label in page
    viewer = client_as("viewer").get("/scanner").text
    assert "Comptoir de prêt" not in viewer and "Rangement" not in viewer and "Inventaire" in viewer
    assert 'id="scanner"' in page and "data-scan-open" in page  # bouton permanent et fenêtre de scan
    assert client_as("librarian").get("/scanner?mode=enrichissement").status_code == 200


def test_inventaire_verifie_sans_rien_ecrire(client_as, settings, database):
    from biblyo.modules.holdings import service as holdings
    from biblyo.modules.holdings.service import LocationInput

    _work, ids = _book(settings, database, 3)
    room = holdings.create_location(database, "room", None, LocationInput("Salle BD"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble 1"), None)
    shelf = holdings.create_location(database, "shelf", furniture, LocationInput("Étagère A"), None)
    other = holdings.create_location(database, "shelf", furniture, LocationInput("Étagère B"), None)
    holdings.shelve(database, [ids[0]], shelf, user_id=None)
    holdings.shelve(database, [ids[1]], other, user_id=None)
    _execute(settings, "UPDATE copies SET status = 'in_processing' WHERE id = ?", ids[0])
    before = _snapshot(settings)
    browser = client_as("volunteer")
    headers = {"HX-Request": "true"}
    first = browser.post("/scanner/inventaire", data={"lieu": str(shelf), "code": "EX-000001"}, headers=headers).text
    assert "Compté" in first and "Présence à confirmer" in first
    again = browser.post(
        "/scanner/inventaire", data={"lieu": str(shelf), "code": "EX-000001", "vu": [str(ids[0])]}, headers=headers
    ).text
    assert "Déjà compté" in again and again.count('name="vu"') == 1
    moved = browser.post(
        "/scanner/inventaire", data={"lieu": str(shelf), "code": "EX-000002", "vu": [str(ids[0])]}, headers=headers
    ).text
    assert "Rangé ailleurs" in moved and "Étagère B" in moved and "Confirmer leur présence" in moved
    card = browser.post("/scanner/inventaire", data={"code": "9782203001190"}, headers=headers).text
    assert "choisissez l’exemplaire en main" in card and "Compter" in card
    assert _snapshot(settings) == before  # aucun prêt, aucun statut, aucune écriture


def test_assistant_de_la_douchette(client_as, settings, database):
    _book(settings, database)
    browser = client_as("volunteer")
    page = browser.get("/scanner/douchette")
    assert page.status_code == 200 and page.text.count('class="code-svg"') == 3 and "BIBLYO-TEST" in page.text

    def essai(text, suffix="enter", interval="12"):
        response = browser.post(
            "/scanner/douchette/essai",
            data={"essai": text, "suffixe": suffix, "intervalle": interval},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        return response.text

    assert "Douchette prête" in essai("BIBLYO-TEST") and "Entrée" in essai("BIBLYO-TEST")
    assert "Douchette prête" in essai("9782123456803", "tab")
    assert "Suffixe non détecté" in essai("BIBLYO-TEST", "none")
    qwerty = essai("BIBLYO)TEST")
    assert "Code non reconnu" in qwerty and "AZERTY" in qwerty
    assert "clé de contrôle" in essai("978212345680")
    assert "préfixe" in essai("]C1BIBLYO-TEST")
    assert "Code inconnu" in essai("EX-999999")
    assert "Code reconnu" in essai("EX-000001") and "Le Lotus bleu" in essai("EX-000001")
    assert "lentement" in essai("BIBLYO-TEST", interval="250")
    assert "Code non reconnu" in essai("&&&{}")
    assert "Code inconnu" in essai("BIBLYO-INCONNU")


def test_le_comptoir_ne_choisit_pas_un_exemplaire_sur_un_isbn(client_as, settings, database):
    _work, (copy_id,) = _book(settings, database)
    _adult(database, card="CARTE-1")
    browser = client_as("volunteer")
    html = _scan(browser, _scan(browser, None, "CARTE-1"), ISBN)
    assert "pas son étiquette" in html and f'value="isbn:{copy_id}"' in html
    assert ("ex", str(copy_id)) not in _state(html)
    chosen = browser.post("/comptoir/choix", data=_form([*_state(html), ("choix", f"isbn:{copy_id}")])).text
    assert ("ex", str(copy_id)) in _state(chosen)


def test_le_retour_ne_rend_pas_un_exemplaire_sur_un_isbn(client_as, settings, database):
    _work, (_copy_id,) = _book(settings, database)
    _adult(database, card="CARTE-1")
    browser = client_as("librarian")
    html = _scan(browser, _scan(browser, None, "CARTE-1"), "EX-000001")
    assert browser.post("/comptoir/valider", data=_form(_state(html))).status_code == 200
    headers = {"HX-Request": "true"}
    by_isbn = browser.post("/comptoir/retours/scan", data={"code": ISBN}, headers=headers).text
    assert "pas son étiquette" in by_isbn and 'value="EX-000001"' in by_isbn
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE returned_at IS NULL") == [(1,)]
    mangled = browser.post("/comptoir/retours/scan", data={"code": "EX)ààààà&"}, headers=headers).text
    assert "clavier américain" in mangled
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE returned_at IS NULL") == [(1,)]
    done = browser.post("/comptoir/retours/scan", data={"choix_code": "EX-000001"}, headers=headers).text
    assert "EX-000001" in done
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE returned_at IS NULL") == [(0,)]


def test_le_comptoir_signale_une_douchette_en_qwerty(client_as, settings, database):
    _book(settings, database)
    _adult(database, card="CARTE-1")
    browser = client_as("volunteer")
    html = _scan(browser, _scan(browser, None, "CARTE-1"), "EX)ààààà&")
    assert "clavier américain" in html and "EX-000001" in html
    assert not [item for item in _state(html) if item[0] == "ex"]


@pytest.mark.parametrize(
    "path",
    ["/scanner", "/scanner?mode=enrichissement", "/scanner/inventaire", "/scanner/douchette", "/etiquettes",
     "/etiquettes/selection", "/etiquettes/selection?code=avec&ordre=rayon", "/etiquettes/reglages",
     "/etiquettes/generer", "/etiquettes/imprimer?code=avec", "/etiquettes/feuille-test.pdf"],
)  # fmt: skip
def test_chaque_page_nouvelle_s_affiche(client_as, settings, database, path):
    from biblyo.modules.holdings import service as holdings
    from biblyo.modules.holdings.service import LocationInput

    _book(settings, database, 2)
    room = holdings.create_location(database, "room", None, LocationInput("Salle BD"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble 1"), None)
    holdings.create_location(database, "shelf", furniture, LocationInput("Étagère A"), None)
    for role in ("librarian", "admin"):
        response = client_as(role).get(path)
        assert response.status_code == 200, (role, path, response.text[-400:])


def test_liste_de_mise_en_service_de_la_douchette(client_as, settings, database):
    import re

    _work, (copy_id,) = _book(settings, database)
    _execute(settings, "UPDATE copies SET inventory_number = '354' WHERE id = ?", copy_id)
    browser = client_as("volunteer")
    page = browser.get("/scanner/douchette").text
    assert page.count('class="code-svg"') == 3 and "BIBLYO-INCONNU" in page
    assert page.count('class="setup-check"') == 8  # rien n'est coché d'avance
    state: list[tuple[str, str]] = []

    def essai(text: str, suffix: str = "enter") -> str:
        data: dict[str, object] = {"essai": text, "suffixe": suffix, "intervalle": "9"}
        data["fait"] = [value for name, value in state if name == "fait"]
        data.update({name: value for name, value in state if name != "fait"})
        html = browser.post("/scanner/douchette/essai", data=data, headers={"HX-Request": "true"}).text
        state[:] = re.findall(r'<input type="hidden" name="(fait|serie|serie_t)" value="([^"]*)">', html)
        return html

    def done() -> set[str]:
        return {value for name, value in state if name == "fait"}

    essai("BIBLYO)TEST")  # douchette en QWERTY : rien n'est coché, le clavier est signalé
    assert "clavier" not in done() and "code128" not in done()
    essai("BIBLYO-TEST")
    essai("9782123456803", "tab")
    assert {"code128", "isbn", "suffixe", "clavier"} <= done()
    essai("EX-000001")
    essai("354")
    assert {"interne", "inventaire"} <= done()
    last = essai("BIBLYO-INCONNU")
    assert done() == {"code128", "isbn", "interne", "inventaire", "inconnu", "serie", "suffixe", "clavier"}
    assert "Douchette prête sur ce poste" in last


def test_progression_de_la_liste() -> None:
    from biblyo.modules.scan import diagnosis

    start = diagnosis.Progress(frozenset(), 0, 0)
    ok = diagnosis.Diagnosis(diagnosis.READY, "BIBLYO-TEST", passed=frozenset({"code128"}))
    after = diagnosis.progress(start, ok, "enter", 100)
    assert after.done == {"code128", "suffixe"} and after.series == 1
    late = diagnosis.progress(after, ok, "enter", 100 + diagnosis.SERIES_WINDOW + 1)
    assert late.series == 1  # trop tard : la série recommence
    broken = diagnosis.progress(after, diagnosis.Diagnosis(diagnosis.UNREADABLE, "x"), "enter", 105)
    assert broken.series == 0
    no_suffix = diagnosis.progress(start, diagnosis.Diagnosis(diagnosis.NO_SUFFIX, "x"), "none", 100)
    assert "suffixe" not in no_suffix.done and no_suffix.series == 0

