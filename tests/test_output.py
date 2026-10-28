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
        "   -2 s  seed      TEXT  present\n"
        "   -1 a  model     TEXT  present\n"
        "      b  model     -     absent     <- stop\n"
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
    assert "Findings (0)\n  No findings: no reached node changes status" in text
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
        "  -1  column_appears           a [k=present]\n"
        "   0  value_dropped            b [m]\n"
        "        parents: a (present; k=present, m=present)\n"
    )


def test_column_appears_lists_values_with_status():
    project = make_project([("a", "b")], {"a": [], "b": ["x"]})
    show = FakeShow({"b": {"k": 1}})
    assert "   0  column_appears           b [k=present, m=absent]\n" in render(
        project, "b", "x", ["k", "m"], show
    )


def test_text_output_both_directions():
    project = make_project([("a", "b"), ("b", "c")], {"a": ["x"], "b": ["x"], "c": []})
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("b"), "x",
        [Direction.UP, Direction.DOWN],
    )
    assert render_text(report) == (
        "Column 'x' (TEXT) from b (model.p.b)\n"
        "Start status: present\n"
        "\n"
        "Upstream\n"
        "   -1 a  model     TEXT  present\n"
        "    0 b  model     TEXT  present\n"
        "\n"
        "Downstream\n"
        "   +1 c  model     -     absent     <- stop\n"
        "\n"
        "Findings (2)\n"
        "  Upstream\n"
        "    -1  up    column_appears           a\n"
        "  Downstream\n"
        "    Stops at\n"
        "      +1  down  column_dropped           c\n"
        "            parents: b (present)\n"
    )


def test_text_output_explains_no_findings_downstream():
    # c gets x from b, so it does not appear at c; nothing else to report.
    project = make_project([("a", "c"), ("b", "c")], {"a": [], "b": ["x"], "c": ["x"]})
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("a"), "x", [Direction.DOWN]
    )
    text = render_text(report)
    assert "Findings (0)\n  No findings: no reached node changes status relative" in text
    assert "column 'x' is absent in a" in text


def test_column_level_values_shortened_when_all_absent():
    project = make_project([("seed:s", "b")], {"seed:s": [], "b": []})
    text = render(project, "b", "x", ["k", "m"], FakeShow({}))
    assert "  -1  column_absent_at_source  s [all values absent]\n" in text


def test_downstream_findings_grouped_by_reach():
    # a -> b (keeps x, leaf), a -> c (drops x) -> d (x appears again, leaf)
    project = make_project(
        [("a", "b"), ("a", "c"), ("c", "d")], {"a": ["x"], "b": ["x"], "c": [], "d": ["x"]}
    )
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("a"), "x", [Direction.DOWN],
        full_trace=True,
    )
    assert render_text(report).split("Findings (4)\n")[1] == (
        "  Reaches\n"
        "    +1  present_at_leaf          b\n"
        "    +2  present_at_leaf          d\n"
        "  Stops at\n"
        "    +1  column_dropped           c\n"
        "          parents: a (present)\n"
        "  Appears at\n"
        "    +2  column_appears           d\n"
        "          parents: c (absent)\n"
    )
