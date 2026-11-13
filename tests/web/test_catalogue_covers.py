# SPDX-License-Identifier: EUPL-1.2
"""Couvertures de bout en bout : envoi, droits, plafond limité à sa route, sauvegarde et restauration."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from biblyo.core.backup import create_backup, restore_backup, verify_backup
from biblyo.core.db import Database
from biblyo.modules.catalogue import repository as repo

from ..factories import make_notice
from ..unit.test_media import image_bytes

ONE_MIO = 1024 * 1024


def _notice(settings, work_id):
    database = Database(settings.database_path)
    with database.session() as session:
        view = repo.load_notice(session, work_id)
    database.dispose()
    return view


def _upload(browser, edition, data=None, filename="couverture.jpg", content_type="image/jpeg"):
    files = {"couverture": (filename, image_bytes() if data is None else data, content_type)}
    return browser.post(f"/catalogue/editions/{edition}/couverture", files=files)


def test_upload_then_display(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("admin")
    assert _upload(browser, edition).status_code == 303
    cover = _notice(settings, work).primary.cover
    assert cover is not None
    page = browser.get(f"/catalogue/notices/{work}").text
    assert cover.path in page
    assert f'alt="Couverture de {_notice(settings, work).title}"' in page
    image = browser.get(cover.path)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/webp"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert "private" in image.headers["cache-control"]
    assert browser.get(cover.thumbnail_path).status_code == 200


def test_a_notice_without_cover_shows_the_replacement(client_as, settings):
    work = make_notice(settings, "Le Lotus bleu", series="Tintin", volume="5", authors=("Hergé",))
    page = client_as("admin").get(f"/catalogue/notices/{work}").text
    assert 'class="cover cv-' in page
    assert '<img class="cover-img"' not in page


def test_cover_counts_for_completeness_and_can_be_removed(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("admin")
    before = _notice(settings, work).completeness
    _upload(browser, edition)
    assert _notice(settings, work).completeness == before + 14
    assert browser.post(f"/catalogue/editions/{edition}/couverture/retirer").status_code == 303
    assert _notice(settings, work).primary.cover is None
    assert _notice(settings, work).completeness == before


@pytest.mark.parametrize(
    ("data", "filename", "content_type"),
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image.svg", "image/svg+xml"),
        (b"<html><script>alert(1)</script></html>", "photo.jpg", "image/jpeg"),
        (b"MZ\x90\x00", "virus.png", "image/png"),
        (image_bytes("GIF"), "anime.jpg", "image/jpeg"),
    ],
    ids=["svg", "html-renomme", "executable-renomme", "gif-renomme"],
)
def test_invalid_files_are_refused_whatever_they_claim(client_as, settings, data, filename, content_type):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    page = _upload(client_as("admin"), edition, data, filename, content_type)
    assert page.status_code == 400
    assert "pas une image JPEG, PNG ou WebP valide" in page.text
    assert _notice(settings, work).primary.cover is None
    assert not settings.media_dir.exists() or not list(settings.media_dir.iterdir())


def test_a_hostile_filename_changes_nothing(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    assert _upload(client_as("admin"), edition, filename="../../../../tmp/piege.jpg").status_code == 303
    names = [path.name for path in settings.media_dir.iterdir()]
    assert all(len(name.split(".")[0].replace("-v", "")) == 64 for name in names)
    assert not (settings.data_dir.parent / "piege.jpg").exists()


def test_file_over_8_mio_is_refused(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    response = _upload(client_as("admin"), edition, b"\xff\xd8" + b"\0" * (9 * ONE_MIO))
    assert response.status_code == 413
    assert _notice(settings, work).primary.cover is None


def test_file_just_over_8_mio_gets_a_clear_message(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    page = _upload(client_as("admin"), edition, b"\xff\xd8" + b"\0" * (8 * ONE_MIO))
    assert page.status_code == 400
    assert "dépasse 8 Mio" in page.text


def test_empty_form_is_explained(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    page = client_as("admin").post(f"/catalogue/editions/{edition}/couverture", data={"x": "y"})
    assert page.status_code == 400
    assert "Choisissez un fichier" in page.text


def test_the_larger_limit_applies_to_this_route_only(client_as, settings):
    work = make_notice(settings)
    browser = client_as("admin")
    body = {"titre": "x" * (2 * ONE_MIO)}
    assert browser.post("/catalogue/notices", data=body).status_code == 413
    assert browser.post(f"/catalogue/notices/{work}/sections/contenu", data=body).status_code == 413
    assert browser.post("/connexion", data=body).status_code == 413


@pytest.mark.parametrize("role", ("volunteer", "viewer"))
def test_only_editors_can_change_a_cover(client_as, settings, role):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as(role)
    assert _upload(browser, edition).status_code == 403
    assert browser.post(f"/catalogue/editions/{edition}/couverture/retirer").status_code == 403
    assert _notice(settings, work).primary.cover is None


def test_upload_requires_csrf(client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    browser = client_as("admin")
    del browser.headers["X-CSRF-Token"]
    assert _upload(browser, edition).status_code == 403


def test_media_route_requires_a_session_and_the_permission(client, client_as, settings):
    work = make_notice(settings)
    edition = _notice(settings, work).primary.id
    _upload(client_as("admin"), edition)
    path = _notice(settings, work).primary.cover.path
    anonymous = client.get(path)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/connexion")
    assert client_as("viewer").get(path).status_code == 200


@pytest.mark.parametrize(
    "name",
    ["biblyo.sqlite", "..%2Fbiblyo.sqlite", "%2e%2e%2fbiblyo.sqlite", "a" * 64 + ".webp", "a" * 64 + ".png"],
)
def test_media_route_serves_nothing_else(client_as, name):
    assert client_as("admin").get(f"/medias/{name}").status_code == 404


def test_same_image_on_two_editions_shares_one_file(client_as, settings):
    first = _notice(settings, make_notice(settings, "A")).primary.id
    second = _notice(settings, make_notice(settings, "B")).primary.id
    browser = client_as("admin")
    _upload(browser, first)
    _upload(browser, second)
    connection = sqlite3.connect(settings.database_path)
    assert connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 1
    connection.close()


def _digests(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.iterdir())}


def test_backup_and_restore_bring_covers_back_identically(client_as, settings):
    work = make_notice(settings, "Avec couverture")
    edition = _notice(settings, work).primary.id
    _upload(client_as("admin"), edition)
    expected = _digests(settings.media_dir)
    assert len(expected) == 2

    info = create_backup(settings)
    manifest = verify_backup(info.path)
    assert {f"media/{name}" for name in expected} <= set(manifest["files"])

    for path in settings.media_dir.iterdir():
        path.write_bytes(b"abime")
    restore_backup(settings, info.path)
    assert _digests(settings.media_dir) == expected
    assert _notice(settings, work).primary.cover is not None


def test_backup_with_a_damaged_cover_is_detected(client_as, settings, tmp_path):
    import tarfile

    work = make_notice(settings)
    _upload(client_as("admin"), _notice(settings, work).primary.id)
    info = create_backup(settings)
    damaged = tmp_path / info.path.name
    with tarfile.open(info.path) as source, tarfile.open(damaged, "w:gz") as target:
        for member in source.getmembers():
            content = source.extractfile(member).read()
            if member.name.startswith("media/"):
                content = b"abime"
                member.size = len(content)
            import io

            target.addfile(member, io.BytesIO(content))
    with pytest.raises(Exception, match=r"(?i)empreinte|intégr|taille|différ"):
        verify_backup(damaged)
