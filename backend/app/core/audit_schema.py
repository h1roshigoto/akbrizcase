"""Closed schemas for new ORM audit writes; historical evidence is never rewritten.

Expert attribution is purpose-bound evidence, still personal data. It is allowed
only in the existing content/applicability evidence events, never runtime logs.
"""

import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

STAFF_ACTIONS = frozenset("bootstrap create disable login logout access csrf".split())
STAFF_RESULTS = frozenset(
    """
success authentication_denied forbidden bootstrap_closed invalid_input
login_exists not_found csrf_denied origin_denied
    """.split()
)
CONTENT_CODES = frozenset(
    """AUTHENTICATION_DENIED FORBIDDEN OPERATION_INVALID
NOT_FOUND VERSION_REQUIRED DRAFT_REQUIRED PUBLISHED_REQUIRED RETIRED_REQUIRED
DOCUMENT_REVOKED REVIEW_REQUIRED REVIEW_STALE REVIEW_IMMUTABLE REVIEW_INVALID
EVIDENCE_REQUIRED EVIDENCE_INVALID PUBLIC_APPROVAL_REQUIRED PUBLIC_APPROVAL_STALE
VISIBILITY_INVALID OPERATOR_IO_FAILURE FILE_CHANGED FILE_METADATA_CONFLICT
SCANNER_FAILURE SCANNER_INFECTED SCANNER_UNCONFIRMED SIGNATURES_CHANGED
SIGNATURES_INVALID SIGNATURES_STALE SIGNATURES_UNAVAILABLE FILE_EXTENSION
FILE_FORMAT FILE_KEY FILE_MIME FILE_NAME FILE_SIZE STORAGE_CONFIG STORAGE_FAILURE
STORAGE_FILE STORAGE_PERMISSIONS PROCESS_OUTPUT_LIMIT PROCESS_TIMEOUT
PROCESS_UNAVAILABLE PARSER_REJECTED PARSER_TASK PDF_SIGNATURE PDF_STRUCTURE
TEXT_ACTIVE TEXT_BINARY TEXT_ENCODING TEXT_UNSAFE_OR_EMPTY UNICODE_INVALID
MANIFEST_CHANGED MANIFEST_INVALID OPERATOR_FILE_INVALID CHECKSUM_CONFLICT
DOCUMENT_CONFLICT DRAFT_CONFLICT DRAFT_NOT_FOUND DRAFT_REVIEW_CONFLICT
SECTIONS_CONFLICT SECTIONS_INVALID SECTIONS_REQUIRED SECTION_DUPLICATE
PREPARATION_CONFLICT PREPARATION_INCOMPLETE IMPORTED_DRAFT READY_FOR_MANUAL_REVIEW
NO_OP MODEL_CONFLICT MODEL_NOT_FOUND MODEL_NOT_PUBLIC TREE_NOT_FOUND TREE_REVOKED
YAML_REQUIRED YAML_CONFLICT YAML_CHANGED YAML_COMPLEXITY YAML_DEPTH YAML_DIRECTIVE
YAML_DUPLICATE_OR_MERGE_KEY YAML_INVALID YAML_KEY_INVALID YAML_MULTIPLE_DOCUMENTS
YAML_REFERENCE_OR_TAG YAML_SCALAR_LIMIT YAML_SIZE SCHEMA_INVALID SCHEMA_FIELD_INVALID
IDENTIFIER_INVALID APPROVAL_DATE_INVALID EXPERT_INVALID CONTENT_REVIEW_REQUIRED
EXPERT_REVIEW_REQUIRED SOURCE_NOT_FOUND SOURCE_NOT_PUBLIC SOURCE_REQUIRED
SOURCE_DUPLICATE ITEM_NOT_FOUND ITEM_DUPLICATE APPLICABILITY_NOT_CONFIRMED
PUBLICATION_BLOCKED DEPENDENCIES_CHANGED REVIEWED PUBLISHED RETIRED REVOKED
PREVIEW CANDIDATE REPLACED CANDIDATE_EXISTS CANDIDATE_CHANGED
EVIDENCE_UNAVAILABLE STRUCTURALLY_VALID_DRAFT GRAPH_INVALID DUPLICATE_KEY
EMPTY_FIELD CYCLE UNREACHABLE START_NOT_FOUND TERMINAL_OPTIONS OPTIONS_REQUIRED
OPTION_DUPLICATE RECOMMENDATIONS_RESULT_ONLY PATH_LIMIT
CATALOG_CHANGED COMMIT_INVALID EXPERT_ONLY_HANDOFF EXTRACTION_FAILED
IMAGE_FORMAT IMAGE_KEY IMAGE_NOT_FOUND IMAGE_OUTPUT MANUAL_TEXT_REQUIRED
PAGES_INVALID SECTION_REQUIRED STORED_DATA_CONFLICT STREAM_FAILURE
TARGET_NOT_FOUND""".split()
)
STATUSES = frozenset(
    """
draft published archived active retired revoked new in_progress closed
cancelled pending sending sent failed
    """.split()
)
BINDING = frozenset(
    """
item_id model_id evidence_section_id expert_name review_reference
approved_at
    """.split()
)
APPLICABILITY = (
    BINDING
    | {"before_" + k for k in BINDING}
    | frozenset(
        """
before_public_allowed after_public_allowed published_by published_at
revision review_event_id publication_reference before_published_by
before_published_at after_published_by after_published_at reason
""".split()
    )
)
REVIEW = frozenset(
    """
version_id checksum_sha256 file_binding_sha256 sections_sha256 expert_name
expert_reference expert_approved_on decision content_checked
full_file_public_allowed public_release_reference approved_at
""".split()
)
TREE = frozenset(
    """
issue_codes blocker_codes binding_sha256 from_status to_status status
yaml_checksum publication_blockers source_count recommendation_count
source_ids item_ids model_id published_ids replacement_id expert_name
review_reference approved_at published_by published_at
""".split()
)


def _schema(action: str) -> tuple[set[str], set[str], frozenset[str]]:
    family, _, operation = action.partition(".")
    if family == "staff" and operation in STAFF_ACTIONS:
        return {"staff_user"}, {"role", "is_active"}, STAFF_RESULTS
    if action == "order.status.change":
        return (
            {"order"},
            set("from_status to_status from_version to_version".split()),
            frozenset({"success"}),
        )
    if action == "notification.retry":
        return {"email_outbox"}, {"from_status", "to_status"}, frozenset({"success"})
    if family in {"catalog_item", "equipment_model"} and operation in {
        "create",
        "edit",
        "publish",
        "archive",
    }:
        return {family}, {"status", "revision"}, frozenset({"success"})
    if family == "applicability" and operation in {
        "candidate",
        "replace",
        "review",
        "publish",
        "revoke",
        "preview",
        "invalidate",
    }:
        return (
            {"catalog_item"},
            set(APPLICABILITY),
            CONTENT_CODES | {"success", "no_op"},
        )
    if family == "tree" and operation in {
        "import",
        "check",
        "publish",
        "restore",
        "retire",
        "revoke",
        "preview",
    }:
        return {"diag_tree"}, set(TREE), CONTENT_CODES
    if family == "kb" and operation in {
        "import",
        "check",
        "review",
        "publish",
        "retire",
        "revoke",
        "visibility",
        "preview",
    }:
        keys = {"document_id"}
        if operation == "review":
            keys |= REVIEW
        if operation in {"publish", "retire", "revoke"}:
            keys |= {"from_status", "to_status", "replacement_id", "review_event_id"}
        if operation == "visibility":
            keys |= {
                "from_visibility",
                "to_visibility",
                "release_reference",
                "released_set_sha256",
            }
        return (
            {"document", "document_version"},
            keys,
            CONTENT_CODES | {"success", "no_op"},
        )
    if action in {"document_version.edit_draft_file", "document_section.edit_draft"}:
        return {family}, set(), frozenset({"success"})
    privacy_entities = {
        "publish": "consent_publication",
        "retire": "consent_publication",
        "order_delete": "deletion_operation",
        "audit_maintenance": "retention_maintenance",
        "retention_review": "retention_maintenance",
    }
    if family == "privacy" and operation in privacy_entities:
        return (
            {privacy_entities[operation]},
            set(),
            frozenset(
                """
success authentication_denied forbidden protected_database_role_required
synthetic_database_required state_conflict invalid_approval_packet
version_exists policy_version_exists
""".split()
            ),
        )
    if action == "operations.observe":
        return (
            {"runtime"},
            set(),
            frozenset({"success", "authentication_denied", "forbidden"}),
        )
    raise ValueError("AUDIT_SCHEMA_INVALID")


def _value(key: str, value: Any) -> bool:
    base = key.removeprefix("before_").removeprefix("after_")
    if base in {
        "public_allowed",
        "is_active",
        "content_checked",
        "full_file_public_allowed",
    }:
        return type(value) is bool
    if base in {
        "revision",
        "from_version",
        "to_version",
        "source_count",
        "recommendation_count",
    }:
        return type(value) is int and 0 <= value <= 2147483647
    if base in {"status", "from_status", "to_status"}:
        return value in STATUSES
    if base in {"from_visibility", "to_visibility"}:
        return value in {"public", "internal"}
    if base == "role":
        return value in {"admin", "manager"}
    if base == "reason":
        return value in {
            "replacement",
            "retire",
            "revoke",
            "visibility",
            "model_status",
        }
    if base == "decision":
        return bool(value == "approved")
    if base in {"issue_codes", "blocker_codes", "publication_blockers"}:
        return (
            isinstance(value, list)
            and len(value) <= 1000
            and all(v in CONTENT_CODES for v in value)
        )
    if base in {"source_ids", "item_ids", "published_ids"}:
        return (
            isinstance(value, list)
            and len(value) <= 1000
            and all(_value("model_id", v) for v in value)
        )
    if not isinstance(value, str):
        return False
    if base.endswith("_id") or base in {"published_by"}:
        return value == "" or str(UUID(value)) == value
    if base.endswith("_sha256") or base == "yaml_checksum":
        return re.fullmatch("[0-9a-f]{64}", value) is not None
    if base.endswith("_reference"):
        return (
            value == ""
            or re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}", value) is not None
        )
    if base == "expert_name":
        return value == "" or (
            3 <= len(value) <= 200 and value == value.strip() and value.isprintable()
        )
    if base in {"approved_at", "published_at"}:
        return value == "" or datetime.fromisoformat(value).tzinfo is not None
    if base == "expert_approved_on":
        return date.fromisoformat(value).isoformat() == value
    return False


def validate_audit(action: str, entity: str, result: str, details: object) -> None:
    try:
        entities, keys, results = _schema(action)
        if (
            entity not in entities
            or result not in results
            or not isinstance(details, dict)
        ):
            raise ValueError
        if not details.keys() <= keys or not all(
            _value(k, v) for k, v in details.items()
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("AUDIT_SCHEMA_INVALID") from None
