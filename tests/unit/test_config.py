import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from control_tower.config import Settings, resolve_database_url, set_alembic_database_url


def test_alembic_config_round_trips_url_encoded_percent() -> None:
    database_url = "postgresql+psycopg://user:p%40ss@localhost:5432/db"
    config = Config()

    set_alembic_database_url(config, database_url)

    assert config.get_main_option("sqlalchemy.url") == database_url


def test_alembic_offline_generation_accepts_url_encoded_percent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = "postgresql+psycopg://user:p%40ss@localhost:5432/db"
    config = Config("alembic.ini")
    set_alembic_database_url(config, database_url)

    command.upgrade(config, "head", sql=True)

    assert "CREATE TABLE products" in capsys.readouterr().out


def test_explicit_alembic_config_url_takes_precedence() -> None:
    settings_url_called = False

    def settings_url() -> str:
        nonlocal settings_url_called
        settings_url_called = True
        return "postgresql+psycopg://settings/settings@localhost/settings"

    config = Config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://explicit/explicit@db/explicit")
    selected_url = resolve_database_url(
        config,
        settings_url,
    )

    assert selected_url == "postgresql+psycopg://explicit/explicit@db/explicit"
    assert not settings_url_called


def test_settings_uses_database_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://override:override@postgres:5432/override"
    )

    settings = Settings()

    assert settings.database_url.scheme == "postgresql+psycopg"
    assert settings.database_url.unicode_string() == (
        "postgresql+psycopg://override:override@postgres:5432/override"
    )


def test_settings_rejects_non_positive_sla_risk_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLA_RISK_WINDOW_HOURS", "0")

    with pytest.raises(ValidationError, match="greater than 0"):
        Settings()


def test_settings_rejects_negative_inventory_mismatch_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVENTORY_MISMATCH_TOLERANCE", "-1")

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings()


def test_settings_loads_deterministic_severity_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERITY_CRITICAL_REVENUE_AT_RISK", "25000")
    monkeypatch.setenv("SEVERITY_HIGH_ORDERS_AFFECTED", "12")
    monkeypatch.setenv("AS_OF", "2025-02-01T08:30:00+01:00")

    settings = Settings()

    assert settings.severity_critical_revenue_at_risk == 25000
    assert settings.severity_high_orders_affected == 12
    assert settings.as_of.isoformat() == "2025-02-01T07:30:00+00:00"


def test_settings_rejects_invalid_severity_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERITY_MEDIUM_ORDERS_AFFECTED", "-1")

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings()
