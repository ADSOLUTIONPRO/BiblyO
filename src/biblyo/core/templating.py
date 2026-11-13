# SPDX-License-Identifier: EUPL-1.2
"""Gabarits Jinja : composants partagés, dossiers des modules, filtres français."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined, select_autoescape

from biblyo import __copyright__, __version__
from biblyo.core import i18n

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATES_DIR = PACKAGE_DIR / "templates"

ContextProcessor = Callable[[Request], Mapping[str, Any]]
ResponseHook = Callable[[Request, HTMLResponse], None]


class Templates:
    def __init__(self, module_dirs: Iterable[Path] = ()) -> None:
        paths = [TEMPLATES_DIR, *module_dirs]
        self.env = Environment(
            loader=ChoiceLoader([FileSystemLoader(str(p)) for p in paths]),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            extensions=["jinja2.ext.i18n"],
        )
        self.env.install_gettext_callables(i18n.gettext, i18n.ngettext, newstyle=True)  # type: ignore[attr-defined]
        self.env.filters.update(
            typo=i18n.typo,
            date_long=i18n.date_long,
            date_with_weekday=i18n.date_with_weekday,
            datetime_short=i18n.datetime_short,
            number=i18n.number,
        )
        self.env.globals.update(static_url=static_url, app_version=__version__, app_copyright=__copyright__)
        self.context_processors: list[ContextProcessor] = []
        self.response_hooks: list[ResponseHook] = []

    def render_text(self, request: Request, template: str, context: Mapping[str, Any] | None = None) -> str:
        data: dict[str, Any] = {"request": request}
        for processor in self.context_processors:
            data.update(processor(request))
        data.update(context or {})
        return self.env.get_template(template).render(data)

    def render(
        self,
        request: Request,
        template: str,
        context: Mapping[str, Any] | None = None,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> HTMLResponse:
        response = HTMLResponse(self.render_text(request, template, context), status_code, headers=headers)
        for hook in self.response_hooks:
            hook(request, response)
        return response


_static_digests: dict[str, str] = {}


def static_url(path: str) -> str:
    """URL d'un fichier statique avec son empreinte, pour une mise en cache longue et sûre."""
    digest = _static_digests.get(path)
    if digest is None:
        target = STATIC_DIR / path
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:10] if target.is_file() else "absent"
        _static_digests[path] = digest
    return f"/static/{path}?v={digest}"
