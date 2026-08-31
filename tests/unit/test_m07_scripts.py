import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_backup_restore_drill_bootstraps_before_dump_and_initializes_cleanup_state():
    script = (ROOT / "scripts/backup_restore_drill.sh").read_text(encoding="utf-8")

    trap_position = script.index("trap cleanup EXIT INT TERM")
    assert script.index('TEST_DB=""', 0, trap_position) < trap_position
    assert script.index('TEST_USER=""', 0, trap_position) < trap_position

    ready_position = script.index('pg_isready -U "$TEST_USER" -d "$TEST_DB"')
    migrate_position = script.index('run --rm --build migrate')
    bootstrap_position = script.index('run --rm --build bootstrap')
    dump_position = script.index('pg_dump -Fc')

    assert ready_position < migrate_position < bootstrap_position < dump_position
    assert script.index("select count(*) from public.orders") > dump_position
    assert "source_order_id = 'O000001'" in script


def test_security_check_script_has_dependency_scan_and_container_hardening_gates():
    script = (ROOT / "scripts/security_check.sh").read_text(encoding="utf-8")

    assert '"ls-files", "--cached", "--others", "--exclude-standard", "-z"' in script
    assert '"ls-files", "--cached", "-z"' in script
    assert '"forbidden secret-bearing filename"' in script
    assert "relative in tracked_paths" in script
    assert 'filename == ".env"' in script
    assert 'filename.startswith(".env.")' in script
    assert 'filename != ".env.example"' in script
    assert "pip-audit" in script
    assert "Config.User" in script
    assert "Config.Volumes" in script
    assert 'os.getuid() == 10001' in script
    assert 'os.access("/app", os.W_OK)' in script


def test_security_check_rejects_tracked_forbidden_secret_filename(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(ROOT / "scripts/security_check.sh", scripts_dir / "security_check.sh")
    (tmp_path / ".env").touch()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", ".env", "scripts/security_check.sh"], cwd=tmp_path, check=True
    )

    result = subprocess.run(
        [str(scripts_dir / "security_check.sh")],
        cwd=tmp_path,
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert ".env:1: forbidden secret-bearing filename" in result.stderr
    assert "tracked-text secret scan found" in result.stderr


def test_compose_passes_postgres_fields_without_interpolating_password_into_url():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "DATABASE_URL:" not in compose
    assert compose.count("POSTGRES_HOST: postgres") == 3
    assert compose.count("POSTGRES_PORT: 5432") == 3
    assert compose.count("POSTGRES_DB: ${POSTGRES_DB:-control_tower}") == 4
    assert compose.count("POSTGRES_USER: ${POSTGRES_USER:-control_tower}") == 4
    assert (
        compose.count("POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD explicitly}")
        == 4
    )


def test_release_documents_separate_m06_and_m07_verification_boundaries():
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    release_review = (ROOT / "docs/release-review.md").read_text(encoding="utf-8")

    for document in (architecture, release_review):
        assert "M06" in document
        assert "scripts/verify_release.sh" in document
        assert "scripts/verify_m07.sh" in document
        assert "scripts/security_check.sh" in document
        assert "scripts/backup_restore_drill.sh" in document
        assert "M07" in document
    assert "Digest pinning" in release_review
    assert "lockfile" in release_review
    assert "non-blocking" in release_review
