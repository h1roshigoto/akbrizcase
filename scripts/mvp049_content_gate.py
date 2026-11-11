"""Offline MVP-049 evidence preflight. Never grants human or runtime authority.

Only the private snapshot supplied by the operator is examined. No DB client,
network access, publication, approval generation or runtime-state repair exists.
"""

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "content/pilot/manifest.schema.json").read_bytes())
MAX_JSON = 1024 * 1024
MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
GATES = (
    "MANIFEST_VALIDATION",
    "SYNTHETIC_CONTENT_REHEARSAL",
    "CATALOG_FINAL",
    "IMAGE_RIGHTS",
    "DOCUMENT_PERMISSIONS",
    "APPLICABILITY_EXPERT_REVIEW",
    "TREE_EXPERT_REVIEW",
    "REAL_TOP5_ACCEPTANCE",
    "TARGET_CONTENT_VERIFICATION",
    "PILOT_CONTENT_ADMISSION",
    "PRODUCTION_ADMISSION",
)
DOMAINS = (
    "catalog",
    "images",
    "documents",
    "applicability",
    "trees",
    "search",
    "minimum",
    "state",
    "git_commit_ancestry",
    "yaml_source_provenance",
)
STATES = {"PASS", "FAIL", "BLOCKED", "NOT RUN", "INCONCLUSIVE"}


class Rejected(ValueError):
    """Fixed codes only; never interpolate protected or caller-supplied data."""


def require(condition, code="CONTENT_INVALID"):
    if not condition:
        raise Rejected(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return sha(canonical(value))


def subject(value, scope=None):
    # Inventory bindings are separate from the original immutable service
    # approvals. Lifecycle status is not part of an expert's content decision.
    scoped = {
        "document_review": (
            "code",
            "version",
            "original_sha256",
            "import_sha256",
            "sections_sha256",
            "section_inventory_sha256",
            "sections",
        ),
        "full_file": (
            "code",
            "version",
            "original_sha256",
            "import_sha256",
            "sections_sha256",
            "section_inventory_sha256",
            "sections",
            "full_file_public_allowed",
        ),
        "tree_review": (
            "key",
            "version",
            "model",
            "yaml_sha256",
            "binding_sha256",
            "inventory_sha256",
            "git_commit",
            "start",
            "nodes",
        ),
        "applicability_review": ("item", "model", "section"),
        "disclosure": (
            "item",
            "model",
            "section",
            "review_approval",
            "disclosure_allowed",
        ),
        "search_expectations": ("version", "corpus_sha256"),
    }
    keys = scoped.get(scope, tuple(k for k in value if not k.endswith("_approval")))
    return digest({k: value[k] for k in keys})


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON_INVALID")
        result[key] = value
    return result


def parse(data):
    require(0 < len(data) <= MAX_JSON, "JSON_INVALID")
    try:
        value = json.loads(
            data,
            object_pairs_hook=unique,
            parse_constant=lambda _: require(False, "JSON_INVALID"),
        )
        budget = [0]

        def bounded(node, depth=0):
            budget[0] += 1
            require(depth <= 20 and budget[0] <= 30000, "JSON_INVALID")
            if isinstance(node, dict):
                require(len(node) <= 1000, "JSON_INVALID")
                for k, v in node.items():
                    require(len(k) <= 200, "JSON_INVALID")
                    bounded(v, depth + 1)
            elif isinstance(node, list):
                require(len(node) <= 4096, "JSON_INVALID")
                for v in node:
                    bounded(v, depth + 1)
            elif isinstance(node, str):
                require(
                    len(node) <= 32000
                    and not any(0xD800 <= ord(c) <= 0xDFFF for c in node),
                    "JSON_INVALID",
                )
            elif type(node) is float:
                require(math.isfinite(node), "JSON_INVALID")
            elif type(node) not in (int, bool, type(None)):
                require(False, "JSON_INVALID")

        bounded(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise Rejected("JSON_INVALID") from None


def validate(value, schema, root=SCHEMA):
    """The small closed JSON-schema subset used by the committed v1 schema."""
    if "$ref" in schema:
        return validate(value, root["$defs"][schema["$ref"].split("/")[-1]], root)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(value, option, root)
                return
            except Rejected:
                pass
        raise Rejected("SCHEMA_INVALID")
    if "enum" in schema:
        require(
            any(type(value) is type(v) and value == v for v in schema["enum"]),
            "SCHEMA_INVALID",
        )
    kind = schema.get("type")
    if kind:
        require(
            type(value)
            is {
                "object": dict,
                "array": list,
                "string": str,
                "integer": int,
                "boolean": bool,
                "null": type(None),
            }[kind],
            "SCHEMA_INVALID",
        )
    if kind == "object":
        require(
            set(schema["required"]) <= set(value) <= set(schema["properties"]),
            "SCHEMA_INVALID",
        )
        for k, v in value.items():
            validate(v, schema["properties"][k], root)
    elif kind == "array":
        require(
            schema.get("minItems", 0) <= len(value) <= schema["maxItems"],
            "SCHEMA_INVALID",
        )
        for v in value:
            validate(v, schema["items"], root)
    elif kind == "string":
        require(
            schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 200),
            "SCHEMA_INVALID",
        )
        if "pattern" in schema:
            require(
                re.fullmatch(schema["pattern"], value) is not None, "SCHEMA_INVALID"
            )
    elif kind == "integer":
        require(schema["minimum"] <= value <= schema["maximum"], "SCHEMA_INVALID")


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "SCHEMA_INVALID")


def indexed(values, key):
    result = {}
    for value in values:
        identity = value[key] if isinstance(key, str) else tuple(value[k] for k in key)
        require(identity not in result, "DUPLICATE_ID")
        result[identity] = value
    return result


def directory(path):
    """Walk every absolute component through directory FDs, never resolve links."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "PRIVATE_INPUT_INVALID")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            other = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = other
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def outside_checkout(path, repo):
    # No private packet, even a synthetic one, is read from a Git checkout.
    require(
        not Path(path).is_relative_to(Path(repo).absolute()), "PRIVATE_INPUT_INVALID"
    )


class PrivateInput:
    def __init__(self, path, repo):
        outside_checkout(path, repo)
        self.path = Path(path)
        self.fd = directory(path)
        try:
            info = os.fstat(self.fd)
            require(
                info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "PRIVATE_INPUT_INVALID",
            )
            self.root_identity = (info.st_dev, info.st_ino)
        except BaseException:
            os.close(self.fd)
            raise
        self.opened = []
        self.total = 0

    def read(self, name, checksum=None, limit=MAX_FILE):
        require(
            type(name) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name)
            and name not in {".", ".."},
            "PRIVATE_INPUT_INVALID",
        )
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and 0 < info.st_size <= limit,
                "PRIVATE_INPUT_INVALID",
            )
            chunks, size = [], 0
            while size <= limit:
                part = os.read(fd, min(65536, limit + 1 - size))
                if not part:
                    break
                chunks.append(part)
                size += len(part)
            data = b"".join(chunks)
            self.total += len(data)
            require(len(data) <= limit and self.total <= MAX_TOTAL, "INPUT_TOO_LARGE")
            require(checksum is None or sha(data) == checksum, "CHECKSUM_MISMATCH")
            require(identity(info) == identity(os.fstat(fd)), "INPUT_CHANGED")
            self.opened.append((fd, name, identity(info), sha(data)))
            return data
        except BaseException:
            os.close(fd)
            raise

    def finish(self):
        fd = directory(self.path)
        try:
            info = os.fstat(fd)
            require(
                (info.st_dev, info.st_ino) == self.root_identity
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o700,
                "INPUT_CHANGED",
            )
        finally:
            os.close(fd)
        for fd, name, original, checksum in self.opened:
            require(
                identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False))
                == original
                and identity(os.fstat(fd)) == original,
                "INPUT_CHANGED",
            )
            os.lseek(fd, 0, os.SEEK_SET)
            hasher = hashlib.sha256()
            remaining = original[5]
            while remaining:
                data = os.read(fd, min(65536, remaining))
                require(bool(data), "INPUT_CHANGED")
                hasher.update(data)
                remaining -= len(data)
            require(
                hasher.hexdigest() == checksum
                and not os.read(fd, 1)
                and identity(os.fstat(fd)) == original,
                "INPUT_CHANGED",
            )

    def close(self):
        for fd, *_ in self.opened:
            os.close(fd)
        os.close(self.fd)


def git_bytes(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo),
            *args,
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=20,
    ).stdout


def git(repo, *args):
    return git_bytes(repo, *args).decode("utf-8").strip()


def yaml_provenance(repo, tree, data):
    """Ancestry is not a claim that private YAML bytes were committed."""
    source = tree.get("git_blob")
    if source is None:
        return "INCONCLUSIVE"
    path = source["path"]
    require(
        type(path) is str
        and 0 < len(path) <= 240
        and all(
            re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", p)
            for p in path.split("/")
        ),
        "YAML_PROVENANCE_INVALID",
    )
    # Resolve through the committed tree, not the working directory, a symlink,
    # submodule or an arbitrary caller-selected object in the object database.
    entry = git_bytes(repo, "ls-tree", "-z", tree["git_commit"], "--", path)
    require(
        entry
        in {
            f"{mode} blob {source['sha1']}\t{path}\0".encode("ascii")
            for mode in ("100644", "100755")
        },
        "YAML_PROVENANCE_INVALID",
    )
    size = git(repo, "cat-file", "-s", source["sha1"])
    require(
        size == str(len(data)) and 0 < len(data) <= 262144, "YAML_PROVENANCE_INVALID"
    )
    require(
        git_bytes(repo, "cat-file", "blob", source["sha1"]) == data,
        "YAML_PROVENANCE_INVALID",
    )
    return "PASS"


def lineage(repo, manifest, head, base):
    require(
        all(re.fullmatch("[0-9a-f]{40}", s or "") for s in (head, base)),
        "LINEAGE_MISMATCH",
    )
    require(
        manifest["git"]["head"] == head
        and manifest["git"]["base"] == base
        and git(repo, "rev-parse", "HEAD") == head
        and not git(repo, "status", "--porcelain", "--untracked-files=all"),
        "LINEAGE_MISMATCH",
    )
    try:
        git(repo, "merge-base", "--is-ancestor", base, head)
    except subprocess.CalledProcessError:
        raise Rejected("LINEAGE_MISMATCH") from None


def empty_result():
    return {
        "format": "MVP049_RESULT/v1",
        "gates": {g: ("NOT RUN" if g in GATES[:2] else "BLOCKED") for g in GATES},
        "technical_checks": {d: "NOT RUN" for d in DOMAINS},
        "counts": {},
        "checksums": {},
        "codes": [],
        "MVP043_EXTERNAL": "BLOCKED",
        "MVP047_EXTERNAL": "BLOCKED",
    }


def verify_source(yaml_bytes, tree, items):
    """Reuse the existing bounded parser, never normalize an independent graph.

    Locked backend dependencies are optional for inventory-only use. Without
    them tree validation is NOT RUN, never PASS. No installation/network occurs.
    """
    require(len(yaml_bytes) <= 262144, "YAML_INVENTORY_MISMATCH")
    interpreter = ROOT / "backend/.venv/bin/python"
    if not interpreter.is_file():
        interpreter = Path(sys.executable)
    request = {
        "yaml": base64.b64encode(yaml_bytes).decode("ascii"),
        "items": {item["id"]: item["sku"] for item in items.values()},
        "expected": {
            key: tree[key] for key in ("key", "version", "model", "start", "nodes")
        },
    }
    result = subprocess.run(
        [str(interpreter), "-I", str(ROOT / "scripts/mvp049_source_check.py")],
        input=canonical(request),
        capture_output=True,
        timeout=15,
        check=False,
    )
    require(
        (result.returncode, result.stdout.strip())
        in {(0, b"PASS"), (1, b"FAIL"), (2, b"NOT RUN")},
        "YAML_INVENTORY_MISMATCH",
    )
    return result.stdout.strip().decode("ascii")


def check_content(manifest, artifacts, records):
    content = manifest["content"]
    checks = dict.fromkeys(DOMAINS, "PASS")
    checks["git_commit_ancestry"] = checks["yaml_source_provenance"] = "NOT RUN"
    approvals = indexed(manifest["approvals"], "id")
    synthetic = manifest["classification"] == "SYNTHETIC ONLY"

    def blocked(domain):
        if checks[domain] != "FAIL":
            checks[domain] = "BLOCKED"

    def approval(reference, scope, entity, domain):
        if reference is None:
            blocked(domain)
            return False
        require(reference in approvals, "APPROVAL_BINDING_INVALID")
        row = approvals[reference]
        require(
            row["scope"] == scope and row["subject_sha256"] == subject(entity, scope),
            "APPROVAL_BINDING_INVALID",
        )
        artifact(row["record_artifact"], "approval")
        require(
            row["classification"]
            == ("SYNTHETIC ONLY" if synthetic else "UNVERIFIED EXTERNAL"),
            "SYNTHETIC_ESCALATION",
        )
        return True

    def artifact(identifier, kind, checksum=None):
        require(
            identifier in artifacts and artifacts[identifier]["kind"] == kind,
            "ARTIFACT_BINDING_INVALID",
        )
        if checksum:
            require(artifacts[identifier]["sha256"] == checksum, "CHECKSUM_MISMATCH")
        return records[identifier]

    models = indexed(content["catalog"]["models"], "code")
    items = indexed(content["catalog"]["items"], "sku")
    require(
        bool(models) and {i["kind"] for i in items.values()} >= {"product", "service"},
        "CATALOG_INCOMPLETE",
    )
    for model in models.values():
        approval(model["business_approval"], "business", model, "catalog")
    for item in items.values():
        require(
            (item["price_mode"] == "fixed") == (item["price"] is not None)
            and (item["kind"] != "service" or not item["requires_shipping"]),
            "BUSINESS_PARAMETERS_INVALID",
        )
        approval(item["business_approval"], "business", item, "catalog")
    require(len({item["id"] for item in items.values()}) == len(items), "DUPLICATE_ID")
    images = indexed(content["images"], "id")
    require(
        len({i["item"] for i in images.values()}) == len(images)
        and {i["item"] for i in images.values()} == set(items),
        "IMAGE_BINDING_INVALID",
    )
    for image in images.values():
        require(
            image["revision"] == items[image["item"]]["revision"], "REVISION_MISMATCH"
        )
        if image["original_artifact"] is None:
            require(
                image["original_sha256"] is image["output_sha256"] is None
                and image["pipeline"] == "ABSENT"
                and image["rights_approval"] is None
                and image["output_artifact"] is image["pipeline_artifact"] is None,
                "IMAGE_BINDING_INVALID",
            )
        else:
            require(
                image["original_sha256"] is not None and image["pipeline"] != "ABSENT",
                "IMAGE_BINDING_INVALID",
            )
            artifact(image["original_artifact"], "original", image["original_sha256"])
            if image["pipeline"] == "VERIFIED":
                require(image["output_sha256"] is not None, "IMAGE_BINDING_INVALID")
                artifact(image["output_artifact"], "original", image["output_sha256"])
                observed = parse(
                    artifact(image["pipeline_artifact"], "image_observation")
                )
                require(
                    canonical(observed)
                    == canonical(
                        {
                            "format": "MVP049_IMAGE_OBSERVATION/v1",
                            "classification": manifest["classification"],
                            **{
                                k: image[k]
                                for k in (
                                    "item",
                                    "revision",
                                    "original_sha256",
                                    "output_sha256",
                                )
                            },
                            "original_scan": "PASS",
                            "output_scan": "PASS",
                            "decoder": "PASS",
                        }
                    ),
                    "IMAGE_BINDING_INVALID",
                )
            if image["pipeline"] != "VERIFIED":
                checks["images"] = "FAIL" if image["pipeline"] == "FAIL" else "NOT RUN"
            approval(image["rights_approval"], "image_rights", image, "images")
    documents = indexed(content["documents"], ("code", "version"))
    sections = {}
    require(
        len({d["code"] for d in documents.values() if d["status"] == "published"})
        == sum(d["status"] == "published" for d in documents.values()),
        "PUBLICATION_CONFLICT",
    )
    for doc in documents.values():
        artifact(doc["original_artifact"], "original", doc["original_sha256"])
        artifact(doc["import_artifact"], "import", doc["import_sha256"])
        require(
            digest(doc["sections"]) == doc["section_inventory_sha256"],
            "CHECKSUM_MISMATCH",
        )
        imported = parse(records[doc["import_artifact"]])
        require(
            type(imported) is dict
            and set(imported)
            <= {
                "code",
                "title",
                "kind",
                "visibility",
                "version",
                "checksum_sha256",
                "preparation",
                "sections",
            }
            and all(imported.get(k) == doc[k] for k in ("code", "version"))
            and type(imported.get("version")) is int
            and imported.get("visibility") in {"public", "internal"}
            and imported.get("checksum_sha256") == doc["original_sha256"]
            and imported.get("kind") in {"manual", "regulation", "article", "other"}
            and type(imported.get("title")) is str
            and imported.get("preparation", "manual") == "manual"
            and type(imported.get("sections")) is list,
            "IMPORT_BINDING_INVALID",
        )
        imported_sections = indexed(imported["sections"], "section_key")
        require(
            set(imported_sections) == {s["key"] for s in doc["sections"]},
            "IMPORT_BINDING_INVALID",
        )
        for section in doc["sections"]:
            require(section["id"] not in sections, "DUPLICATE_ID")
            artifact(section["artifact"], "section", section["sha256"])
            imported_section = imported_sections[section["key"]]
            require(
                set(imported_section)
                <= {
                    "section_key",
                    "title",
                    "text",
                    "page_from",
                    "page_to",
                    "assistant_allowed",
                }
                and type(imported_section.get("title")) is str
                and 0 < len(imported_section["title"]) <= 200
                and type(imported_section.get("text")) is str
                and 0 < len(imported_section["text"]) <= 32000
                and all(
                    v is None or (type(v) is int and 1 <= v <= 100)
                    for v in (
                        imported_section.get("page_from"),
                        imported_section.get("page_to"),
                    )
                )
                and (
                    (
                        imported_section.get("page_from") is None
                        and imported_section.get("page_to") is None
                    )
                    or (
                        imported_section.get("page_from") is not None
                        and imported_section.get("page_to") is not None
                        and imported_section["page_from"] <= imported_section["page_to"]
                    )
                )
                and type(imported_section.get("assistant_allowed", False)) is bool
                and imported_section.get("assistant_allowed", False)
                == section["assistant_allowed"]
                and imported_section["text"].encode("utf-8")
                == records[section["artifact"]],
                "IMPORT_BINDING_INVALID",
            )
            sections[section["id"]] = (doc, section)
        if doc["status"] in {"published", "retired"}:
            approval(doc["review_approval"], "document_review", doc, "documents")
            if doc["visibility"] == "public":
                if not doc["full_file_public_allowed"]:
                    blocked("documents")
                approval(doc["full_file_approval"], "full_file", doc, "documents")
        require(
            doc["full_file_public_allowed"] or doc["full_file_approval"] is None,
            "PERMISSION_CONFLICT",
        )

    def valid_section(identifier, public=False):
        require(identifier in sections, "SOURCE_REF_INVALID")
        doc, section = sections[identifier]
        return (
            doc["document_status"] == "active"
            and doc["status"] == "published"
            and (
                not public
                or (
                    doc["visibility"] == "public"
                    and section["assistant_allowed"]
                    and doc["full_file_public_allowed"]
                )
            )
        )

    pairs = indexed(content["applicability"], ("item", "model"))
    confirmed = set()
    for pair, link in pairs.items():
        item, model = pair
        require(item in items and model in models, "APPLICABILITY_BINDING_INVALID")
        reviewed = approval(
            link["review_approval"], "applicability_review", link, "applicability"
        )
        disclosed = (
            approval(link["disclosure_approval"], "disclosure", link, "applicability")
            if link["disclosure_allowed"]
            else False
        )
        require(
            link["disclosure_allowed"] or link["disclosure_approval"] is None,
            "PERMISSION_CONFLICT",
        )
        admissible = (
            valid_section(link["section"])
            and reviewed
            and disclosed
            and items[item]["status"] == models[model]["status"] == "published"
        )
        require(
            link["status"] == ("confirmed" if admissible else "unknown"),
            "APPLICABILITY_STATUS_INVALID",
        )
        allowed_sources = (
            [link["section"]]
            if admissible and valid_section(link["section"], public=True)
            else []
        )
        require(link["public_sources"] == allowed_sources, "INTERNAL_SOURCE_DISCLOSURE")
        if admissible:
            confirmed.add(pair)
        else:
            blocked("applicability")

    trees = indexed(content["trees"], ("key", "version"))
    published = []
    require(
        len({t["key"] for t in trees.values() if t["status"] == "published"})
        == sum(t["status"] == "published" for t in trees.values()),
        "PUBLICATION_CONFLICT",
    )
    require(
        all(
            len(
                {
                    other["model"]
                    for other in trees.values()
                    if other["key"] == tree["key"]
                }
            )
            == 1
            for tree in trees.values()
        ),
        "TREE_BINDING_INVALID",
    )
    for tree in trees.values():
        require(tree["model"] in models, "TREE_BINDING_INVALID")
        yaml_bytes = artifact(tree["yaml_artifact"], "yaml", tree["yaml_sha256"])
        source_status = verify_source(yaml_bytes, tree, items)
        require(source_status != "FAIL", "YAML_INVENTORY_MISMATCH")
        if source_status == "NOT RUN" and checks["trees"] == "PASS":
            checks["trees"] = "NOT RUN"
        binding = digest(
            {k: tree[k] for k in ("key", "version", "model", "start", "nodes")}
        )
        require(binding == tree["inventory_sha256"], "TREE_BINDING_INVALID")
        nodes = indexed(tree["nodes"], "id")
        paths, visited = [], set()

        def walk(
            identifier,
            path,
            nodes=nodes,
            visited=visited,
            paths=paths,
            model=tree["model"],
        ):
            require(
                identifier in nodes and identifier not in path and len(path) <= 30,
                "TREE_PATH_INVALID",
            )
            node = nodes[identifier]
            visited.add(identifier)
            require(
                (node["type"] == "question" and 2 <= len(node["next"]) <= 20)
                or (node["type"] in {"result", "handoff"} and not node["next"]),
                "TREE_PATH_INVALID",
            )
            require(len(set(node["next"])) == len(node["next"]), "TREE_PATH_INVALID")
            if node["type"] != "handoff":
                require(bool(node["source_refs"]), "SOURCE_REF_INVALID")
            for source in node["source_refs"]:
                require(valid_section(source, public=True), "TREE_DEPENDENCY_INVALID")
            for item in node["recommendations"]:
                require((item["item"], model) in confirmed, "TREE_DEPENDENCY_INVALID")
            path = [*path, identifier]
            if not node["next"]:
                paths.append(path)
                require(len(paths) <= 4096, "TREE_PATH_INVALID")
            for child in node["next"]:
                walk(child, path)

        walk(tree["start"], [])
        require(visited == set(nodes), "TREE_PATH_INVALID")
        if tree["paths_artifact"] is None:
            blocked("trees")
        else:
            proof = parse(artifact(tree["paths_artifact"], "paths"))
            fields(proof, "format yaml_sha256 binding_sha256 inventory_sha256 paths")
            require(
                proof
                == {
                    "format": "MVP049_PATHS/v1",
                    "yaml_sha256": tree["yaml_sha256"],
                    "binding_sha256": tree["binding_sha256"],
                    "inventory_sha256": binding,
                    "paths": paths,
                },
                "TREE_PATH_INVALID",
            )
        approval(tree["review_approval"], "tree_review", tree, "trees")
        if tree["status"] == "published":
            require(
                models[tree["model"]]["status"] == "published",
                "TREE_DEPENDENCY_INVALID",
            )
            published.append(tree)
        else:
            blocked("trees")
    minimum = content["minimum"]
    require(
        len(set(minimum["models"])) == len(minimum["models"])
        and set(minimum["models"]) <= set(models),
        "MINIMUM_INVALID",
    )
    approval(minimum["agreement_approval"], "minimum", minimum, "minimum")
    if len(published) < minimum["scenarios"] or not set(minimum["models"]) <= {
        t["model"] for t in published
    }:
        blocked("minimum")
    search = content["search"]
    expectations_present = approval(
        search["expectations_approval"], "search_expectations", search, "search"
    )
    corpus_bytes = artifact(
        search["corpus_artifact"], "corpus", search["corpus_sha256"]
    )
    corpus = parse(corpus_bytes)
    fields(corpus, "format version classification queries")
    require(
        corpus["format"] == "MVP049_CORPUS/v1"
        and corpus["version"] == search["version"]
        and corpus["classification"] == manifest["classification"],
        "CORPUS_INVALID",
    )
    queries = corpus["queries"]
    require(type(queries) is list and len(queries) <= 500, "CORPUS_INVALID")
    query_map, bags = {}, set()
    for q in queries:
        fields(q, "id text group expected_sections behavior safe_sections")
        require(
            type(q["id"]) is str
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", q["id"])
            and q["id"] not in query_map
            and type(q["text"]) is str
            and 0 < len(q["text"]) <= 2000,
            "CORPUS_INVALID",
        )
        bag = tuple(sorted(re.findall(r"\w+", q["text"].casefold())))
        require(bag and bag not in bags, "CORPUS_INVALID")
        bags.add(bag)
        require(
            q["group"] in {"relevant", "negative"}
            and q["behavior"] in {"results", "no_match", "safe_quote", "handoff"},
            "CORPUS_INVALID",
        )
        for field in ("expected_sections", "safe_sections"):
            require(
                type(q[field]) is list
                and len(q[field]) <= 100
                and all(type(s) is str for s in q[field])
                and len(set(q[field])) == len(q[field]),
                "CORPUS_INVALID",
            )
            require(
                all(valid_section(s, public=True) for s in q[field]), "CORPUS_INVALID"
            )
        require(
            (
                q["group"] == "relevant"
                and q["behavior"] == "results"
                and bool(q["expected_sections"])
                and not q["safe_sections"]
            )
            or (
                q["group"] == "negative"
                and not q["expected_sections"]
                and q["behavior"] != "results"
                and (q["behavior"] == "safe_quote") == bool(q["safe_sections"])
            ),
            "CORPUS_INVALID",
        )
        query_map[q["id"]] = q
    relevant = sum(q["group"] == "relevant" for q in queries)
    require(relevant >= 20 and len(queries) - relevant >= 20, "CORPUS_INCOMPLETE")
    if search["measurement_artifact"] is None:
        checks["search"] = "NOT RUN" if expectations_present else "BLOCKED"
    else:
        measurement = parse(artifact(search["measurement_artifact"], "measurement"))
        fields(measurement, "format head corpus_sha256 rows")
        require(
            measurement["format"] == "MVP049_MEASUREMENT/v1"
            and measurement["head"] == manifest["git"]["head"]
            and measurement["corpus_sha256"] == sha(corpus_bytes)
            and type(measurement["rows"]) is list,
            "MEASUREMENT_INVALID",
        )
        rows = indexed(measurement["rows"], "id")
        require(set(rows) == set(query_map), "MEASUREMENT_INCOMPLETE")
        hits = 0
        for qid, row in rows.items():
            fields(row, "id returned_sections total resolution execution acl_incidents")
            require(
                row["execution"] == "PASS"
                and type(row["acl_incidents"]) is int
                and row["acl_incidents"] == 0
                and type(row["total"]) is int
                and 0 <= row["total"] <= 2147483647
                and type(row["returned_sections"]) is list
                and len(row["returned_sections"]) == min(5, row["total"])
                and all(type(s) is str for s in row["returned_sections"])
                and len(set(row["returned_sections"])) == len(row["returned_sections"])
                and all(
                    valid_section(s, public=True) for s in row["returned_sections"]
                ),
                "MEASUREMENT_INVALID",
            )
            q = query_map[qid]
            returned = row["returned_sections"]
            if q["group"] == "relevant":
                require(
                    row["resolution"] == ("results" if returned else "no_match"),
                    "MEASUREMENT_INVALID",
                )
                hits += bool(set(returned) & set(q["expected_sections"]))
            else:
                require(
                    (
                        q["behavior"] == "no_match"
                        and row["total"] == 0
                        and row["resolution"] == "no_match"
                    )
                    or (
                        q["behavior"] == "safe_quote"
                        and bool(returned)
                        and set(returned) <= set(q["safe_sections"])
                        and row["resolution"] == "results"
                    )
                    or (
                        q["behavior"] == "handoff"
                        and row["resolution"] == "handoff"
                        and not returned
                    ),
                    "NEGATIVE_SAFETY_FAILED",
                )
        require(hits * 5 >= relevant * 4, "TOP5_FAILED")
    # Validate all approval references, including unused records, so test-only
    # attestations cannot hide behind a REAL UNVERIFIED manifest label.
    for row in approvals.values():
        artifact(row["record_artifact"], "approval")
        require(
            row["classification"]
            == ("SYNTHETIC ONLY" if synthetic else "UNVERIFIED EXTERNAL"),
            "SYNTHETIC_ESCALATION",
        )
    return checks


def evaluate(packet, filename, expected_sha, repo, head, base):
    raw = packet.read(filename, expected_sha, MAX_JSON)
    manifest = parse(raw)
    validate(manifest, SCHEMA)
    lineage(repo, manifest, head, base)
    for source_commit in {t["git_commit"] for t in manifest["content"]["trees"]}:
        git(repo, "merge-base", "--is-ancestor", source_commit, head)
    artifacts = indexed(manifest["artifacts"], "id")
    require(
        len({a["filename"] for a in artifacts.values()}) == len(artifacts)
        and filename not in {a["filename"] for a in artifacts.values()},
        "DUPLICATE_ID",
    )
    records = {
        key: packet.read(a["filename"], a["sha256"]) for key, a in artifacts.items()
    }
    previous = manifest["git"]["previous_manifest_sha256"]
    if previous is not None:
        require(
            "previous-manifest" in artifacts
            and artifacts["previous-manifest"]["sha256"] == previous
            and artifacts["previous-manifest"]["kind"] == "previous",
            "LINEAGE_MISMATCH",
        )
        old = parse(records["previous-manifest"])
        validate(old, SCHEMA)
        require(
            old["release_id"] == manifest["release_id"] and old["git"]["base"] == base,
            "LINEAGE_MISMATCH",
        )
        git(repo, "merge-base", "--is-ancestor", old["git"]["head"], head)
    result = empty_result()
    result["technical_checks"] = check_content(manifest, artifacts, records)
    trees = manifest["content"]["trees"]
    if trees:
        # The ancestor checks above concern only Git metadata. Byte provenance
        # needs an independently checked commit/path/blob binding as well.
        result["technical_checks"]["git_commit_ancestry"] = "PASS"
        provenance = [
            yaml_provenance(repo, tree, records[tree["yaml_artifact"]])
            for tree in trees
        ]
        result["technical_checks"]["yaml_source_provenance"] = (
            "PASS" if all(p == "PASS" for p in provenance) else "INCONCLUSIVE"
        )
    result["gates"]["MANIFEST_VALIDATION"] = "PASS"
    result["checksums"] = {
        "manifest": sha(raw),
        "content": digest(manifest["content"]),
        "head": head,
        "base": base,
    }
    result["counts"] = {
        key: len(manifest["content"][key])
        for key in ("images", "documents", "applicability", "trees")
    }
    result["counts"].update(
        {key: len(manifest["content"]["catalog"][key]) for key in ("models", "items")}
    )
    state_id = manifest["state_artifact"]
    if state_id is None:
        result["technical_checks"]["state"] = "NOT RUN"
    else:
        require(
            state_id in artifacts and artifacts[state_id]["kind"] == "state",
            "ARTIFACT_BINDING_INVALID",
        )
        state = parse(records[state_id])
        fields(state, "format head classification content")
        validate(state["content"], SCHEMA["$defs"]["content"])
        require(
            state["format"] == "MVP049_STATE/v1"
            and state["head"] == head
            and state["classification"] == manifest["classification"]
            and canonical(state["content"]) == canonical(manifest["content"]),
            "STATE_MISMATCH",
        )
    rehearsal_id = manifest["rehearsal_artifact"]
    if rehearsal_id is not None:
        from mvp049_rehearsal import validate_receipt

        require(
            manifest["classification"] == "SYNTHETIC ONLY"
            and rehearsal_id in artifacts
            and artifacts[rehearsal_id]["kind"] == "rehearsal",
            "SYNTHETIC_ESCALATION",
        )
        validate_receipt(parse(records[rehearsal_id]), repo, head)
        result["gates"]["SYNTHETIC_CONTENT_REHEARSAL"] = "INCONCLUSIVE"
    # Offline JSON cannot prove real people, rights, independent provenance or
    # freshness of target DB reads. There is deliberately no PASS branch here.
    result["codes"] = ["INDEPENDENT_VERIFICATION_REQUIRED", "MVP050_REQUIRED"]
    if rehearsal_id is not None:
        result["codes"].append("UNVERIFIED_REHEARSAL_RECEIPT")
    if result["technical_checks"]["yaml_source_provenance"] == "INCONCLUSIVE":
        result["codes"].append("YAML_PROVENANCE_UNVERIFIED")
    if manifest["classification"] == "SYNTHETIC ONLY":
        result["codes"].append("SYNTHETIC_ONLY")
    packet.finish()
    lineage(repo, manifest, head, base)
    return result


def private_output(path, result, repo, input_root):
    outside_checkout(path, repo)
    require(not Path(path).is_relative_to(Path(input_root)), "OUTPUT_INVALID")
    parent = directory(Path(path).parent)
    try:
        os.mkdir(Path(path).name, 0o700, dir_fd=parent)
        fd = os.open(
            Path(path).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            os.fchmod(fd, 0o700)
            output = os.open(
                "result.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            with os.fdopen(output, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(canonical(result) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise Rejected("ARGUMENT_INVALID")


def main(argv=None):
    packet = None
    result = empty_result()
    try:
        parser = QuietParser(description=__doc__)
        parser.add_argument("operation", choices=("check", "report"))
        parser.add_argument("--input-root", required=True)
        parser.add_argument("--manifest", default="manifest.json")
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--expected-head", required=True)
        parser.add_argument("--expected-base", required=True)
        parser.add_argument("--repo", default=str(ROOT))
        parser.add_argument("--output-dir")
        args = parser.parse_args(argv)
        require(
            re.fullmatch("[0-9a-f]{64}", args.expected_sha256) is not None,
            "ARGUMENT_INVALID",
        )
        require(
            (args.operation == "report") == (args.output_dir is not None),
            "ARGUMENT_INVALID",
        )
        packet = PrivateInput(args.input_root, args.repo)
        result = evaluate(
            packet,
            args.manifest,
            args.expected_sha256,
            args.repo,
            args.expected_head,
            args.expected_base,
        )
        if args.operation == "report":
            private_output(args.output_dir, result, args.repo, args.input_root)
        print("MVP049_RESULT " + canonical(result).decode())
        # 0 = manifest/evidence validation, NOT pilot admission.
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
        subprocess.SubprocessError,
    ):
        # Never print exception text, parser values, private paths or tracebacks.
        result = empty_result()
        result["gates"]["MANIFEST_VALIDATION"] = "FAIL"
        result["codes"] = ["PREFLIGHT_REJECTED"]
        print("MVP049_RESULT " + canonical(result).decode())
        return 1
    finally:
        if packet is not None:
            packet.close()


if __name__ == "__main__":
    sys.exit(main())
