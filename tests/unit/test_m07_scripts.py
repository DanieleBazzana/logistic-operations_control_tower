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
    assert 'relative.startswith("tests/")' not in script
    assert '"synthetic",' not in script


def test_secret_ignore_patterns_cover_env_variants_and_preserve_example():
    for path in (".gitignore", ".dockerignore"):
        ignore = (ROOT / path).read_text(encoding="utf-8").splitlines()
        assert ".env" in ignore
        assert ".env.*" in ignore
        assert "!.env.example" in ignore


def test_architecture_describes_versioned_observation_and_activation_flow():
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    for phrase in (
        "source observation", "normalized observation", "capability assessment",
        "operational promotion", "STAGED", "READY", "DatasetActivation", "ACTIVE",
        "RETIRED", "dataset-scoped", "preactivation invisibility", "Neon recovery branch",
        "historical",
    ):
        assert phrase.lower() in architecture.lower()


def test_security_check_scans_fixture_placeholders_without_broad_bypass(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(ROOT / "scripts/security_check.sh", scripts_dir / "security_check.sh")
    fixture = tmp_path / "tests" / "fixtures"
    fixture.mkdir(parents=True)
    (fixture / "sample.py").write_text(
        'PASSWORD = "synthetic-but-not-allowlisted-credential-value"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "scripts/security_check.sh", "tests"], cwd=tmp_path, check=True)

    result = subprocess.run(
        [str(scripts_dir / "security_check.sh")], cwd=tmp_path,
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "tests/fixtures/sample.py:1: credential assignment" in result.stderr
    assert "synthetic-but-not-allowlisted-credential-value" not in result.stderr


def test_security_check_allows_only_known_placeholders_at_exact_paths(tmp_path):
    allowed_sources = {
        ".env.example": (5, 14),
        ".github/workflows/ci.yml": (29, 49, 66, 70),
        "docs/operations.md": (113, 114),
        "tests/unit/test_config.py": (52, 59, 64),
        "tests/unit/test_db.py": (27,),
        "tests/unit/test_m07_production.py": (174,),
        "tests/unit/test_m07_scripts.py": (72,),
    }
    examples = []
    for relative, line_numbers in allowed_sources.items():
        lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        examples.extend((relative, lines[line_number - 1]) for line_number in line_numbers)

    def run_scan(root, files):
        scripts_dir = root / "scripts"
        scripts_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/security_check.sh", scripts_dir / "security_check.sh")
        for relative, line in files:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(line + "\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        docker = root / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *Config.User*) printf '10001:10001\\n' ;;\n"
            "  *Config.Volumes*) printf 'null\\n' ;;\n"
            "  *) printf 'present\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return subprocess.run(
            [str(scripts_dir / "security_check.sh")],
            cwd=root,
            env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "PYTHON": sys.executable},
            capture_output=True,
            text=True,
            check=False,
        )

    accepted = run_scan(tmp_path / "accepted", examples)
    assert accepted.returncode == 0, accepted.stderr

    # Moving each exact source line to another path must not inherit its exception.
    for index, (_, line) in enumerate(examples):
        rejected = run_scan(tmp_path / f"moved-{index}", [(f"unapproved/{index}.txt", line)])
        assert rejected.returncode != 0
        assert f"unapproved/{index}.txt:1:" in rejected.stderr
        assert line.split("=", 1)[-1].strip().strip("\\\"'") not in rejected.stderr

    novel = 'PASS' + 'WORD = "synthetic-unlisted-canary-value-9a4d"'
    rejected = run_scan(tmp_path / "novel", [("tests/fixtures/new.py", novel)])
    assert rejected.returncode != 0
    assert "tests/fixtures/new.py:1: credential assignment" in rejected.stderr
    assert "synthetic-unlisted-canary-value-9a4d" not in rejected.stderr


def test_security_check_detects_and_redacts_deleted_historical_credential(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(ROOT / "scripts/security_check.sh", scripts_dir / "security_check.sh")
    marker = "synthetic-history-credential-marker-7f29c13a"
    (tmp_path / "historical.txt").write_text(f"TOKEN={marker}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", "historical.txt", "scripts/security_check.sh"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "add synthetic credential"], cwd=tmp_path, check=True)
    (tmp_path / "historical.txt").unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "remove synthetic credential"], cwd=tmp_path, check=True
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scanner = bin_dir / "test-gitleaks"
    scanner.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "repo = sys.argv[1]\n"
        "args = sys.argv[2:]\n"
        "assert '--log-opts=--all' in args\n"
        "assert '--redact=100' in args\n"
        "history = subprocess.run(\n"
        "    ['git', 'log', '-p', '--all'], cwd=repo, capture_output=True, text=True, check=True\n"
        ").stdout\n"
        f"if {marker!r} in history:\n"
        "    print('historical credential finding: [REDACTED]')\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if 'zricethezav/gitleaks:v8.24.2' in args:\n"
        "    assert '--rm' in args\n"
        "    mount = next(arg for arg in args if arg.startswith('--mount='))\n"
        "    assert mount.endswith(',readonly')\n"
        "    repo = mount.split('source=', 1)[1].split(',', 1)[0]\n"
        "    cmd = [os.environ['TEST_HISTORY_SCANNER'], repo, *args[args.index('detect') + 1:]]\n"
        "    raise SystemExit(subprocess.run(cmd, check=False).returncode)\n"
        "if args[:2] == ['image', 'inspect']:\n"
        "    print('present')\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['run', '--rm']:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('unexpected docker invocation')\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = subprocess.run(
        [str(scripts_dir / "security_check.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHON": sys.executable,
            "TEST_HISTORY_SCANNER": str(scanner),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    script = (scripts_dir / "security_check.sh").read_text(encoding="utf-8")
    assert "zricethezav/gitleaks:v8.24.2" in script
    assert "--log-opts=--all" in script
    assert "--redact=100" in script
    assert result.returncode != 0
    assert "historical credential finding: [REDACTED]" in result.stdout
    assert marker not in result.stdout
    assert marker not in result.stderr


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


def test_release_documents_separate_historical_m06_and_current_m07_evidence():
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    release_review = (ROOT / "docs/release-review.md").read_text(encoding="utf-8")

    assert "M06" in architecture
    assert "M06 and M07 verification boundaries" in architecture
    for gate in (
        "scripts/verify_release.sh",
        "scripts/verify_m07.sh",
        "scripts/security_check.sh",
        "scripts/backup_restore_drill.sh",
    ):
        assert gate in architecture
        assert gate in release_review

    assert "M07 traceability" in release_review
    assert "history" in release_review
    assert "Digest pinning" in release_review
    assert "lockfile" in release_review
    assert "non-blocking" in release_review
