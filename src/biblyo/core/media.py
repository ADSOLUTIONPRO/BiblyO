# SPDX-License-Identifier: EUPL-1.2
"""Images téléversées : rien de ce que le navigateur annonce n'est cru.

Le format se lit dans le contenu, jamais dans le nom ni le type déclaré. La taille et les dimensions sont bornées avant
tout décodage. Chaque image est décodée puis réencodée : un fichier piégé ne survit pas, et les métadonnées (position
GPS, auteur, profil) non plus. Le fichier est nommé par son empreinte : aucune saisie n'entre dans un chemin.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
MIN_SIDE = 32
MAX_SIDE = 12_000
STORED_SIDE = 1600
THUMBNAIL_SIDE = 320
QUALITY = 85
ACCEPTED_FORMATS = ("JPEG", "PNG", "WEBP")
STORED_MIME = "image/webp"
_FILE_NAME = re.compile(r"^(?P<digest>[0-9a-f]{64})(?P<thumbnail>-v)?\.webp$")


class InvalidImage(BusinessError):
    """Fichier refusé : le message s'adresse à la personne qui l'a envoyé."""


@dataclass(frozen=True)
class StoredImage:
    sha256: str
    bytes: int
    width: int
    height: int
    mime: str = STORED_MIME


def _open(data: bytes) -> Image.Image:
    if not data:
        raise InvalidImage(_("Ce fichier est vide."))
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidImage(_("Cette image dépasse 8 Mio."))
    try:
        image = Image.open(io.BytesIO(data), formats=ACCEPTED_FORMATS)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidImage(_("Ce fichier n’est pas une image JPEG, PNG ou WebP valide.")) from exc
    width, height = image.size  # lu dans l'en-tête, avant tout décodage
    if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
        raise InvalidImage(_("Cette image est trop grande : 40 millions de pixels au plus."))
    if min(width, height) < MIN_SIDE:
        raise InvalidImage(_("Cette image est trop petite : 32 pixels de côté au moins."))
    return image


def _decode(image: Image.Image) -> Image.Image:
    try:
        image.load()
        upright = ImageOps.exif_transpose(image)  # l'orientation est appliquée, puis l'EXIF disparaît
        return upright.convert(
            "RGBA" if upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info else "RGB"
        )
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InvalidImage(_("Ce fichier est abîmé ou incomplet.")) from exc


def _encode(image: Image.Image, side: int) -> bytes:
    resized = image.copy()
    resized.thumbnail((side, side), Image.Resampling.LANCZOS)
    resized.info.clear()
    buffer = io.BytesIO()
    resized.save(buffer, "WEBP", quality=QUALITY, method=4)
    return buffer.getvalue()


def _write_atomically(target: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".envoi-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def store_image(data: bytes, directory: Path) -> StoredImage:
    """Contrôle, réencode et range l'image. Une image identique réutilise le même fichier."""
    image = _decode(_open(data))
    main = _encode(image, STORED_SIDE)
    digest = hashlib.sha256(main).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    _write_atomically(directory / f"{digest}.webp", main)
    _write_atomically(directory / f"{digest}-v.webp", _encode(image, THUMBNAIL_SIDE))
    stored = Image.open(io.BytesIO(main))
    return StoredImage(digest, len(main), stored.width, stored.height)


def preview_webp(data: bytes, side: int = THUMBNAIL_SIDE) -> bytes:
    """Contrôle et réencode une image sans la ranger : vignette montrée avant toute décision."""
    return _encode(_decode(_open(data)), side)


def resolve_media_path(directory: Path, name: str) -> Path | None:
    """Chemin d'un fichier d'après son nom public. Tout autre nom est refusé : aucun chemin arbitraire."""
    if not _FILE_NAME.match(name):
        return None
    path = directory / name
    return path if path.is_file() else None
