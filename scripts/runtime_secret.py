"""Create/validate local runtime secret files without revealing their contents."""

import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

NAMES = ("postgres_password", "migrator_password", "runtime_password", "staff_csrf_secret")


def main() -> None:
    if sys.argv[1:] == ["init"]:
        directory = Path(".secrets")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        for name in NAMES:
            path = directory / name
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                print(f"Preserved existing .secrets/{name}")
                continue
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
            # Local Compose binds source file permissions. Non-root UID 10001
            # must read the selected mount; host traversal is blocked by 0700.
            if name != "postgres_password":
                path.chmod(0o444)
            print(f"Created protected .secrets/{name} (value not displayed)")
        return
    if sys.argv[1:] != ["check"]:
        sys.exit("Usage: runtime_secret.py init|check")
    config = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    definitions = json.loads(config.stdout)["secrets"]
    for name in NAMES:
        path = Path(definitions[name]["file"])
        if not path.is_file():
            sys.exit(
                "Secret file missing; run make init-secrets or supply protected files"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
        # A private file OR read-only file inside a private directory is safe.
        if mode & 0o022 or (mode & 0o077 and parent_mode & 0o077):
            sys.exit(
                "Secret must be private or read-only inside a private 0700 directory"
            )
        if len(path.read_bytes().strip()) < 32:
            sys.exit(
                "Secret requires at least 32 random characters; no default is provided"
            )
    print("Runtime secret files: present, protected, non-empty (values not displayed)")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.stderr.strip() or "Unable to validate Compose configuration")
