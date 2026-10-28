import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dgt.project import DbtProject, unresolved_dependency_nodes

TESTPROJECT = Path(__file__).resolve().parent.parent / "testproject"
BUILD_ARTIFACTS = ("target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal", ".user.yml")
# A backward wall-clock jump during parsing (seen on WSL2) can leave dbt's
# manifest with unresolved dependencies; see dgt.project.validate_manifest.
# Only that failure is retried.
BUILD_ATTEMPTS = 3


@pytest.fixture(scope="session")
def dbt_executable() -> str:
    local = Path(sys.executable).parent / "dbt"
    found = str(local) if local.exists() else shutil.which("dbt")
    if found is None:
        pytest.skip("dbt is not installed")
    return found


@pytest.fixture(scope="session")
def built_project_dir(tmp_path_factory, dbt_executable) -> Path:
    """A copy of testproject with seeds/models built and catalog generated."""
    project_dir = tmp_path_factory.mktemp("dbt") / "testproject"
    shutil.copytree(TESTPROJECT, project_dir, ignore=shutil.ignore_patterns(*BUILD_ARTIFACTS))
    for _ in range(BUILD_ATTEMPTS):
        if _build(dbt_executable, project_dir):
            return project_dir
        shutil.rmtree(project_dir / "target")
    pytest.fail(f"dbt parse produced unresolved dependencies {BUILD_ATTEMPTS} times in a row")


def _build(dbt: str, project_dir: Path) -> bool:
    """Run dbt build and docs generate.

    Returns False if the parse left nodes without dependencies; fails the
    test session on any other error.
    """
    for command in (["build"], ["docs", "generate"]):
        result = subprocess.run(
            [dbt, *command, "--profiles-dir", str(project_dir)],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            continue
        manifest_path = project_dir / "target" / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if unresolved_dependency_nodes(manifest):
                return False
        output = (result.stdout + result.stderr).strip()
        errors = [line for line in output.splitlines() if "error" in line.lower()]
        headline = errors[-1].strip() if errors else output.splitlines()[-1]
        pytest.fail(f"dbt {' '.join(command)} failed: {headline}\n{output}")
    return True


@pytest.fixture(scope="session")
def project(built_project_dir) -> DbtProject:
    return DbtProject.load(built_project_dir)
