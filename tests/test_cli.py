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


def test_resolves_start_node(built_project_dir, capsys):
    code = main(["fct_orders", "-c", "order_id", "--project-dir", str(built_project_dir)])
    assert code == EXIT_OK
    assert "model.testproject.fct_orders" in capsys.readouterr().err


def test_unknown_model_is_tool_error(built_project_dir, capsys):
    code = main(["nope", "-c", "x", "--project-dir", str(built_project_dir)])
    assert code == EXIT_TOOL_ERROR
    assert "no model" in capsys.readouterr().err


def test_missing_project_is_tool_error(tmp_path, capsys):
    assert main(["m", "-c", "x", "--project-dir", str(tmp_path)]) == EXIT_TOOL_ERROR
