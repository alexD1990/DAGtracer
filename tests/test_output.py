from dgt.graph import DagGraph, Direction
from dgt.output import render_text
from dgt.traverse import trace_column

from factory import make_project


def test_text_output_groups_levels():
    project = make_project(
        [("seed:s", "a"), ("a", "c"), ("b", "c")],
        {"seed:s": ["x"], "a": ["x"], "b": [], "c": ["x"]},
    )
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("c"), "x", [Direction.UP]
    )
    assert render_text(report) == (
        "Column 'x' from c (model.p.c)\n"
        "Start status: present\n"
        "\n"
        "Upstream\n"
        "   -1 a  model     present\n"
        "      b  model     absent     <- stop\n"
        "   -2 s  seed      present\n"
        "\n"
        "Findings (1)\n"
        "  -2  present_at_source  s\n"
    )


def test_text_output_lists_warnings_and_empty_trace():
    project = make_project(
        [], {"a": ["x"]}, generated_at=("2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z")
    )
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("a"), "y", [Direction.UP]
    )
    text = render_text(report)
    assert "  (no neighbors)\n" in text
    assert "Findings (0)\n  none\n" in text
    assert "\nWarnings\n  catalog.json" in text


def test_text_output_lists_parents_of_finding():
    project = make_project([("a", "b")], {"a": [], "b": ["x"]})
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("b"), "x", [Direction.UP]
    )
    assert render_text(report).endswith(
        "Findings (1)\n"
        "   0  column_appears     b\n"
        "        parents: a (absent)\n"
    )


def test_text_output_marks_uncertain_findings():
    project = make_project(
        [("a", "b"), ("b", "c")], {"a": [], "b": None, "c": ["x"]}, ephemeral={"b"}
    )
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("c"), "x", [Direction.UP]
    )
    assert render_text(report).endswith(
        "   0  column_appears     c  (uncertain: c, b)\n"
        "        parents: a (absent, via b)\n"
    )
