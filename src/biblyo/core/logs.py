# SPDX-License-Identifier: EUPL-1.2
"""Journal structuré : des champs fixes, puis le message, sur une ligne, en heure UTC."""

from __future__ import annotations

import logging
import sys
import time

HANDLER_NAME = "biblyo-console"


class StructuredFormatter(logging.Formatter):
    converter = time.gmtime  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__(
            fmt="at=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
        )


class PrivateQueryFilter(logging.Filter):
    """Une recherche de lecteur peut porter un courriel ou un téléphone : le journal n'en garde que le chemin."""

    PREFIXES = ("/lecteurs", "/foyers")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2]
            if "?" in path and path.startswith(self.PREFIXES):
                record.args = (args[0], args[1], path.split("?", 1)[0], args[3], args[4])
        return True


def hide_private_queries() -> None:
    """À appeler après la configuration d'uvicorn. Sans effet si le filtre est déjà posé."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, PrivateQueryFilter) for item in logger.filters):
        logger.addFilter(PrivateQueryFilter())


def configure_logging(level: str = "INFO") -> None:
    """Sortie sur le flux d'erreur, lue par `docker logs`. Sans effet si elle est déjà en place."""
    logger = logging.getLogger("biblyo")
    logger.setLevel(level.upper())
    if any(handler.get_name() == HANDLER_NAME for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)
