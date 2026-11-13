"""Run trusted Chromium staff acceptance; operator enters credentials on its TTY."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = Path("/home/hiros/.akbriz-front-demo")
if not sys.stdin.isatty():
    raise SystemExit("Run from a private terminal. Never pipe staff credentials.")
command = [
    "docker",
    "run",
    "--rm",
    "-it",
    "--network",
    "host",
    "--shm-size=256m",
    "--user",
    "1000:1000",
    "-e",
    "HOME=/tmp/browser-home",
    "-e",
    "NODE_EXTRA_CA_CERTS=/ca.crt",
]
for source, target, readonly in [
    (REPO / "frontend", "/work", True),
    (ROOT / "ca.crt", "/ca.crt", True),
    (REPO / "docs/mvp-polish-001/screenshots", "/evidence", False),
]:
    command += [
        "--mount",
        f"type=bind,source={source},target={target}"
        + (",readonly" if readonly else ""),
    ]
command += [
    "--workdir",
    "/work",
    "--entrypoint",
    "sh",
    "akbrizcase-product001-browser:local",
    "-c",
    'mkdir -p "$HOME/.pki/nssdb" && certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password && certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n polish -i /ca.crt && exec node tests/polish-staff.mjs',
]
raise SystemExit(subprocess.call(command))
