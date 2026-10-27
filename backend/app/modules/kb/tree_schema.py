"""Bounded, constructor-free YAML and deterministic diagnostic DAG validation."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

import yaml
from pydantic import Field, JsonValue, ValidationError, field_validator, model_validator
from yaml.events import (
    AliasEvent,
    DocumentEndEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
    StreamEndEvent,
    StreamStartEvent,
)

from app.modules.kb.import_manifest import Key, StrictModel

MAX_YAML = 262144
MAX_NODES = 200
MAX_EVENTS = 12000
MAX_DEPTH = 12
Commit = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Text = Annotated[str, Field(min_length=1, max_length=4000)]


@dataclass(frozen=True)
class Issue:
    code: str
    # Only schema-owned field names and checked ASCII node keys are exposed.
    node: str | None = None
    field: str | None = None


class TreeRejected(ValueError):
    def __init__(self, code: str, issues: tuple[Issue, ...] = ()) -> None:
        super().__init__(code)
        self.code, self.issues = code, issues


class Option(StrictModel):
    id: Key
    text: Text
    next: Key


class Recommendation(StrictModel):
    item_id: str
    qty: Annotated[int, Field(ge=1, le=999)]

    @field_validator("item_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return checked_uuid(value)


def checked_uuid(value: str) -> str:
    identifier = UUID(value)
    if str(identifier) != value or identifier.int == 0:
        raise ValueError("UUID_INVALID")
    return value


class Node(StrictModel):
    type: Literal["question", "result", "handoff"]
    text: Text
    risk: Literal["observation", "expert_only"]
    source_refs: Annotated[list[str], Field(max_length=20)] = Field(
        default_factory=list
    )
    options: Annotated[list[Option], Field(max_length=10)] = Field(default_factory=list)
    recommendations: Annotated[list[Recommendation], Field(max_length=20)] = Field(
        default_factory=list
    )

    @field_validator("source_refs")
    @classmethod
    def identifiers(cls, values: list[str]) -> list[str]:
        return [checked_uuid(value) for value in values]

    @model_validator(mode="after")
    def permitted_fields(self) -> "Node":
        if self.type != "question" and "options" in self.model_fields_set:
            raise ValueError("TERMINAL_OPTIONS")
        if self.type != "result" and "recommendations" in self.model_fields_set:
            raise ValueError("RECOMMENDATIONS_RESULT_ONLY")
        return self


class TreeInput(StrictModel):
    key: Key
    version: Annotated[int, Field(ge=1, le=2147483647)]
    model_code: Key
    title: Annotated[str, Field(min_length=1, max_length=200)]
    start: Key
    nodes: Annotated[dict[Key, Node], Field(min_length=1, max_length=MAX_NODES)]

    def graph(self) -> dict[str, JsonValue]:
        return {
            "start": self.start,
            "nodes": {
                key: node.model_dump(
                    mode="json",
                    exclude={"source_refs", "recommendations"}
                    | ({"options"} if node.type != "question" else set()),
                )
                for key, node in self.nodes.items()
            },
        }


def _plain(value: str) -> bool:
    return unicodedata.normalize("NFC", value) == value and not any(
        unicodedata.category(char).startswith("C") and char not in "\n\t"
        for char in value
    )


def _yaml_value(data: bytes) -> object:
    """Consume parsing events only: no YAML constructors/object deserialization.

    Every tag, directive, alias and anchor is rejected, including benign ones.
    Scalar resolution is a tiny JSON-like subset; dates/floats stay strings.
    """
    source = data.decode("utf-8", errors="strict")
    if not _plain(source) or source.startswith("\ufeff"):
        raise TreeRejected("UNICODE_INVALID")
    events = iter(yaml.parse(source, Loader=yaml.SafeLoader))
    count = 0

    def event() -> object:
        nonlocal count
        count += 1
        if count > MAX_EVENTS:
            raise TreeRejected("YAML_COMPLEXITY")
        current = next(events)
        if (
            isinstance(current, AliasEvent)
            or getattr(current, "anchor", None) is not None
            or getattr(current, "tag", None) is not None
        ):
            raise TreeRejected("YAML_REFERENCE_OR_TAG")
        return current

    def value(current: object, depth: int) -> object:
        if depth > MAX_DEPTH:
            raise TreeRejected("YAML_DEPTH")
        if isinstance(current, ScalarEvent):
            scalar = current.value
            if len(scalar) > 4000:
                raise TreeRejected("YAML_SCALAR_LIMIT")
            if current.style is None:
                if scalar in {"null", "~", ""}:
                    return None
                if scalar in {"true", "false"}:
                    return scalar == "true"
                if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,10})", scalar):
                    return int(scalar)
            return scalar
        if isinstance(current, MappingStartEvent):
            mapping: dict[str, object] = {}
            while not isinstance(current := event(), MappingEndEvent):
                # Mapping keys must be simple strings; no merge or complex keys.
                if not isinstance(current, ScalarEvent):
                    raise TreeRejected("YAML_KEY_INVALID")
                key = current.value
                if len(key) > 100:
                    raise TreeRejected("YAML_KEY_INVALID")
                if key == "<<" or key in mapping:
                    raise TreeRejected("YAML_DUPLICATE_OR_MERGE_KEY")
                mapping[key] = value(event(), depth + 1)
            return mapping
        if isinstance(current, SequenceStartEvent):
            sequence: list[object] = []
            while not isinstance(current := event(), SequenceEndEvent):
                sequence.append(value(current, depth + 1))
            return sequence
        raise TreeRejected("YAML_INVALID")

    if not isinstance(event(), StreamStartEvent):
        raise TreeRejected("YAML_INVALID")
    start = event()
    if not isinstance(start, DocumentStartEvent) or start.version or start.tags:
        raise TreeRejected("YAML_DIRECTIVE")
    result = value(event(), 0)
    if not isinstance(event(), DocumentEndEvent) or not isinstance(
        event(), StreamEndEvent
    ):
        raise TreeRejected("YAML_MULTIPLE_DOCUMENTS")
    return result


# Conservative signals only. A passing text still requires human expert review.
_ACTIONS = re.compile(
    r"https?\s*:|www\.|[<>`]|\$\(|\$\{|javascript\s*:|data\s*:|"
    r"file\s*:|(?:^|\s)(?:bash|sh|sudo|curl|wget|eval|exec|python)\s+|"
    r"под\s+напряж|вскры|обход\w*\s+защит|внутрен\w*\s+предохран|"
    r"live\s+(?:wire|repair)|bypass|energiz|open\s+(?:the\s+)?housing|"
    r"replace\s+(?:the\s+)?internal\s+fuse",
    re.IGNORECASE,
)


def validate_graph(tree: TreeInput) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    if (
        not tree.title.strip()
        or tree.title != tree.title.strip()
        or not _plain(tree.title)
        or any(c in tree.title for c in "\n\t")
        or _ACTIONS.search(tree.title)
    ):
        issues.append(Issue("TEXT_UNSAFE_OR_EMPTY", field="title"))
    adjacency: dict[str, list[str]] = {}
    for key, node in sorted(tree.nodes.items()):
        if (
            not node.text.strip()
            or not _plain(node.text)
            or _ACTIONS.search(node.text)
            or any(
                not option.text.strip()
                or not _plain(option.text)
                or _ACTIONS.search(option.text)
                for option in node.options
            )
        ):
            issues.append(Issue("TEXT_UNSAFE_OR_EMPTY", key, "text"))
        if node.risk == "expert_only" and node.type != "handoff":
            issues.append(Issue("EXPERT_ONLY_HANDOFF", key, "risk"))
        if node.type in {"question", "result"} and not node.source_refs:
            issues.append(Issue("SOURCE_REQUIRED", key, "source_refs"))
        if node.type == "question" and len(node.options) < 2:
            issues.append(Issue("OPTIONS_REQUIRED", key, "options"))
        if len({option.id for option in node.options}) != len(node.options):
            issues.append(Issue("OPTION_DUPLICATE", key, "options"))
        if len(set(node.source_refs)) != len(node.source_refs):
            issues.append(Issue("SOURCE_DUPLICATE", key, "source_refs"))
        if len({r.item_id for r in node.recommendations}) != len(node.recommendations):
            issues.append(Issue("ITEM_DUPLICATE", key, "recommendations"))
        adjacency[key] = [option.next for option in node.options]
        if any(target not in tree.nodes for target in adjacency[key]):
            issues.append(Issue("TARGET_NOT_FOUND", key, "options"))
    if tree.start not in tree.nodes:
        issues.append(Issue("START_NOT_FOUND", field="start"))
    reachable: set[str] = set()
    pending = [tree.start]
    while pending:
        key = pending.pop()
        if key not in reachable and key in adjacency:
            reachable.add(key)
            pending.extend(adjacency[key])
    issues.extend(
        Issue("UNREACHABLE", key) for key in sorted(tree.nodes.keys() - reachable)
    )
    # Kahn topological sort over ALL nodes catches even unreachable cycles.
    incoming = dict.fromkeys(tree.nodes, 0)
    for targets in adjacency.values():
        for target in targets:
            if target in incoming:
                incoming[target] += 1
    pending = sorted(key for key, count in incoming.items() if count == 0)
    longest = dict.fromkeys(tree.nodes, 0)
    visited = 0
    while pending:
        key = pending.pop()
        visited += 1
        for target in adjacency[key]:
            if target in incoming:
                longest[target] = max(longest[target], longest[key] + 1)
                incoming[target] -= 1
                if incoming[target] == 0:
                    pending.append(target)
    if visited != len(tree.nodes):
        issues.append(Issue("CYCLE", field="nodes"))
    elif max(longest.values()) > 30:
        issues.append(Issue("PATH_LIMIT", field="nodes"))
    # A finite DAG with only questions (>=2 valid outgoing edges) and leaf-only
    # result/handoff necessarily terminates at one of those terminal types.
    return tuple(issues)


def parse_tree(data: bytes) -> TreeInput:
    if not 1 <= len(data) <= MAX_YAML:
        raise TreeRejected("YAML_SIZE")
    try:
        tree = TreeInput.model_validate(_yaml_value(data))
    except TreeRejected:
        raise
    except ValidationError as error:
        # Never expose Pydantic's raw loc/value/message or YAML parser snippets.
        allowed = set(TreeInput.model_fields) | set(Node.model_fields)
        issues = tuple(
            Issue(
                "SCHEMA_FIELD_INVALID",
                field=next((p for p in e["loc"] if p in allowed), None),
            )
            for e in error.errors(include_input=False, include_context=False)[:20]
        )
        raise TreeRejected("SCHEMA_INVALID", issues) from None
    except (yaml.YAMLError, UnicodeError, ValueError, RecursionError, StopIteration):
        raise TreeRejected("YAML_INVALID") from None
    issues = validate_graph(tree)
    if issues:
        raise TreeRejected("GRAPH_INVALID", issues)
    return tree


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
