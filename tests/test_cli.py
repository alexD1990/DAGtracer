import pytest

from dgt.cli import EXIT_OK, EXIT_TOOL_ERROR, build_parser, main, read_values


def test_values_repeatable():
    args = build_parser().parse_args(["m", "-c", "x", "-v", "a", "-v", "b"])
    assert read_values(args) == ["a", "b"]


def test_values_file_skips_blank_lines(tmp_path):
    path = tmp_path / "values.txt"
    path.write_text("a\n\n  b  \n")
    args = build_parser().parse_args(["m", "-c", "x", "--values-file", str(path)])
    assert read_values(args) == ["a", "b"]


def test_values_and_values_file_exclusive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["m", "-c", "x", "-v", "a", "--values-file", "f"])
    assert exc.value.code == 2


def test_column_required():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["m"])
    assert exc.value.code == 2


def test_traces_upstream(built_project_dir, capsys):
    code = main(["dim_customers", "-c", "last_name", "--project-dir", str(built_project_dir)])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Findings (1)" in out
    assert "present_at_source  raw_customers" in out


def test_unknown_start_status_is_tool_error(built_project_dir, capsys):
    code = main(["int_order_payments", "-c", "total_paid", "--project-dir", str(built_project_dir)])
    assert code == EXIT_TOOL_ERROR


@pytest.mark.parametrize(
    "extra", [["-d", "down"], ["-d", "both"], ["-v", "a"], ["-o", "json"]]
)
def test_unimplemented_options_are_tool_errors(built_project_dir, extra, capsys):
    argv = ["dim_customers", "-c", "x", "--project-dir", str(built_project_dir), *extra]
    assert main(argv) == EXIT_TOOL_ERROR
    assert "not implemented yet" in capsys.readouterr().err


def test_unknown_model_is_tool_error(built_project_dir, capsys):
    code = main(["nope", "-c", "x", "--project-dir", str(built_project_dir)])
    assert code == EXIT_TOOL_ERROR
    assert "no model" in capsys.readouterr().err


def test_missing_project_is_tool_error(tmp_path, capsys):
    assert main(["m", "-c", "x", "--project-dir", str(tmp_path)]) == EXIT_TOOL_ERROR


def test_config_warnings_go_to_stderr(built_project_dir, tmp_path, monkeypatch, capsys):
    (tmp_path / ".dgt.yml").write_text(f"project_dir: {built_project_dir}\nprofile_dir: x\n")
    monkeypatch.chdir(tmp_path)
    assert main(["dim_customers", "-c", "last_name"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "dgt: warning:" in captured.err
    assert "did you mean 'profiles_dir'?" in captured.err
    assert "warning" not in captured.out
