"""Create a local demo secret or validate the configured Compose secret file."""

import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if sys.argv[1:] == ["init"]:
        directory = Path(".secrets")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / "postgres_password"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            sys.exit("Secret already exists; refusing to overwrite it.")
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
        print("Created protected .secrets/postgres_password (value not displayed).")
        return
    if sys.argv[1:] != ["check"]:
        sys.exit("Usage: runtime_secret.py init|check")
    config = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(json.loads(config.stdout)["secrets"]["postgres_password"]["file"])
    if not path.is_file():
        sys.exit(
            "PostgreSQL secret file is missing; run make init-secrets or supply a protected file."
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        sys.exit("PostgreSQL secret file must be private: chmod 600 <secret-file>.")
    if len(path.read_bytes().strip()) < 32:
        sys.exit(
            "PostgreSQL secret must contain at least 32 random characters; no default is provided."
        )
    print("Runtime secret file: present, private, non-empty (value not displayed).")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.stderr.strip() or "Unable to validate Compose configuration.")
