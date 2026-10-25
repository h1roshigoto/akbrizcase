"""Generate a development-only local CA and localhost certificate, without logs."""

import subprocess
from pathlib import Path


def main() -> None:
    directory = Path(".certs")
    directory.mkdir(mode=0o700, exist_ok=True)
    if any(directory.iterdir()):
        raise SystemExit(
            "Preserved existing .certs; remove it explicitly to regenerate"
        )

    def openssl(*args: str) -> None:
        subprocess.run(
            ["openssl", *args],
            cwd=directory,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:3072",
        "-nodes",
        "-days",
        "7",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
        "-subj",
        "/CN=akbrizcase local development CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
    )
    openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:3072",
        "-nodes",
        "-keyout",
        "localhost.key",
        "-out",
        "localhost.csr",
        "-subj",
        "/CN=localhost",
    )
    extensions = directory / "localhost.ext"
    extensions.write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "extendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\n"
    )
    openssl(
        "x509",
        "-req",
        "-in",
        "localhost.csr",
        "-CA",
        "ca.crt",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "localhost.crt",
        "-days",
        "7",
        "-extfile",
        "localhost.ext",
    )
    (directory / "ca.key").chmod(0o600)
    for name in ("ca.crt", "localhost.crt"):
        (directory / name).chmod(0o444)
    # nginx UID 101 can read the selected mount; host parent remains private.
    (directory / "localhost.key").chmod(0o444)
    print(
        "Created seven-day development certificates in ignored .certs (no key displayed)"
    )


if __name__ == "__main__":
    main()
