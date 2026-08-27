"""Validated application settings loaded from environment variables."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from pydantic import AnyHttpUrl, Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _ConfigWithMainOption(Protocol):
    def get_main_option(self, name: str) -> str | None:
        ...


class _ConfigWithSetMainOption(Protocol):
    def set_main_option(self, name: str, value: str) -> None:
        ...


def resolve_database_url(
    config: _ConfigWithMainOption,
    settings_url: Callable[[], str],
) -> str:
    """Prefer a non-empty Alembic URL, falling back to application settings."""

    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url and configured_url.strip():
        return configured_url.strip()
    return settings_url()


def set_alembic_database_url(config: _ConfigWithSetMainOption, database_url: str) -> None:
    """Store a database URL safely in Alembic's ConfigParser-backed options."""

    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


class Settings(BaseSettings):
    """Validated, local-first configuration for the control tower."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://control_tower:control_tower@localhost:5432/control_tower"
    )
    api_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000/api/v1")
    sla_risk_window_hours: int = Field(default=4, gt=0)
    safety_stock: Decimal = Field(default=Decimal("20"), ge=0)
    inventory_mismatch_tolerance: Decimal = Field(default=Decimal("5"), ge=0)
    deterministic_seed: int = Field(default=20250301, ge=0)
    as_of: datetime = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)

    # M03 severity rules consume these in descending operational impact order.
    severity_critical_revenue_at_risk: Decimal = Field(default=Decimal("10000"), ge=0)
    severity_high_revenue_at_risk: Decimal = Field(default=Decimal("5000"), ge=0)
    severity_medium_revenue_at_risk: Decimal = Field(default=Decimal("1000"), ge=0)
    severity_critical_orders_affected: int = Field(default=50, ge=0)
    severity_high_orders_affected: int = Field(default=20, ge=0)
    severity_medium_orders_affected: int = Field(default=5, ge=0)
    severity_critical_overdue_hours: int = Field(default=48, ge=0)
    severity_high_overdue_hours: int = Field(default=24, ge=0)
    severity_medium_overdue_hours: int = Field(default=4, ge=0)

    @field_validator("as_of")
    @classmethod
    def normalize_as_of_to_utc(cls, value: datetime) -> datetime:
        """Require a timezone and normalize deterministic evaluation time to UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value.astimezone(timezone.utc)
