"""Behavior of dbt itself that dgt relies on."""

import json
import shutil
import subprocess

ARTIFACTS = ("manifest.json", "catalog.json", "run_results.json", "semantic_manifest.json")


def test_no_write_json_show_leaves_target_artifacts_untouched(
    built_project_dir, dbt_executable, tmp_path
):
    project_dir = tmp_path / "testproject"
    shutil.copytree(built_project_dir, project_dir)
    target = project_dir / "target"
    before = {name: (target / name).read_bytes() for name in ARTIFACTS if (target / name).exists()}
    relation = json.loads(before["manifest.json"])["nodes"]["model.testproject.stg_orders"][
        "relation_name"
    ]

    result = subprocess.run(
        [
            dbt_executable, "--no-write-json", "show",
            "--inline", f"select count(*) as v0 from {relation}",
            "--output", "json", "--quiet", "--profiles-dir", str(project_dir),
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"v0": 5' in result.stdout
    after = {name: (target / name).read_bytes() for name in ARTIFACTS if (target / name).exists()}
    assert after.keys() == before.keys()
    for name in before:
        assert after[name] == before[name], f"{name} was rewritten"
