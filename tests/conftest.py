import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dgt.project import DbtProject

TESTPROJECT = Path(__file__).resolve().parent.parent / "testproject"
BUILD_ARTIFACTS = ("target", "logs", "testproject.duckdb", ".user.yml")


def _dbt_executable() -> str:
    local = Path(sys.executable).parent / "dbt"
    found = str(local) if local.exists() else shutil.which("dbt")
    if found is None:
        pytest.skip("dbt is not installed")
    return found


@pytest.fixture(scope="session")
def built_project_dir(tmp_path_factory) -> Path:
    """A copy of testproject with seeds/models built and catalog generated."""
    project_dir = tmp_path_factory.mktemp("dbt") / "testproject"
    shutil.copytree(TESTPROJECT, project_dir, ignore=shutil.ignore_patterns(*BUILD_ARTIFACTS))
    dbt = _dbt_executable()
    for command in (["build"], ["docs", "generate"]):
        result = subprocess.run(
            [dbt, *command, "--profiles-dir", str(project_dir)],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    return project_dir


@pytest.fixture(scope="session")
def project(built_project_dir) -> DbtProject:
    return DbtProject.load(built_project_dir)
