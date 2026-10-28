"""The JSON/NDJSON output contract, validated against schema/dgt-output-1.0.schema.json."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from dgt.cli import EXIT_OK, EXIT_TOOL_ERROR, main
from dgt.graph import DagGraph, Direction
from dgt.output import RunMeta, build_document, render_ndjson
from dgt.probes import ValueProbe
from dgt.traverse import trace_column

from factory import FakeShow, make_project

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "dgt-output-1.0.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
DOCUMENT = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
EVENT = Draft202012Validator(
    {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/ndjson_event"}, format_checker=FormatChecker()
)
META = RunMeta("0.1.0", "both", None, "2024-01-01T00:00:00.000Z", "2024-01-01T00:00:01.000Z")
TIMESTAMPS = ("started_at", "finished_at")

# The published schema allows unknown fields (new fields are a minor bump), so
# the exact key sets of what dgt writes today are pinned here instead. A new
# field must be added both here and to the schema.
NODE_REF = {"unique_id", "name", "resource_type"}
KEYS = {
    "document": {"schema_version", "tool_version", "status", "run", "traversals", "findings",
                 "warnings", "error"},
    "run_header": {"start", "column", "column_type", "values", "direction", "full_trace",
                   "target", "started_at"},
    "start": NODE_REF | {"status", "values"},
    "stats": {"nodes_visited", "nodes_probed", "value_queries", "query_errors", "findings",
              "uncertain_findings"},
    "traversals": {"up", "down"},
    "traversal": {"nodes"},
    "visit": NODE_REF | {"level", "status", "values", "column_type", "unknown_reason", "detail",
                         "stopped", "reached_from"},
    "finding": {"direction", "level", "cause", "node", "values", "uncertain", "candidates",
                "parents"},
    "parent": NODE_REF | {"status", "values", "via"},
    "error": {"message"},
    "run_started": {"event", "schema_version", "tool_version", "run"},
    "node_probed": {"event", "direction", "node"},
    "finding_event": {"event", "finding"},
    "warning": {"event", "message"},
    "run_finished": {"event", "status", "finished_at", "stats", "error"},
}


def assert_keys(obj, kind):
    assert set(obj) == KEYS[kind], f"{kind}: {sorted(set(obj) ^ KEYS[kind])}"


def assert_run_header_keys(run, extra=frozenset()):
    assert set(run) == KEYS["run_header"] | extra
    assert_keys(run["start"], "start")


def assert_finding_keys(finding):
    assert_keys(finding, "finding")
    assert set(finding["node"]) == NODE_REF
    for parent in finding["parents"]:
        assert_keys(parent, "parent")


def validate_document(document):
    DOCUMENT.validate(document)
    assert_keys(document, "document")
    assert_keys(document["traversals"], "traversals")
    if document["error"] is not None:
        assert_keys(document["error"], "error")
    if document["run"] is not None:
        assert_run_header_keys(document["run"], {"finished_at", "stats"})
        assert_keys(document["run"]["stats"], "stats")
    for traversal in document["traversals"].values():
        if traversal is not None:
            assert_keys(traversal, "traversal")
            for visit in traversal["nodes"]:
                assert_keys(visit, "visit")
    for finding in document["findings"]:
        assert_finding_keys(finding)


def validate_event(event):
    EVENT.validate(event)
    kind = event["event"]
    assert_keys(event, "finding_event" if kind == "finding" else kind)
    if kind == "run_started":
        assert_run_header_keys(event["run"])
    elif kind == "node_probed":
        assert_keys(event["node"], "visit")
    elif kind == "finding":
        assert_finding_keys(event["finding"])
    elif kind == "run_finished":
        if event["stats"] is not None:
            assert_keys(event["stats"], "stats")
        if event["error"] is not None:
            assert_keys(event["error"], "error")


def test_schema_is_valid():
    Draft202012Validator.check_schema(SCHEMA)


def run_cli(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out


def without_timestamps(value):
    if isinstance(value, dict):
        return {k: without_timestamps(v) for k, v in value.items() if k not in TIMESTAMPS}
    if isinstance(value, list):
        return [without_timestamps(v) for v in value]
    return value


@pytest.mark.parametrize(
    "argv",
    [
        ["dim_customers", "-c", "last_name"],
        ["stg_customers", "-c", "last_name", "-d", "both"],
        ["dim_customers", "-c", "nope", "-d", "both", "--full-trace"],
        ["fct_orders", "-c", "total_paid", "-d", "down"],
    ],
)
def test_json_output_matches_schema(built_project_dir, capsys, argv):
    code, out = run_cli(capsys, *argv, "-o", "json", "--project-dir", str(built_project_dir))
    assert code == EXIT_OK
    document = json.loads(out)
    validate_document(document)
    assert document["status"] in ("complete", "partial")


def test_json_reports_partial_for_uncertain_findings(built_project_dir, capsys):
    argv = ["fct_orders", "-c", "total_paid", "-o", "json", "--project-dir", str(built_project_dir)]
    document = json.loads(run_cli(capsys, *argv)[1])
    assert document["status"] == "partial"
    (finding,) = document["findings"]
    assert finding["uncertain"] is True
    assert finding["candidates"] == [
        "model.testproject.fct_orders",
        "model.testproject.int_order_payments",
    ]


def test_tool_error_writes_error_document(built_project_dir, capsys):
    code, out = run_cli(
        capsys, "nope", "-c", "x", "-o", "json", "--project-dir", str(built_project_dir)
    )
    assert code == EXIT_TOOL_ERROR
    document = json.loads(out)
    validate_document(document)
    assert document["status"] == "error"
    assert "no model, seed or snapshot named 'nope'" in document["error"]["message"]


def test_tool_error_with_ndjson_writes_run_finished(built_project_dir, capsys):
    code, out = run_cli(
        capsys, "nope", "-c", "x", "-o", "ndjson", "--project-dir", str(built_project_dir)
    )
    assert code == EXIT_TOOL_ERROR
    (line,) = out.splitlines()
    event = json.loads(line)
    validate_event(event)
    assert (event["event"], event["status"]) == ("run_finished", "error")


def test_ndjson_events_match_schema_and_order(built_project_dir, capsys):
    argv = ["stg_customers", "-c", "last_name", "-d", "both", "-o", "ndjson",
            "--project-dir", str(built_project_dir)]
    code, out = run_cli(capsys, *argv)
    assert code == EXIT_OK
    events = [json.loads(line) for line in out.splitlines()]
    for event in events:
        validate_event(event)
    kinds = [event["event"] for event in events]
    order = ["run_started", "node_probed", "finding", "warning", "run_finished"]
    assert kinds == sorted(kinds, key=order.index)
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert kinds.count("finding") == events[-1]["stats"]["findings"]


@pytest.mark.parametrize("output", ["json", "ndjson"])
def test_output_is_deterministic(built_project_dir, capsys, output):
    argv = ["dim_customers", "-c", "last_name", "-d", "both", "--full-trace", "-o", output,
            "--project-dir", str(built_project_dir)]
    first, second = run_cli(capsys, *argv)[1], run_cli(capsys, *argv)[1]
    parse = (
        (lambda text: json.loads(text))
        if output == "json"
        else (lambda text: [json.loads(line) for line in text.splitlines()])
    )
    assert without_timestamps(parse(first)) == without_timestamps(parse(second))
    strip = lambda text: "\n".join(  # noqa: E731
        line for line in text.splitlines() if not any(f'"{t}"' in line for t in TIMESTAMPS)
    )
    if output == "json":
        assert strip(first) == strip(second)


def _value_report(fail=()):
    project = make_project(
        [("seed:s", "e"), ("e", "a"), ("seed:t", "a"), ("a", "b")],
        {"seed:s": ["x"], "seed:t": ["x"], "e": None, "a": ["x"], "b": ["X"]},
        ephemeral={"e"},
    )
    show = FakeShow(
        {"s": {"k": 1}, "t": {"m": 1}, "a": {"m": 1}, "b": {"m": 1}}, fail=fail
    )
    return trace_column(
        project, DagGraph(project.manifest), project.find_node("a"), "x",
        [Direction.UP, Direction.DOWN], value_probe=ValueProbe(["k", "m"], show),
    )


def test_value_mode_document_matches_schema():
    document = build_document(_value_report(), META)
    validate_document(document)
    assert document["run"]["values"] == ["k", "m"]
    assert document["run"]["start"]["values"] == {"k": "absent", "m": "present"}
    causes = {(f["cause"], f["node"]["name"]) for f in document["findings"]}
    assert ("value_dropped", "a") in causes
    for line in render_ndjson(_value_report(), META).splitlines():
        validate_event(json.loads(line))


def test_query_error_makes_status_partial():
    document = build_document(_value_report(fail={"t"}), META)
    validate_document(document)
    assert document["status"] == "partial"
    assert document["run"]["stats"]["query_errors"] == 1
    (failed,) = [n for n in document["traversals"]["up"]["nodes"] if n["name"] == "t"]
    assert failed["unknown_reason"] == "query_error"
    assert failed["detail"].startswith("dbt show failed")


def test_column_mode_has_empty_values_everywhere(built_project_dir, capsys):
    argv = ["dim_customers", "-c", "last_name", "-o", "json", "--project-dir", str(built_project_dir)]
    document = json.loads(run_cli(capsys, *argv)[1])
    assert document["run"]["values"] == []
    assert all(node["values"] == {} for node in document["traversals"]["up"]["nodes"])
    assert all(f["values"] == {} for f in document["findings"])
    assert document["traversals"]["down"] is None


def test_schema_accepts_unknown_fields():
    document = build_document(_value_report(), META)
    document["added_in_1_1"] = {"anything": True}
    document["run"]["new_run_field"] = 1
    document["findings"][0]["new_finding_field"] = "x"
    document["traversals"]["up"]["nodes"][0]["new_visit_field"] = None
    DOCUMENT.validate(document)
    EVENT.validate({"event": "warning", "message": "m", "new_event_field": 1})


def test_key_check_catches_unintended_fields():
    document = build_document(_value_report(), META)
    document["findings"][0]["debug"] = True
    with pytest.raises(AssertionError, match="finding"):
        validate_document(document)
