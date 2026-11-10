"""Real Docker image-binding guards: tiny owned images, no containers/network/DB."""

import io
import json
import subprocess
import tarfile
from uuid import uuid4

from mvp048_images import REVISION, SOURCE_TREE, source_trees, verify_application_images


def main():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    trees = source_trees(head)
    created = []
    tags = []
    prefix = "akbrizcase-mvp048-image-guard-" + uuid4().hex

    def docker(*args):
        return subprocess.check_output(
            ["docker", *args], text=True, stderr=subprocess.PIPE, timeout=30
        ).strip()

    def image(revision, tree):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w"):
            pass
        tag = prefix + ":" + str(len(tags))
        changes = ["--change", "LABEL com.akbrizcase.image-guard=" + prefix]
        if revision is not None:
            changes += ["--change", f"LABEL {REVISION}={revision}"]
        changes += ["--change", f"LABEL {SOURCE_TREE}={tree}"]
        result = subprocess.run(
            ["docker", "import", *changes, "-", tag],
            input=archive.getvalue(),
            capture_output=True,
            check=True,
            timeout=30,
        )
        identifier = result.stdout.decode().strip()
        created.append(identifier)
        tags.append(tag)
        return identifier

    try:
        backend = image(head, trees["backend"])
        frontend = image(head, trees["frontend"])
        services = {
            "api": {"Image": backend},
            "worker": {"Image": backend},
            "nginx": {"Image": frontend},
        }
        verify_application_images(docker, services, head, trees)
        for revision, tree in [
            ("0" * 40, trees["backend"]),
            (None, trees["backend"]),
            (head, "0" * 40),
        ]:
            rejected = image(revision, tree)
            stale = {
                **services,
                "api": {"Image": rejected},
                "worker": {"Image": rejected},
            }
            try:
                verify_application_images(docker, stale, head, trees)
            except ValueError:
                continue
            raise RuntimeError("IMAGE_GUARD_ACCEPTED_UNVERIFIED_SOURCE")
        print("MVP048_IMAGE_GUARDS PASS positive/stale/unlabelled/context; no load")
    finally:
        # Remove only images created here, after checking their private ownership
        # label. No container, network or persistent volume action is performed.
        for tag in tags:
            detail = json.loads(docker("image", "inspect", tag))[0]
            if (
                detail["Id"] not in created
                or (detail["Config"].get("Labels") or {}).get(
                    "com.akbrizcase.image-guard"
                )
                != prefix
            ):
                raise ValueError("IMAGE_GUARD_OWNERSHIP_MISMATCH")
            docker("image", "rm", tag)


if __name__ == "__main__":
    main()
