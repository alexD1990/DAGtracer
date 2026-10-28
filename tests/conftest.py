import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dgt.project import DbtProject

TESTPROJECT = Path(__file__).resolve().parent.parent / "testproject"
BUILD_ARTIFACTS = ("target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal", ".user.yml")
# dbt resolves ref() only for nodes with created_at >= the parse start time
# (dbt/parser/manifest.py, process_refs), both taken from time.time(). A
# backward wall-clock jump during parsing (seen on WSL2) therefore leaves
# depends_on empty and the build fails; a clean rebuild succeeds.
BUILD_ATTEMPTS = 3


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
    for _ in range(BUILD_ATTEMPTS):
        failure = _build(dbt, project_dir)
        if failure is None:
            return project_dir
        shutil.rmtree(project_dir / "target", ignore_errors=True)
    pytest.fail(failure)


def _build(dbt: str, project_dir: Path) -> str | None:
    """Run dbt build and docs generate; return a failure message or None."""
    for command in (["build"], ["docs", "generate"]):
        result = subprocess.run(
            [dbt, *command, "--profiles-dir", str(project_dir), "--no-partial-parse"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            errors = [line for line in output.splitlines() if "error" in line.lower()]
            headline = errors[-1].strip() if errors else output.splitlines()[-1]
            return f"dbt {' '.join(command)} failed: {headline}\n{output}"
    return None


@pytest.fixture(scope="session")
def project(built_project_dir) -> DbtProject:
    return DbtProject.load(built_project_dir)
