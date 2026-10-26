"""Real local ClamAV, signed production CVD only; no update or network client."""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.modules.kb.files.process import FileRejected, Limits
from app.modules.kb.files.sandbox import run_tool

SCAN_LIMITS = Limits(cpu=30, memory=2 * 1024**3, seconds=45, output=16384)
MAX_SIGNATURE_AGE = 72 * 3600


@dataclass(frozen=True)
class ScanStamp:
    checked_at: int
    database_version: str
    signatures_at: int


class ClamAV:
    def __init__(self, database: Path = Path("/var/lib/akbrizcase/clamav")) -> None:
        self.database = database

    def _database(self) -> tuple[str, int]:
        try:
            # No loose .ndb/.hdb/.cld, symlinks, test or extra databases. Releases
            # must be operator-verified, immutable directories, atomically selected.
            if self.database.is_symlink() or set(
                p.name for p in self.database.iterdir()
            ) != {
                "main.cvd",
                "daily.cvd",
                "bytecode.cvd",
            }:
                raise FileRejected("SIGNATURES_INVALID")
            versions: list[str] = []
            daily_epoch = 0
            for name in ("main.cvd", "daily.cvd", "bytecode.cvd"):
                path = self.database / name
                if path.is_symlink() or not path.is_file():
                    raise FileRejected("SIGNATURES_INVALID")
                output = run_tool(
                    ["/usr/bin/sigtool", "--info", str(path)],
                    fds=(),
                    limits=SCAN_LIMITS,
                )
                if b"Verification OK" not in output:
                    raise FileRejected("SIGNATURES_INVALID")
                with path.open("rb") as file:
                    header = file.read(512).decode("ascii").strip().split(":")
                if len(header) != 9 or header[0] != "ClamAV-VDB":
                    raise FileRejected("SIGNATURES_INVALID")
                versions.append(header[2])
                if name == "daily.cvd":
                    daily_epoch = int(header[8])
            self.require_fresh(daily_epoch)
            return ":".join(versions), daily_epoch
        except FileRejected:
            raise
        except (OSError, UnicodeError, ValueError):
            raise FileRejected("SIGNATURES_UNAVAILABLE") from None

    @staticmethod
    def require_fresh(epoch: int) -> None:
        age = int(datetime.now(UTC).timestamp()) - epoch
        if age < -300 or age > MAX_SIGNATURE_AGE:
            raise FileRejected("SIGNATURES_STALE")

    def scan(self, descriptor: int) -> ScanStamp:
        version, epoch = self._database()
        os.lseek(descriptor, 0, os.SEEK_SET)
        output = run_tool(
            [
                "/usr/bin/clamscan",
                "--stdout",
                "--database=" + str(self.database),
                "--max-filesize=26M",
                "--max-scansize=100M",
                "--max-recursion=8",
                "--max-files=100",
                "--alert-exceeds-max=yes",
                "--tempdir=/tmp",
                f"/proc/self/fd/{descriptor}",
            ],
            fds=(descriptor,),
            limits=SCAN_LIMITS,
        )
        if (
            not re.search(rb"^Known viruses: [1-9][0-9]*\r?$", output, re.M)
            or not re.search(rb"^Scanned files: 1\r?$", output, re.M)
            or not re.search(rb"^Infected files: 0\r?$", output, re.M)
            or b"ERROR" in output
            or b"WARNING" in output
        ):
            raise FileRejected("SCANNER_UNCONFIRMED")
        if self._database() != (version, epoch):
            raise FileRejected("SIGNATURES_CHANGED")
        return ScanStamp(int(datetime.now(UTC).timestamp()), version, epoch)
