# SPDX-License-Identifier: EUPL-1.2
"""Images : le format se lit dans le contenu, les métadonnées disparaissent, aucun nom n'entre dans un chemin."""

from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image, PngImagePlugin

from biblyo.core.media import (
    MAX_UPLOAD_BYTES,
    STORED_SIDE,
    THUMBNAIL_SIDE,
    InvalidImage,
    resolve_media_path,
    store_image,
)


def image_bytes(fmt="JPEG", size=(200, 300), color=(180, 70, 46), **options) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt, **options)
    return buffer.getvalue()


def giant_png(width: int, height: int) -> bytes:
    """En-tête annonçant une image immense, sans les données : une bombe n'a pas besoin d'être lourde."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0" * 10)) + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_accepted_formats_are_reencoded_as_webp(tmp_path, fmt):
    stored = store_image(image_bytes(fmt), tmp_path)
    assert stored.mime == "image/webp"
    assert (stored.width, stored.height) == (200, 300)
    with Image.open(tmp_path / f"{stored.sha256}.webp") as result:
        assert result.format == "WEBP"
    with Image.open(tmp_path / f"{stored.sha256}-v.webp") as thumbnail:
        assert thumbnail.format == "WEBP"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"<html><body>Pas une image</body></html>",
        b"GIF89a" + b"\0" * 100,
        b"\xff\xd8\xff\xe0" + b"\0" * 20,
        b"\x89PNG\r\n\x1a\n" + b"\0" * 20,
        b"MZ\x90\x00" + b"\0" * 100,
    ],
    ids=["vide", "svg", "html", "gif", "jpeg-faux", "png-faux", "executable"],
)
def test_files_that_are_not_images_are_refused(tmp_path, data):
    with pytest.raises(InvalidImage):
        store_image(data, tmp_path)
    assert not list(tmp_path.iterdir())


def test_a_real_gif_is_refused(tmp_path):
    with pytest.raises(InvalidImage, match="JPEG, PNG ou WebP"):
        store_image(image_bytes("GIF"), tmp_path)


def test_truncated_image_is_refused(tmp_path):
    data = image_bytes("JPEG", size=(800, 800))
    with pytest.raises(InvalidImage, match="abîmé ou incomplet"):
        store_image(data[: len(data) // 2], tmp_path)


def test_decompression_bomb_is_refused_before_decoding(tmp_path):
    with pytest.raises(InvalidImage):
        store_image(giant_png(9000, 9000), tmp_path)
    with pytest.raises(InvalidImage):
        store_image(giant_png(60000, 60000), tmp_path)


def test_too_wide_image_is_refused(tmp_path):
    with pytest.raises(InvalidImage, match="trop grande"):
        store_image(giant_png(20000, 100), tmp_path)


def test_too_small_image_is_refused(tmp_path):
    with pytest.raises(InvalidImage, match="trop petite"):
        store_image(image_bytes("PNG", size=(8, 8)), tmp_path)


def test_too_heavy_file_is_refused(tmp_path):
    with pytest.raises(InvalidImage, match="8 Mio"):
        store_image(b"\xff\xd8" + b"\0" * MAX_UPLOAD_BYTES, tmp_path)


def test_metadata_is_removed(tmp_path):
    exif = Image.Exif()
    exif[0x010E] = "Secret dans la description"
    exif[0x013B] = "Nom du photographe"
    source = image_bytes("JPEG", exif=exif.tobytes(), icc_profile=b"profil-confidentiel" * 8)
    with Image.open(io.BytesIO(source)) as original:
        assert original.getexif()[0x013B] == "Nom du photographe"
    stored = store_image(source, tmp_path)
    for name in (f"{stored.sha256}.webp", f"{stored.sha256}-v.webp"):
        raw = (tmp_path / name).read_bytes()
        assert b"Secret" not in raw and b"photographe" not in raw and b"profil-confidentiel" not in raw
        with Image.open(tmp_path / name) as result:
            assert len(result.getexif()) == 0
            assert "icc_profile" not in result.info


def test_png_text_chunks_are_removed(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "Texte caché dans le PNG")
    stored = store_image(image_bytes("PNG", pnginfo=info), tmp_path)
    assert b"cach" not in (tmp_path / f"{stored.sha256}.webp").read_bytes()


def test_orientation_is_applied_before_removing_exif(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # à faire pivoter de 90 degrés
    stored = store_image(image_bytes("JPEG", size=(200, 300), exif=exif.tobytes()), tmp_path)
    assert (stored.width, stored.height) == (300, 200)


def test_large_image_is_reduced_and_gets_a_thumbnail(tmp_path):
    stored = store_image(image_bytes("PNG", size=(3200, 4800)), tmp_path)
    assert max(stored.width, stored.height) == STORED_SIDE
    with Image.open(tmp_path / f"{stored.sha256}-v.webp") as thumbnail:
        assert max(thumbnail.size) == THUMBNAIL_SIDE


def test_small_image_is_never_enlarged(tmp_path):
    stored = store_image(image_bytes("PNG", size=(64, 96)), tmp_path)
    assert (stored.width, stored.height) == (64, 96)


def test_transparency_is_kept(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 96), (0, 0, 0, 0)).save(buffer, "PNG")
    stored = store_image(buffer.getvalue(), tmp_path)
    with Image.open(tmp_path / f"{stored.sha256}.webp") as result:
        assert result.mode == "RGBA"


def test_same_image_gives_the_same_file(tmp_path):
    data = image_bytes()
    assert store_image(data, tmp_path).sha256 == store_image(data, tmp_path).sha256
    assert len(list(tmp_path.iterdir())) == 2
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".envoi-")]


@pytest.mark.parametrize(
    "name",
    [
        "../biblyo.sqlite",
        "biblyo.sqlite",
        "..%2F..%2Fetc%2Fpasswd",
        "a" * 64 + ".png",
        "a" * 63 + ".webp",
        ("g" * 64) + ".webp",
        ("a" * 64) + ".webp/../../x",
        ("a" * 64) + "-x.webp",
        "",
    ],
)
def test_only_digest_names_are_resolved(tmp_path, name):
    (tmp_path / "biblyo.sqlite").write_text("secret")
    assert resolve_media_path(tmp_path, name) is None


def test_existing_digest_name_is_resolved(tmp_path):
    stored = store_image(image_bytes(), tmp_path)
    assert resolve_media_path(tmp_path, f"{stored.sha256}.webp") == tmp_path / f"{stored.sha256}.webp"
    assert resolve_media_path(tmp_path, f"{stored.sha256}-v.webp") == tmp_path / f"{stored.sha256}-v.webp"
    assert resolve_media_path(tmp_path, "b" * 64 + ".webp") is None
