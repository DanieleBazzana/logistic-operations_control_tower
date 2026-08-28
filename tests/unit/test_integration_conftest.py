"""Regression tests for the destructive PostgreSQL integration reset guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _reset_fixture_function():
    path = Path(__file__).parents[1] / "integration" / "conftest.py"
    spec = importlib.util.spec_from_file_location("integration_conftest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.reset_disposable_postgres_database.__wrapped__, module


def test_postgres_reset_requires_explicit_destructive_opt_in(monkeypatch):
    reset, _ = _reset_fixture_function()
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://test:test@localhost/control_tower_m04",
    )
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_TEST_DB", raising=False)

    with pytest.raises(pytest.fail.Exception, match="ALLOW_DESTRUCTIVE_TEST_DB=1"):
        next(reset())


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://test:test@localhost/control_tower_m04",
        "postgresql+psycopg://test:test@127.0.0.1/control_tower_test",
        "postgresql+psycopg://test:test@[::1]/control_tower_m04_test",
    ],
)
def test_postgres_reset_allows_isolated_local_test_targets(monkeypatch, database_url):
    reset, module = _reset_fixture_function()
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_TEST_DB", "1")
    calls = []
    monkeypatch.setattr(
        module.command,
        "downgrade",
        lambda config, revision: calls.append(("down", revision)),
    )
    monkeypatch.setattr(
        module.command,
        "upgrade",
        lambda config, revision: calls.append(("up", revision)),
    )

    generator = reset()
    next(generator)
    generator.close()

    assert calls == [("down", "base"), ("up", "head")]


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://test:test@database.example/control_tower_m04",
        "postgresql+psycopg://test:test@localhost/control_tower",
    ],
)
def test_postgres_reset_rejects_remote_or_unsafe_local_targets(monkeypatch, database_url):
    reset, module = _reset_fixture_function()
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_TEST_DB", "1")
    calls = []
    monkeypatch.setattr(
        module.command,
        "downgrade",
        lambda config, revision: calls.append(("down", revision)),
    )
    monkeypatch.setattr(
        module.command,
        "upgrade",
        lambda config, revision: calls.append(("up", revision)),
    )

    with pytest.raises(pytest.fail.Exception, match="isolated local test database"):
        next(reset())

    assert calls == []


@pytest.mark.parametrize(
    ("parameter_name", "parameter_value"),
    [
        ("host", "127.0.0.1"),
        ("hostaddr", "127.0.0.1"),
        ("port", "5432"),
        ("dbname", "control_tower_m04"),
        ("database", "control_tower_m04"),
        ("service", "control_tower_m04"),
        ("socket", "/tmp/.s.PGSQL.5432"),
        ("HOST", "127.0.0.1"),
    ],
)
def test_postgres_reset_rejects_any_query_parameter(
    monkeypatch, parameter_name, parameter_value
):
    reset, module = _reset_fixture_function()
    database_url = (
        "postgresql+psycopg://test:test@localhost/control_tower_m04?"
        f"{parameter_name}={parameter_value}"
    )
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_TEST_DB", "1")
    calls = []
    monkeypatch.setattr(
        module.command,
        "downgrade",
        lambda config, revision: calls.append(("down", revision)),
    )
    monkeypatch.setattr(
        module.command,
        "upgrade",
        lambda config, revision: calls.append(("up", revision)),
    )

    with pytest.raises(pytest.fail.Exception, match="query parameters"):
        next(reset())

    assert calls == []


def test_postgres_reset_rejects_duplicate_query_parameters(monkeypatch):
    reset, module = _reset_fixture_function()
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://test:test@localhost/control_tower_m04?"
        "host=localhost&host=127.0.0.1",
    )
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_TEST_DB", "1")
    calls = []
    monkeypatch.setattr(
        module.command,
        "downgrade",
        lambda config, revision: calls.append(("down", revision)),
    )
    monkeypatch.setattr(
        module.command,
        "upgrade",
        lambda config, revision: calls.append(("up", revision)),
    )

    with pytest.raises(pytest.fail.Exception, match="query parameters"):
        next(reset())

    assert calls == []
