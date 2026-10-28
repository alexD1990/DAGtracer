from pathlib import Path

import pytest

from dgt.config import resolve_config
from dgt.errors import ConfigError


def write_config(cwd: Path, text: str) -> None:
    (cwd / ".dgt.yml").write_text(text, encoding="utf-8")


def test_defaults_without_file(tmp_path):
    config = resolve_config(cwd=tmp_path)
    assert config.project_dir == tmp_path.resolve()
    assert config.profiles_dir is None
    assert config.target is None


def test_file_values_used(tmp_path):
    write_config(tmp_path, "project_dir: proj\nprofiles_dir: /etc/dbt\ntarget: ci\n")
    config = resolve_config(cwd=tmp_path)
    assert config.project_dir == (tmp_path / "proj").resolve()
    assert config.profiles_dir == Path("/etc/dbt")
    assert config.target == "ci"


def test_cli_overrides_file(tmp_path):
    write_config(tmp_path, "project_dir: proj\nprofiles_dir: profiles\ntarget: ci\n")
    config = resolve_config(project_dir="other", profiles_dir="p2", target="prod", cwd=tmp_path)
    assert config.project_dir == (tmp_path / "other").resolve()
    assert config.profiles_dir == (tmp_path / "p2").resolve()
    assert config.target == "prod"


def test_cli_and_file_merge_per_key(tmp_path):
    write_config(tmp_path, "target: ci\n")
    config = resolve_config(project_dir="proj", cwd=tmp_path)
    assert config.project_dir == (tmp_path / "proj").resolve()
    assert config.target == "ci"
    assert config.profiles_dir is None


def test_empty_file_is_allowed(tmp_path):
    write_config(tmp_path, "")
    assert resolve_config(cwd=tmp_path).project_dir == tmp_path.resolve()


@pytest.mark.parametrize(
    "text, message",
    [
        ("project_dir: [a, b]\n", "non-empty string"),
        ("target: 3\n", "non-empty string"),
        ("- a\n- b\n", "mapping"),
        ("project_dir: [\n", "invalid YAML"),
    ],
)
def test_invalid_file_raises(tmp_path, text, message):
    write_config(tmp_path, text)
    with pytest.raises(ConfigError, match=message):
        resolve_config(cwd=tmp_path)


def test_nulls_ignored(tmp_path):
    write_config(tmp_path, "target: null\nproject_dir: proj\n")
    config = resolve_config(cwd=tmp_path)
    assert config.target is None
    assert config.project_dir == (tmp_path / "proj").resolve()
    assert config.warnings == ()


def test_unknown_key_warns_with_suggestion(tmp_path):
    write_config(tmp_path, "profile_dir: p\n")
    config = resolve_config(cwd=tmp_path)
    assert config.profiles_dir is None
    (warning,) = config.warnings
    assert warning.endswith("unknown key 'profile_dir' ignored; did you mean 'profiles_dir'?")


def test_unknown_key_without_close_match_lists_known_keys(tmp_path):
    write_config(tmp_path, "future_option: 1\n")
    (warning,) = resolve_config(cwd=tmp_path).warnings
    assert warning.endswith(
        "unknown key 'future_option' ignored; known keys: project_dir, profiles_dir, target"
    )
