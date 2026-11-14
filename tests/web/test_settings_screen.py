# SPDX-License-Identifier: EUPL-1.2
"""Écran « Paramètres » (lot L6, finalisation) : contrôle réel des réglages, identité, logo, sauvegardes, droits."""

from __future__ import annotations

import io
import sqlite3

from PIL import Image


def _rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 80, 40)).save(out, "PNG")
    return out.getvalue()


def test_access_follows_the_rights(client, client_as):
    assert client.get("/parametres").status_code in (302, 303)
    assert client_as("viewer").get("/parametres").status_code == 403
    assert client_as("volunteer").get("/parametres").status_code == 403
    librarian = client_as("librarian")
    page = librarian.get("/parametres").text
    assert "Prêts et lecteurs" in page and "Catalogue et affichage" in page and "Ma bibliothèque" in page
    assert "Contrôle des réglages" in librarian.get("/parametres/avance").text
    assert "Règles de prêt" in librarian.get("/parametres/prets").text
    backups = librarian.get("/parametres/sauvegardes").text
    assert "Créer une sauvegarde maintenant" not in backups  # réservé à l'administration
    assert librarian.post("/parametres/sauvegardes").status_code == 403
    assert "Créer une sauvegarde maintenant" in client_as("admin").get("/parametres/sauvegardes").text


def test_the_identity_is_saved_validated_and_shown_everywhere(client_as, settings):
    browser = client_as("librarian")
    bad = browser.post("/parametres/identite", data={"nom": "X", "courriel": "pas-une-adresse"})
    assert bad.status_code == 400 and "2 à 120 caractères" in bad.text and "Adresse de courriel invalide" in bad.text
    ok = browser.post(
        "/parametres/identite",
        data={"nom": "Club Essai", "courriel": "contact@exemple.invalid", "telephone": "01 02", "adresse": "1 rue"},
    )
    assert ok.status_code == 303
    assert "Club Essai" in browser.get("/").text
    assert _rows(settings, "SELECT value FROM settings WHERE key = 'library.contact_email'") == [
        ('"contact@exemple.invalid"',)
    ]
    assert _rows(settings, "SELECT action FROM audit_log WHERE action = 'settings.identity'")


def test_the_logo_is_checked_kept_in_the_database_served_and_removed(client, client_as, settings):
    browser = client_as("librarian")
    refused = browser.post("/parametres/logo", files={"logo": ("x.png", b"pas une image", "image/png")})
    assert refused.status_code == 400
    assert browser.post("/parametres/logo", files={"logo": ("logo.png", _png(), "image/png")}).status_code == 303
    assert _rows(settings, "SELECT COUNT(*) FROM settings WHERE key = 'library.logo'") == [(1,)]
    assert "/identite/logo.webp?v=" in browser.get("/").text
    served = client.get("/identite/logo.webp")  # public, comme le nom de la bibliothèque
    assert served.status_code == 200 and served.headers["content-type"] == "image/webp"
    assert served.content[:4] == b"RIFF"  # réencodé en WebP
    assert client.get("/identite/logo.webp", headers={"If-None-Match": served.headers["etag"]}).status_code == 304
    assert browser.post("/parametres/logo/retirer").status_code == 303
    assert client.get("/identite/logo.webp").status_code == 404


def test_a_backup_is_created_verified_and_audited_by_an_administrator(client_as, settings):
    admin = client_as("admin")
    assert admin.post("/parametres/sauvegardes").status_code == 303
    archives = sorted(settings.backup_dir.glob("biblyo-*.tar.gz"))
    assert len(archives) == 1
    page = admin.post("/parametres/sauvegardes/verifier", data={"archive": archives[0].name})
    assert page.status_code == 200 and "vérifiée : archive intacte" in page.text
    assert admin.post("/parametres/sauvegardes/verifier", data={"archive": "../../etc/passwd"}).status_code == 404
    assert _rows(settings, "SELECT action FROM audit_log WHERE action = 'backup.create'") == [("backup.create",)]


def test_the_rights_table_reads_the_roles(client_as):
    page = client_as("admin").get("/parametres/utilisateurs").text
    table = page[page.index("Ce que chaque rôle peut faire") :]
    assert "Accepter métadonnées et couvertures" in table and "Bibliothécaire" in table and "Bénévole" in table


def test_the_settings_are_organised_in_rubrics_with_a_shared_navigation(client_as):
    admin = client_as("admin")
    page = admin.get("/parametres").text
    for label in (
        "Ma bibliothèque",
        "Catalogue et affichage",
        "Enrichissement et couvertures",
        "Prêts et lecteurs",
        "Sauvegardes",
        "Utilisateurs et permissions",
        "Paramètres avancés",
    ):
        assert label in page
    for path in ("bibliotheque", "catalogue", "enrichissement", "prets", "sauvegardes", "utilisateurs", "avance"):
        rubric = admin.get(f"/parametres/{path}")
        assert rubric.status_code == 200, path
        assert 'aria-current="page"' in rubric.text and "Rubriques des paramètres" in rubric.text
    assert admin.get("/parametres/inconnue").status_code == 404


def test_a_rubric_is_refused_without_its_rights(client_as):
    volunteer = client_as("volunteer")
    for path in ("bibliotheque", "avance", "sauvegardes", "enrichissement"):
        assert volunteer.get(f"/parametres/{path}").status_code == 403, path


def test_the_sources_rubric_keeps_technical_settings_folded(client_as):
    admin = client_as("admin")
    old = admin.get("/admin/sources")
    assert old.status_code == 303 and old.headers["location"] == "/parametres/enrichissement"
    page = admin.get("/parametres/enrichissement").text
    assert "Ordre de recherche" in page and "Tester" in page and "Proposer ses couvertures" in page
    assert page.index("Réglages avancés") < page.index("Intervalle entre deux appels (ms)")
    assert "Droits et conditions de réutilisation" in page
