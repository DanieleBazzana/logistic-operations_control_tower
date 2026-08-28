from __future__ import annotations

from datetime import datetime, timezone

import pytest

from control_tower.exceptions import __main__ as exception_cli


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.closed = False

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeEngine:
    def dispose(self) -> None:
        pass


class FakeService:
    def __init__(self, session: FakeSession, settings: object) -> None:
        self.session = session
        self.settings = settings

    def detect(self, as_of: datetime) -> object:
        return type(
            "Result",
            (),
            {
                "as_of": as_of,
                "detections": (),
                "count": 0,
                "created": 2,
                "updated": 1,
                "skipped": 0,
            },
        )()


def test_help_describes_explicit_as_of(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        exception_cli.main(["--help"])

    assert exit_info.value.code == 0
    assert "--as-of" in capsys.readouterr().out


def test_as_of_is_required_and_timezone_aware() -> None:
    with pytest.raises(SystemExit) as missing:
        exception_cli.main([])
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as naive:
        exception_cli.main(["--as-of", "2025-01-15T12:00:00"])
    assert naive.value.code == 2


def test_main_runs_service_with_explicit_utc_instant_and_database_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession()
    captured: dict[str, object] = {}

    def fake_settings(**kwargs: object) -> object:
        captured["settings"] = kwargs
        return kwargs

    def fake_engine(settings: object) -> object:
        captured["engine_settings"] = settings
        return FakeEngine()

    def fake_factory(*, engine: object) -> object:
        captured["engine"] = engine
        return lambda: session

    monkeypatch.setattr(exception_cli, "Settings", fake_settings)
    monkeypatch.setattr(exception_cli, "create_db_engine", fake_engine)
    monkeypatch.setattr(exception_cli, "create_session_factory", fake_factory)
    monkeypatch.setattr(exception_cli, "ExceptionService", FakeService)

    exit_code = exception_cli.main(
        [
            "--as-of",
            "2025-01-15T13:00:00+01:00",
            "--database-url",
            "postgresql+psycopg://test:test@localhost/control_tower_test",
        ]
    )

    assert exit_code == 0
    assert captured["settings"] == {
        "database_url": "postgresql+psycopg://test:test@localhost/control_tower_test",
        "as_of": datetime(2025, 1, 15, 12, tzinfo=timezone.utc),
    }
    assert session.committed
    assert session.closed
    assert "\"created\": 2" in capsys.readouterr().out
