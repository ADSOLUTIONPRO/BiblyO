# SPDX-License-Identifier: EUPL-1.2
"""Assemblage de l'application : `create_app` lit le registre et monte les modules."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from biblyo import __version__
from biblyo.config import Settings
from biblyo.core import health
from biblyo.core.db import Database
from biblyo.core.errors import install_error_handlers
from biblyo.core.logs import hide_private_queries
from biblyo.core.maintenance import MaintenanceMiddleware
from biblyo.core.modules import Module, order_modules
from biblyo.core.permissions import assert_routes_protected
from biblyo.core.schema_state import SchemaMonitor
from biblyo.core.security import install
from biblyo.core.security.csrf import csrf_protect
from biblyo.core.security.headers import SecurityHeadersMiddleware
from biblyo.core.security.limits import BodyLimitMiddleware
from biblyo.core.security.ratelimit import FailureLimiter
from biblyo.core.templating import STATIC_DIR, Templates
from biblyo.core.web import finish_page, page_context
from biblyo.modules.registry import all_modules

CORE_PUBLIC_PATHS = ("/sante",)
LOGIN_FAILURES_PER_IDENTIFIER = 20  # pour une adresse et un identifiant : une erreur ne gêne pas les autres comptes
LOGIN_FAILURES_PER_ADDRESS = 100  # plafond contre les essais en masse. Plusieurs postes peuvent partager une adresse
INSTALL_FAILURES_PER_ADDRESS = 10
FAILURE_WINDOW_SECONDS = 300.0
SEARCHES_PER_MINUTE = 90  # recherches de lecteurs par compte : large pour une saisie, bloquant pour une énumération


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    if app.state.schema.current().ok:
        await run_in_threadpool(install.announce_token_if_needed, app.state.database)
    yield


def create_app(settings: Settings | None = None, modules: list[Module] | None = None) -> FastAPI:
    settings = settings or Settings()
    ordered = order_modules(modules if modules is not None else all_modules())
    app = FastAPI(
        title="biblyO",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.modules = ordered
    app.state.database = Database(settings.database_path)
    app.state.schema = SchemaMonitor(settings, ttl=0.0 if settings.env == "test" else 2.0)
    app.state.login_limiter = FailureLimiter(LOGIN_FAILURES_PER_IDENTIFIER, FAILURE_WINDOW_SECONDS)
    app.state.login_address_limiter = FailureLimiter(LOGIN_FAILURES_PER_ADDRESS, FAILURE_WINDOW_SECONDS)
    app.state.install_limiter = FailureLimiter(INSTALL_FAILURES_PER_ADDRESS, FAILURE_WINDOW_SECONDS)
    app.state.search_limiter = FailureLimiter(SEARCHES_PER_MINUTE, 60.0)
    app.state.search_alerts = FailureLimiter(1, 60.0)

    hide_private_queries()
    templates = Templates(m.template_dir for m in ordered if m.template_dir is not None)
    templates.context_processors.append(page_context)
    templates.response_hooks.append(finish_page)
    app.state.templates = templates
    install_error_handlers(app, templates)
    app.add_middleware(MaintenanceMiddleware, monitor=app.state.schema, env=templates.env)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # le dernier ajouté enveloppe les autres : aucune réponse n'y échappe

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    for module in ordered:
        if module.router is not None:
            app.include_router(module.router)
    assert_routes_protected(
        app,
        public_paths=[*CORE_PUBLIC_PATHS, *(path for m in ordered for path in m.public_paths)],
        known_permissions=[p.code for m in ordered for p in m.permissions],
    )
    return app
