# SPDX-License-Identifier: EUPL-1.2
"""Page Couverture (conception 17) : fichier, adresse, aperçu, remplacement, suppression, rétablissement, droits.

Les essais de la mission 7 couverts ici : 9 à 18, et l'affichage dans le catalogue et au comptoir (20). Aucune image
n'est téléchargée pour de vrai : l'adresse est servie par une fonction de remplacement."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.core.remote import RemoteImage, RemoteImageError
from biblyo.modules.catalogue import covers
from biblyo.modules.catalogue import repository as repo

from ..factories import make_notice
from ..unit.test_media import image_bytes

PNG = image_bytes("PNG", size=(240, 360), color=(20, 120, 200))
WEBP = image_bytes("WEBP", size=(240, 360), color=(200, 200, 20))
JPEG = image_bytes("JPEG", size=(240, 360), color=(180, 70, 46))


def _notice(settings, work_id):
    database = Database(settings.database_path)
    with database.session() as session:
        view = repo.load_notice(session, work_id)
    database.dispose()
    return view


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _stage_file(browser, work, edition, data=JPEG, *, rights="club", detail="", name="couverture.jpg"):
    return browser.post(
        f"/catalogue/notices/{work}/couverture/fichier",
        data={"edition_id": str(edition), "droit": rights, "droit_detail": detail, "origine": "fichier"},
        files={"couverture": (name, data, "application/octet-stream")},
    )


def _token(response) -> str:
    assert response.status_code == 303, response.text
    match = re.search(r"/couverture/apercu/([0-9a-f]{32})$", response.headers["location"])
    assert match, response.headers["location"]
    return match.group(1)


def _confirm(browser, work, token, **fields):
    return browser.post(
        f"/catalogue/notices/{work}/couverture/apercu/{token}", data={"action": "enregistrer", **fields}
    )


def _add(browser, settings, work, data=JPEG, **fields):
    edition = _notice(settings, work).primary.id
    token = _token(_stage_file(browser, work, edition, data))
    current = _notice(settings, work).primary.cover
    return _confirm(browser, work, token, attendue=str(current.media_id) if current else "", **fields)


def test_the_cover_page_offers_every_way_to_add_a_cover(client_as, settings):
    work = make_notice(settings, "Le Grand Meaulnes", authors=("Alain-Fournier",))
    page = client_as("librarian").get(f"/catalogue/notices/{work}/couverture").text
    for text in (
        "Couverture actuelle",
        "Ajouter : importer un fichier",
        "Depuis une adresse web",
        "Rechercher une couverture",
        "Historique",
        "data-dropzone",
    ):
        assert text in page
    notice = client_as("librarian").get(f"/catalogue/notices/{work}").text
    assert f"/catalogue/notices/{work}/couverture?edition=" in notice and "Ajouter une couverture" in notice


@pytest.mark.parametrize(("data", "label"), [(JPEG, "jpeg"), (PNG, "png"), (WEBP, "webp")])
def test_a_jpeg_png_or_webp_file_goes_through_a_preview_then_is_saved(client_as, settings, data, label):
    """Essais 9, 10, 11 : l'aperçu ne change rien ; la confirmation rattache l'image et la consigne."""
    work = make_notice(settings, f"Livre {label}")
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, edition, data))
    preview = browser.get(f"/catalogue/notices/{work}/couverture/apercu/{token}")
    assert preview.status_code == 200 and "Enregistrer la couverture" in preview.text
    assert _notice(settings, work).primary.cover is None  # rien n'est rattaché avant la confirmation
    response = _confirm(browser, work, token, attendue="")
    assert response.status_code == 303 and response.headers["location"].startswith(f"/catalogue/notices/{work}")
    cover = _notice(settings, work).primary.cover
    assert cover is not None and cover.origin == "upload"
    assert cover.license_note == "Photo prise pour le Club"
    assert _rows(settings, "SELECT kind, source_code, rights_note, user_id IS NOT NULL FROM cover_changes") == [
        ("upload", None, "Photo prise pour le Club", 1)
    ]
    assert browser.get(cover.path).status_code == 200 and browser.get(cover.thumbnail_path).status_code == 200


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"<html><script>alert(1)</script></html>", "n’est pas une image JPEG, PNG ou WebP valide"),
        (b"", "Ce fichier est vide."),
        (b"\xff\xd8\xff\xe0" + b"0" * 500, "n’est pas une image JPEG, PNG ou WebP valide"),
        (image_bytes("JPEG", size=(20, 20)), "trop petite"),
    ],
)
def test_an_invalid_file_has_a_clear_message_and_changes_nothing(client_as, settings, data, message):
    """Essai 12."""
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    response = _stage_file(client_as("librarian"), work, edition, data)
    assert response.status_code == 400 and message in response.text
    assert _notice(settings, work).primary.cover is None


def test_an_image_over_8_mio_is_refused(client_as, settings):
    """Essai 13 : juste au-dessus, un message clair ; bien au-dessus, la requête est coupée avant d'être lue."""
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    response = _stage_file(browser, work, edition, b"\xff\xd8" + b"0" * (8 * 1024 * 1024 + 10))
    assert response.status_code == 400 and "8 Mio" in response.text
    assert _stage_file(browser, work, edition, b"\xff\xd8" + b"0" * (9 * 1024 * 1024)).status_code == 413
    assert _notice(settings, work).primary.cover is None


def test_the_right_of_use_is_required_and_other_needs_a_detail(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    missing = _stage_file(browser, work, edition, rights="")
    assert missing.status_code == 400 and "votre droit de l’utiliser" in missing.text
    vague = _stage_file(browser, work, edition, rights="autre")
    assert vague.status_code == 400 and "Précisez l’origine" in vague.text
    token = _token(_stage_file(browser, work, edition, rights="autre", detail="scan du dossier de presse de l'éditeur"))
    _confirm(browser, work, token, attendue="")
    assert _notice(settings, work).primary.cover.license_note == (
        "Autre origine, droit d’utilisation vérifié : scan du dossier de presse de l'éditeur"
    )


def test_an_image_from_an_allowed_address(client_as, settings, app):
    """Essai 14 : téléchargement côté serveur (simulé), aperçu, confirmation ; l'adresse est conservée."""
    seen = []

    def fetch(url: str) -> RemoteImage:
        seen.append(url)
        return RemoteImage(PNG, "https://images.example.org/finale.png", "image/png")

    app.state.fetch_remote_image = fetch
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    response = browser.post(
        f"/catalogue/notices/{work}/couverture/adresse",
        data={
            "edition_id": str(edition),
            "adresse": "https://images.example.org/c.png",
            "droit": "editeur",
            "origine": "adresse",
        },
    )
    token = _token(response)
    assert seen == ["https://images.example.org/c.png"]
    assert (
        "https://images.example.org/c.png" in browser.get(f"/catalogue/notices/{work}/couverture/apercu/{token}").text
    )
    _confirm(browser, work, token, attendue="")
    cover = _notice(settings, work).primary.cover
    assert cover.origin == "url" and cover.origin_url == "https://images.example.org/finale.png"
    assert _rows(settings, "SELECT kind, reference FROM cover_changes") == [("url", "https://images.example.org/c.png")]


def test_a_dangerous_address_is_refused_with_its_message(client_as, settings, app):
    """Essai 15 : la vraie fonction de téléchargement refuse avant tout appel."""
    app.state.fetch_remote_image = None
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    for address, message in (
        ("http://images.example.org/c.png", "https://"),
        ("https://127.0.0.1/c.png", "pas une adresse IP"),
        ("https://localhost/c.png", "pas une adresse IP ni un nom local"),
        ("https://intranet:8080/c.png", "sans identifiant ni port"),
    ):
        response = browser.post(
            f"/catalogue/notices/{work}/couverture/adresse",
            data={"edition_id": str(edition), "adresse": address, "droit": "club", "origine": "adresse"},
        )
        assert response.status_code == 400 and message in response.text, address
    assert _notice(settings, work).primary.cover is None


def test_a_remote_failure_is_explained(client_as, settings, app):
    def fetch(url: str) -> RemoteImage:
        raise RemoteImageError("Le serveur est trop lent : téléchargement interrompu.")

    app.state.fetch_remote_image = fetch
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    response = client_as("librarian").post(
        f"/catalogue/notices/{work}/couverture/adresse",
        data={
            "edition_id": str(edition),
            "adresse": "https://lent.example.org/c.png",
            "droit": "club",
            "origine": "adresse",
        },
    )
    assert response.status_code == 400 and "trop lent" in response.text


def test_replacing_needs_its_box_and_never_overwrites_a_later_change(client_as, settings):
    """Essai 16 : sans la case, rien ne change ; une couverture changée depuis l'aperçu n'est pas écrasée."""
    work = make_notice(settings)
    browser = client_as("librarian")
    _add(browser, settings, work, JPEG)
    first = _notice(settings, work).primary.cover
    edition = _notice(settings, work).primary.id
    token = _token(_stage_file(browser, work, edition, PNG))
    refused = _confirm(browser, work, token, attendue=str(first.media_id))
    assert refused.status_code == 400 and "Remplacer la couverture actuelle" in refused.text
    assert _notice(settings, work).primary.cover.sha256 == first.sha256
    stale = _confirm(browser, work, token, attendue="", remplacer="1")
    assert stale.status_code == 409 and "a changé depuis l’aperçu" in stale.text
    assert _confirm(browser, work, token, attendue=str(first.media_id), remplacer="1").status_code == 303
    assert _notice(settings, work).primary.cover.sha256 != first.sha256


def test_delete_then_restore_the_previous_cover(client_as, settings):
    """Essais 17 et 18 : suppression, puis rétablissement depuis l'historique ; le dernier changement seulement."""
    work = make_notice(settings)
    browser = client_as("librarian")
    _add(browser, settings, work, JPEG)
    cover = _notice(settings, work).primary.cover
    edition = _notice(settings, work).primary.id
    assert (
        browser.post(f"/catalogue/notices/{work}/couverture/supprimer", data={"edition_id": str(edition)}).status_code
        == 303
    )
    assert _notice(settings, work).primary.cover is None
    page = browser.get(f"/catalogue/notices/{work}/couverture").text
    assert "Couverture supprimée" in page and "Rétablir la couverture précédente" in page
    assert (
        browser.post(f"/catalogue/notices/{work}/couverture/retablir", data={"edition_id": str(edition)}).status_code
        == 303
    )
    assert _notice(settings, work).primary.cover.sha256 == cover.sha256
    again = browser.post(f"/catalogue/notices/{work}/couverture/retablir", data={"edition_id": str(edition)})
    assert again.status_code == 409 and "Aucun changement de couverture à défaire" in again.text
    assert [r[0] for r in _rows(settings, "SELECT kind FROM cover_changes ORDER BY id")] == [
        "upload",
        "remove",
        "restore",
    ]


@pytest.mark.parametrize("role", ("volunteer", "viewer"))
def test_only_editors_change_a_cover_but_everyone_who_can_see_reads_its_page(client_as, settings, role):
    """Essai 17, permissions : la page se lit ; rien ne s'y modifie."""
    work = make_notice(settings)
    librarian = client_as("librarian")
    _add(librarian, settings, work, JPEG)
    edition = _notice(settings, work).primary.id
    browser = client_as(role)
    page = browser.get(f"/catalogue/notices/{work}/couverture")
    assert (
        page.status_code == 200
        and "Supprimer la couverture" not in page.text
        and "importer un fichier" not in page.text
    )
    assert _stage_file(browser, work, edition).status_code == 403
    assert (
        browser.post(f"/catalogue/notices/{work}/couverture/supprimer", data={"edition_id": str(edition)}).status_code
        == 403
    )
    assert (
        browser.post(f"/catalogue/notices/{work}/couverture/retablir", data={"edition_id": str(edition)}).status_code
        == 403
    )
    assert browser.get(f"/catalogue/notices/{work}/couverture/propositions?edition={edition}").status_code == 403
    assert _notice(settings, work).primary.cover is not None


def test_a_preview_belongs_to_its_author_and_to_its_notice(client_as, settings):
    work = make_notice(settings)
    other = make_notice(settings, "Autre livre")
    edition = _notice(settings, work).primary.id
    token = _token(_stage_file(client_as("librarian"), work, edition))
    intruder = client_as("librarian")
    assert intruder.get(f"/catalogue/notices/{work}/couverture/apercu/{token}").status_code == 404
    assert _confirm(intruder, work, token, attendue="").status_code == 404
    assert client_as("admin").get(f"/catalogue/notices/{other}/couverture/apercu/{token}").status_code == 404
    assert client_as("librarian").get(f"/catalogue/notices/{work}/couverture/apercu/../../etc").status_code == 404


def test_an_edition_of_another_work_is_refused(client_as, settings):
    work = make_notice(settings)
    other = make_notice(settings, "Autre livre")
    foreign = _notice(settings, other).primary.id
    assert _stage_file(client_as("librarian"), work, foreign).status_code == 404


def test_abandoning_a_preview_changes_nothing(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, edition))
    staged = covers.load(settings.media_dir, token, browser.user_id)
    assert staged is not None and (settings.media_dir / f"{staged.sha256}.webp").exists()
    response = browser.post(f"/catalogue/notices/{work}/couverture/apercu/{token}", data={"action": "abandonner"})
    assert response.status_code == 303 and _notice(settings, work).primary.cover is None
    assert browser.get(f"/catalogue/notices/{work}/couverture/apercu/{token}").status_code == 404
    # Arbitrage du 12 novembre 2026 : une image non confirmée n'est pas conservée
    assert not (settings.media_dir / f"{staged.sha256}.webp").exists()
    assert not (settings.media_dir / f"{staged.sha256}-v.webp").exists()


def test_an_abandoned_preview_keeps_an_image_still_in_use(client_as, settings):
    """La même image enregistrée ailleurs, ou encore en aperçu, n'est jamais supprimée par un abandon."""
    first, second = make_notice(settings, "Premier"), make_notice(settings, "Second")
    browser = client_as("librarian")
    saved = _token(_stage_file(browser, first, _notice(settings, first).primary.id))
    form = {"action": "enregistrer", "attendue": ""}
    assert browser.post(f"/catalogue/notices/{first}/couverture/apercu/{saved}", data=form).status_code == 303
    token = _token(_stage_file(browser, second, _notice(settings, second).primary.id))
    digest = covers.load(settings.media_dir, token, browser.user_id).sha256
    browser.post(f"/catalogue/notices/{second}/couverture/apercu/{token}", data={"action": "abandonner"})
    assert (settings.media_dir / f"{digest}.webp").exists() and _notice(settings, first).primary.cover is not None


def test_an_expired_preview_releases_its_image(client_as, settings):
    import time

    work = make_notice(settings)
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, _notice(settings, work).primary.id, PNG))
    digest = covers.load(settings.media_dir, token, browser.user_id).sha256
    database = Database(settings.database_path)
    try:
        in_use = covers.in_use_checker(database)
        assert covers.expire(settings.media_dir, now=time.time() + 60, in_use=in_use) == 0
        assert (settings.media_dir / f"{digest}.webp").exists()
        assert covers.expire(settings.media_dir, now=time.time() + 3 * 3600, in_use=in_use) == 1
        assert not (settings.media_dir / f"{digest}.webp").exists()
    finally:
        database.dispose()


def test_the_new_cover_shows_in_the_catalogue_lists_and_at_the_counter(client_as, settings):
    """Essai 20 : la vignette de l'édition principale change partout, sous un nouveau nom de fichier."""
    work = make_notice(settings, "Couverture visible partout")
    from biblyo.modules.catalogue import service

    database = Database(settings.database_path)
    service.add_copies(database, _notice(settings, work).primary.id, 1, "", None)
    database.dispose()
    browser = client_as("admin")
    _add(browser, settings, work, PNG)
    thumbnail = _notice(settings, work).primary.cover.thumbnail_path
    for path in ("/catalogue?q=visible+partout", "/catalogue?q=visible+partout&view=list", "/"):
        assert thumbnail in browser.get(path).text, path
    counter = browser.post("/comptoir/livres", data={"livre_q": "visible partout"})
    assert counter.status_code == 200 and thumbnail in counter.text


def test_each_edition_keeps_its_own_cover(client_as, settings):
    """Mission 4 : une couverture ne passe jamais à une autre édition de l'œuvre."""
    work = make_notice(settings)
    browser = client_as("librarian")
    assert (
        browser.post(
            f"/catalogue/notices/{work}/editions/nouvelle", data={"langue": "fre", "editeur": "Autre éditeur"}
        ).status_code
        == 303
    )
    editions = _notice(settings, work).editions
    second = next(e for e in editions if not e.is_primary)
    token = _token(_stage_file(browser, work, second.id))
    _confirm(browser, work, token, attendue="")
    after = {e.id: e.cover for e in _notice(settings, work).editions}
    assert after[second.id] is not None and after[_notice(settings, work).primary.id] is None


def test_stale_previews_expire_and_the_purge_removes_unused_files(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, edition, PNG))
    staged = covers.load(settings.media_dir, token, browser.user_id)
    assert staged is not None and (settings.media_dir / f"{staged.sha256}.webp").exists()
    from biblyo.modules.catalogue import service

    database = Database(settings.database_path)
    report = service.purge_media(database, settings.media_dir, apply=True)
    assert report.staged_files == ()  # trop récent : un aperçu en cours n'est jamais retiré
    import time

    report = service.purge_media(database, settings.media_dir, apply=False, now=time.time() + 3 * 3600)
    assert f"{staged.sha256}.webp" in report.staged_files and (settings.media_dir / f"{staged.sha256}.webp").exists()
    service.purge_media(database, settings.media_dir, apply=True, now=time.time() + 3 * 3600)
    assert not (settings.media_dir / f"{staged.sha256}.webp").exists()
    database.dispose()


def test_the_media_purge_command_simulates_unless_asked(client_as, settings, run_cli):
    import os
    import time

    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, edition, PNG))
    staged = covers.load(settings.media_dir, token, browser.user_id)
    old = time.time() - 3 * 3600
    for name in (f"{staged.sha256}.webp", f"{staged.sha256}-v.webp"):
        os.utime(settings.media_dir / name, (old, old))
    simulated = run_cli(settings, "biblyo medias nettoyer")
    assert simulated.exit_code == 0 and "à supprimer : 2" in simulated.output and "--appliquer" in simulated.output
    assert (settings.media_dir / f"{staged.sha256}.webp").exists()
    applied = run_cli(settings, "biblyo medias nettoyer --appliquer")
    assert applied.exit_code == 0 and "supprimé(s) : 2" in applied.output
    assert not (settings.media_dir / f"{staged.sha256}.webp").exists()


def test_an_image_can_be_withdrawn_before_90_days_but_never_a_current_cover(client_as, settings, run_cli):
    """Arbitrage du 12 novembre 2026 : retrait anticipé possible (droits contestés), jamais d'une image affichée."""
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, edition))
    form = {"action": "enregistrer", "attendue": ""}
    assert browser.post(f"/catalogue/notices/{work}/couverture/apercu/{token}", data=form).status_code == 303
    cover = _notice(settings, work).primary.cover
    digest = cover.sha256

    refused = run_cli(settings, f"biblyo medias retirer {digest} --motif essai --appliquer")
    assert refused.exit_code != 0 and "couverture actuelle" in refused.output
    assert (settings.media_dir / f"{digest}.webp").exists()

    removed = browser.post(
        f"/catalogue/notices/{work}/couverture/supprimer", data={"edition_id": str(edition)}
    )
    assert removed.status_code == 303 and _notice(settings, work).primary.cover is None
    simulated = run_cli(settings, f"biblyo medias retirer {digest} --motif contestation")
    assert simulated.exit_code == 0 and "Simulation" in simulated.output
    assert (settings.media_dir / f"{digest}.webp").exists()
    applied = run_cli(settings, f"biblyo medias retirer {digest} --motif contestation --appliquer")
    assert applied.exit_code == 0 and "Image retirée" in applied.output
    assert not (settings.media_dir / f"{digest}.webp").exists()
    assert not (settings.media_dir / f"{digest}-v.webp").exists()
    restore = browser.post(f"/catalogue/notices/{work}/couverture/retablir", data={"edition_id": str(edition)})
    assert _notice(settings, work).primary.cover is None  # l'image retirée ne revient pas
    assert restore.status_code in (303, 409)
    connection = sqlite3.connect(settings.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'media.withdrawn'").fetchone() == (1,)
    finally:
        connection.close()


def test_the_purge_simulation_changes_nothing_at_all(client_as, settings, run_cli):
    """En simulation, même les notes d'aperçus expirés restent (défaut relevé le 12 novembre 2026)."""
    import os
    import time

    work = make_notice(settings)
    browser = client_as("librarian")
    token = _token(_stage_file(browser, work, _notice(settings, work).primary.id))
    note = settings.media_dir / covers.STAGING_DIR / f"{token}.json"
    old = time.time() - 3 * 3600
    os.utime(note, (old, old))
    assert run_cli(settings, "biblyo medias nettoyer").exit_code == 0
    assert note.exists()


@pytest.mark.parametrize(("role", "manages"), (("librarian", True), ("volunteer", False), ("viewer", False)))
def test_a_click_on_the_cover_manages_it_for_editors_and_only_zooms_otherwise(client_as, settings, role, manages):
    """Arbitrage du 12 novembre 2026 : qui peut modifier ouvre la gestion ; qui consulte ne fait qu'agrandir."""
    work = make_notice(settings)
    _add(client_as("librarian"), settings, work, JPEG)
    page = client_as(role).get(f"/catalogue/notices/{work}").text
    assert ('class="couv-link"' in page) is manages
    assert (f"/catalogue/notices/{work}/couverture?edition=" in page) is manages
    assert ('class="notice-zoom plain"' in page) is not manages
    assert 'data-open-dialog="zoom-couverture"' in page  # l'agrandissement reste pour tous
