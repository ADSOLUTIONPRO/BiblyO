# SPDX-License-Identifier: EUPL-1.2
"""Réglages d'environnement. Ce qui se règle dans l'interface vit dans la table `settings`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIBLYO_", extra="ignore")

    env: Literal["development", "test", "production"] = "production"
    data_dir: Path = Path("/data")
    backup_dir: Path = Path("/backups")
    base_url: str = "http://localhost:8000"
    trusted_proxies: str = ""
    session_days: int = Field(default=14, ge=1, le=365)
    source_url: str = ""
    log_level: str = "INFO"
    show_planned_modules: bool | None = None
    # Hors production seulement : réponses enregistrées servies à la place des sources (essais de bout en bout)
    enrichment_fixtures: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "biblyo.sqlite"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def migration_lock_path(self) -> Path:
        return self.data_dir / ".migration.lock"

    @property
    def migration_failure_path(self) -> Path:
        return self.data_dir / ".migration-failed.json"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.base_url.lower().startswith("https://")

    @property
    def planned_modules_visible(self) -> bool:
        if self.show_planned_modules is not None:
            return self.show_planned_modules
        return not self.is_production

    @property
    def trusted_proxy_list(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.trusted_proxies.split(",") if p.strip())
