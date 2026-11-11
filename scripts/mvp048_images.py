"""Exact-source application images shared by synthetic and target preflight.

OCI revision binds a build to the commit; the committed context tree binds the
backend/frontend inputs. Third-party PG/Python images have no application Git
revision: their immutable IDs are recorded alongside this verified application
binding (and must match the authorization packet in target mode).
"""

import json
import re
import subprocess

REVISION = "org.opencontainers.image.revision"
SOURCE_TREE = "com.akbrizcase.source-tree"
BACKEND_SERVICES = ("api", "worker", "migrate", "seed", "kb", "privacy")


def source_trees(head):
    if not re.fullmatch("[0-9a-f]{40}", head):
        raise ValueError("MVP048_IMAGE_HEAD_INVALID")
    return {
        role: subprocess.check_output(
            ["git", "rev-parse", f"{head}:{context}"], text=True
        ).strip()
        for role, context in (("backend", "backend"), ("frontend", "frontend"))
    }


def build_services(head, trees):
    backend = "akbrizcase-backend:mvp048-" + head
    frontend = "akbrizcase-frontend:mvp048-" + head
    result = {service: {"image": backend} for service in BACKEND_SERVICES}
    result["api"]["build"] = {"labels": {REVISION: head, SOURCE_TREE: trees["backend"]}}
    result["nginx"] = {
        "image": frontend,
        "build": {"labels": {REVISION: head, SOURCE_TREE: trees["frontend"]}},
    }
    return result


def verify_application_images(docker, services, head, trees):
    """Reject stale/unlabelled images and a worker differing from the checked API.

    This runs before seeding in synthetic mode and before HTTP preflight in
    target mode, then again before load. Callers start consumers by content ID.
    """
    ids = {"backend": services["api"]["Image"], "frontend": services["nginx"]["Image"]}
    if services["worker"]["Image"] != ids["backend"]:
        raise ValueError("MVP048_WORKER_IMAGE_MISMATCH")
    for role, identifier in ids.items():
        if not re.fullmatch("sha256:[0-9a-f]{64}", identifier):
            raise ValueError("MVP048_IMAGE_ID_INVALID")
        detail = json.loads(docker("image", "inspect", identifier))[0]
        labels = detail["Config"].get("Labels") or {}
        if (
            detail["Id"] != identifier
            or labels.get(REVISION) != head
            or labels.get(SOURCE_TREE) != trees[role]
        ):
            raise ValueError("MVP048_IMAGE_SOURCE_UNVERIFIED")
    return {
        **ids,
        "application_revision": head,
        "backend_source_tree": trees["backend"],
        "frontend_source_tree": trees["frontend"],
    }


def build_and_pin(compose, stack, override_path, head, trees):
    """The full synthetic build cannot silently reuse a stale mutable tag."""
    compose("build", "api", "nginx")
    override = json.loads(override_path.read_text())
    services = {
        name: {
            "Image": json.loads(
                stack.docker("image", "inspect", override["services"][name]["image"])
            )[0]["Id"]
        }
        for name in ("api", "nginx", "worker")
    }
    binding = verify_application_images(stack.docker, services, head, trees)
    for service in BACKEND_SERVICES:
        override["services"][service]["image"] = binding["backend"]
    override["services"]["nginx"]["image"] = binding["frontend"]
    override_path.write_text(json.dumps(override))
    return binding
