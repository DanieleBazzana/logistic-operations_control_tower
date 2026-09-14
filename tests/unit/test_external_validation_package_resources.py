"""Regression coverage for external-validation manifest packaging."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from importlib import resources
from pathlib import Path

import pytest

from control_tower.external_validation.acquisition import load_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_MANIFEST_DIR = PROJECT_ROOT / "docs" / "external-validation" / "manifests"


@pytest.mark.parametrize("dataset", ["olist", "dataco"])
def test_packaged_manifest_is_byte_identical_to_canonical_fixture(dataset: str) -> None:
    """Wheel-installed manifests must preserve the canonical fixture bytes."""

    filename = f"{dataset}.json"
    canonical = (CANONICAL_MANIFEST_DIR / filename).read_bytes()
    packaged = (
        resources.files("control_tower.external_validation")
        .joinpath("manifests", filename)
        .read_bytes()
    )

    assert packaged == canonical
    assert load_manifest(dataset) == json.loads(canonical)


def _run_checked(
    command: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and include captured output when it fails."""

    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        output = error.stdout or "<no subprocess output>"
        rendered_command = " ".join(command)
        raise AssertionError(
            f"subprocess failed with exit code {error.returncode}: {rendered_command}\n{output}"
        ) from error


def test_non_editable_wheel_preserves_manifests_outside_repository(tmp_path: Path) -> None:
    """An installed wheel must provide runtime resources without source-tree imports."""

    outside_dir = tmp_path / "outside-repository"
    outside_dir.mkdir()
    assert PROJECT_ROOT not in outside_dir.parents

    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    venv_dir = tmp_path / "venv"
    command_env = os.environ.copy()
    command_env.pop("PYTHONPATH", None)

    _run_checked(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(PROJECT_ROOT),
        ],
        cwd=outside_dir,
        env=command_env,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one project wheel, found {wheels}"

    _run_checked(
        [sys.executable, "-m", "venv", str(venv_dir)],
        cwd=outside_dir,
        env=command_env,
    )
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run_checked(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheels[0]),
        ],
        cwd=outside_dir,
        env=command_env,
    )

    subprocess_script = textwrap.dedent(
        """
        import importlib
        import importlib.util
        import json
        import sys
        import types
        from importlib import resources
        from pathlib import Path

        canonical_dir = Path(sys.argv[1]).resolve()
        repository_root = Path(sys.argv[2]).resolve()
        virtualenv_root = Path(sys.argv[3]).resolve()

        import control_tower

        # Load only the stdlib-only submodule; package __init__ exports unrelated optional code.
        external_validation_path = Path(control_tower.__file__).parent / "external_validation"
        external_validation = types.ModuleType("control_tower.external_validation")
        external_validation.__path__ = [str(external_validation_path)]
        external_validation.__spec__ = importlib.util.spec_from_file_location(
            "control_tower.external_validation",
            external_validation_path / "__init__.py",
            submodule_search_locations=[str(external_validation_path)],
        )
        sys.modules[external_validation.__name__] = external_validation
        acquisition = importlib.import_module("control_tower.external_validation.acquisition")
        load_manifest = acquisition.load_manifest

        site_packages = [
            Path(entry).resolve()
            for entry in sys.path
            if Path(entry).name == "site-packages"
            and virtualenv_root in Path(entry).resolve().parents
        ]
        assert site_packages, f"venv site-packages missing from sys.path: {sys.path}"
        for module in (control_tower, acquisition):
            module_path = Path(module.__file__).resolve()
            assert repository_root not in module_path.parents, (
                f"{module.__name__} imported from source tree: {module_path}"
            )
            assert any(site_path in module_path.parents for site_path in site_packages), (
                f"{module.__name__} imported outside venv site-packages: {module_path}"
            )

        loaded = {}
        for dataset in ("olist", "dataco"):
            filename = f"{dataset}.json"
            canonical = (canonical_dir / filename).read_bytes()
            resource = resources.files("control_tower.external_validation").joinpath(
                "manifests"
            ).joinpath(filename)
            assert resource.is_file(), f"wheel resource is absent: {filename}"
            packaged = resource.read_bytes()
            assert packaged == canonical, f"installed resource differs from canonical: {filename}"
            loaded[dataset] = load_manifest(dataset)
            assert loaded[dataset] == json.loads(canonical)

        print(
            json.dumps(
                {"modules": [control_tower.__file__, acquisition.__file__], "datasets": loaded}
            )
        )
        """
    )
    result = _run_checked(
        [
            str(venv_python),
            "-c",
            subprocess_script,
            str(CANONICAL_MANIFEST_DIR),
            str(PROJECT_ROOT),
            str(venv_dir),
        ],
        cwd=outside_dir,
        env=command_env,
    )
    assert result.stdout
