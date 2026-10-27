"""Synthetic adversarial YAML/graph corpus, without database or UI."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.kb.cli import main
from app.modules.kb.tree_schema import MAX_YAML, TreeRejected, parse_tree


def sample() -> dict[str, object]:
    source = str(uuid4())
    return {
        "key": "SYNTHETIC027",
        "version": 1,
        "model_code": "SYNTHETIC027",
        "title": "SYNTHETIC ONLY",
        "start": "question",
        "nodes": {
            "question": {
                "type": "question",
                "risk": "observation",
                "text": "Synthetic?",
                "source_refs": [source],
                "options": [
                    {"id": "a", "text": "Synthetic A", "next": "result"},
                    {"id": "b", "text": "Synthetic B", "next": "handoff"},
                ],
            },
            "result": {
                "type": "result",
                "risk": "observation",
                "text": "Synthetic result",
                "source_refs": [source],
                "recommendations": [{"item_id": str(uuid4()), "qty": 1}],
            },
            "handoff": {
                "type": "handoff",
                "risk": "expert_only",
                "text": "SYNTHETIC ONLY: contact a specialist.",
            },
        },
    }


def test_valid_and_normalized() -> None:
    tree = parse_tree(json.dumps(sample()).encode())
    graph = json.dumps(tree.graph())
    assert "source_refs" not in graph and "recommendations" not in graph
    assert tree.nodes["handoff"].risk == "expert_only"


@pytest.mark.parametrize(
    "data",
    [
        b"key: one\nkey: two",
        b"{nodes: {a: {text: x, text: y}}}",
        b"!!python/object/apply:os.system ['echo synthetic']",
        b"!!str synthetic",
        b"a: &a [1]\nb: [*a,*a]",
        b"a: &a [*a]",
        b"a: &a [1]\nb: &b [*a,*a]\nc: [*b,*b]",
        b"{<<: {key: x}}",
        b"---\n{}\n---\n{}",
        b"%YAML 1.1\n---\n{}",
        b"\xff",
        b"\xef\xbb\xbf{}",
        b"a: \x00",
        b"a: [" * 20 + b"]" * 20,
        b"x" * (MAX_YAML + 1),
        b"",
        b"a: " + b"[" * 20 + b"0" + b"]" * 20,
        b"[? [x, y]: z]",
        b"a: " + b"x" * 4001,
    ],
)
def test_unsafe_yaml(data: bytes) -> None:
    with pytest.raises(TreeRejected) as caught:
        parse_tree(data)
    assert "echo synthetic" not in str(caught.value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("start",), None),
        (("start",), "missing"),
        (("version",), True),
        (("version",), 0),
        (("version",), "1"),
        (("model_code",), ""),
        (("nodes", "question", "type"), "info"),
        (("nodes", "question", "risk"), "safe"),
        (("nodes", "question", "risk"), "expert_only"),
        (("nodes", "result", "risk"), "expert_only"),
        (("nodes", "question", "source_refs"), []),
        (("nodes", "result", "source_refs"), []),
        (("nodes", "result", "options"), []),
        (("nodes", "handoff", "recommendations"), []),
        (("nodes", "question", "action"), "shell"),
        (("nodes", "question", "text"), "<script>synthetic</script>"),
        (("nodes", "question", "text"), "https://example.invalid"),
        (("nodes", "question", "text"), "$(echo synthetic)"),
        (("nodes", "question", "text"), "Вскрыть оборудование"),
        (("nodes", "question", "text"), "Bypass protection"),
        (("nodes", "question", "text"), "Synthetic\u202ereversed"),
        (("title",), " "),
        (("title",), "leading "),
        (("title",), "e\u0301"),
        (("nodes", "result", "recommendations"), [{"item_id": str(uuid4()), "qty": 0}]),
        (
            ("nodes", "result", "recommendations"),
            [{"item_id": str(uuid4()), "qty": True}],
        ),
        (
            ("nodes", "result", "recommendations"),
            [{"item_id": str(uuid4()), "qty": 1.5}],
        ),
        (
            ("nodes", "result", "recommendations"),
            [{"item_id": str(uuid4()), "qty": 1000}],
        ),
        (("nodes", "result", "source_refs"), ["https://example.invalid"]),
        (
            ("nodes", "result", "source_refs"),
            [{"section_id": str(uuid4()), "document_id": str(uuid4())}],
        ),
    ],
)
def test_invalid_schema_or_graph(path: tuple[str, ...], value: object) -> None:
    data = sample()
    current = data
    for key in path[:-1]:
        child = current[key]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value
    with pytest.raises(TreeRejected):
        parse_tree(json.dumps(data).encode())


@pytest.mark.parametrize(
    "case",
    [
        "missing_start",
        "unreachable",
        "cycle",
        "target",
        "one_option",
        "duplicate_option",
        "duplicate_source",
        "duplicate_item",
        "too_many_nodes",
        "long_path",
    ],
)
def test_graph_invariants(case: str) -> None:
    data = sample()
    nodes = data["nodes"]
    assert isinstance(nodes, dict)
    question = nodes["question"]
    if case == "missing_start":
        del data["start"]
    elif case == "unreachable":
        nodes["extra"] = nodes["handoff"]
    elif case in {"cycle", "target"}:
        question["options"][0]["next"] = "question" if case == "cycle" else "missing"
    elif case == "one_option":
        question["options"].pop()
    elif case == "duplicate_option":
        question["options"][1]["id"] = "a"
    elif case == "duplicate_source":
        question["source_refs"] *= 2
    elif case == "duplicate_item":
        nodes["result"]["recommendations"] *= 2
    elif case == "too_many_nodes":
        nodes.update({"n" + str(i): nodes["handoff"] for i in range(201)})
    else:
        data["start"] = "n0"
        data["nodes"] = {"end": nodes["handoff"]}
        chain = data["nodes"]
        assert isinstance(chain, dict)
        for i in range(31):
            chain["n" + str(i)] = {
                **question,
                "options": [
                    {
                        "id": "a",
                        "text": "A",
                        "next": "n" + str(i + 1) if i < 30 else "end",
                    },
                    {"id": "b", "text": "B", "next": "end"},
                ],
            }
    with pytest.raises(TreeRejected):
        parse_tree(json.dumps(data).encode())


def test_content_examples() -> None:
    root = Path(__file__).resolve().parents[2] / "content" / "trees"
    for path in (root / "valid").glob("*.yaml"):
        parse_tree(path.read_bytes())
    for path in (root / "invalid").glob("*.yaml"):
        with pytest.raises(TreeRejected):
            parse_tree(path.read_bytes())


@pytest.mark.parametrize(
    "option", ["--actor-id", "--skip-validation", "--publish", "--password"]
)
def test_cli_rejects_bypasses_without_echo(
    option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "tree-import",
                "--file",
                "synthetic.yaml",
                "--git-commit",
                "a" * 40,
                option,
                "SYNTHETIC-SECRET",
            ]
        )
    captured = capsys.readouterr()
    assert "SYNTHETIC-SECRET" not in captured.err + captured.out
