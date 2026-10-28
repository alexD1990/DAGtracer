from dgt.graph import DagGraph, Direction
from dgt.output import render_text
from dgt.probes import ValueProbe
from dgt.traverse import trace_column

from factory import FakeShow, make_project


def render(project, start, column, values=(), show=None):
    value_probe = ValueProbe(values, show) if values else None
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node(start), column, [Direction.UP],
        value_probe=value_probe,
    )
    return render_text(report)


def test_text_output_groups_levels():
    project = make_project(
        [("seed:s", "a"), ("a", "c"), ("b", "c")],
        {"seed:s": ["x"], "a": ["x"], "b": [], "c": ["x"]},
    )
    assert render(project, "c", "x") == (
        "Column 'x' (TEXT) from c (model.p.c)\n"
        "Start status: present\n"
        "\n"
        "Upstream\n"
        "   -1 a  model     TEXT  present\n"
        "      b  model     -     absent     <- stop\n"
        "   -2 s  seed      TEXT  present\n"
        "\n"
        "Findings (1)\n"
        "  -2  present_at_source        s\n"
    )


def test_text_output_lists_warnings_and_empty_trace():
    project = make_project(
        [], {"a": ["x"]}, generated_at=("2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z")
    )
    text = render(project, "a", "y")
    assert "  (no neighbors)\n" in text
    assert "Findings (0)\n  none\n" in text
    assert "\nWarnings\n  catalog.json" in text


def test_text_output_lists_parents_of_finding():
    project = make_project([("a", "b")], {"a": [], "b": ["x"]})
    assert render(project, "b", "x").endswith(
        "Findings (1)\n"
        "   0  column_appears           b\n"
        "        parents: a (absent)\n"
    )


def test_text_output_marks_uncertain_findings():
    project = make_project(
        [("a", "b"), ("b", "c")], {"a": [], "b": None, "c": ["x"]}, ephemeral={"b"}
    )
    assert render(project, "c", "x").endswith(
        "   0  column_appears           c  (uncertain: c, b)\n"
        "        parents: a (absent, via b)\n"
    )


def test_text_output_value_mode():
    project = make_project([("a", "b")], {"a": ["x"], "b": ["x"]})
    show = FakeShow({"a": {"k": 2, "m": 1}, "b": {"k": 1}})
    assert render(project, "b", "x", ["k", "m"], show) == (
        "Column 'x' (TEXT) from b (model.p.b)\n"
        "Values: k, m\n"
        "Start status: present; k=present, m=absent\n"
        "\n"
        "Upstream\n"
        "   -1 a  model     TEXT  present    k=present, m=present\n"
        "\n"
        "Findings (2)\n"
        "   0  value_dropped            b [m]\n"
        "        parents: a (present; k=present, m=present)\n"
        "  -1  column_appears           a [k]\n"
    )
