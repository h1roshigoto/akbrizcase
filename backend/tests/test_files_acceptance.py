"""Real clamscan and kernel/process acceptance; only synthetic disposable files.

No production configuration switch accepts this test database. This subclass is
trusted test composition and is never imported by application code.
"""

import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pytest import MonkeyPatch

from app.modules.kb.files import FilePipeline, FileRejected
from app.modules.kb.files import scanner as scanner_module
from app.modules.kb.files.formats import validate
from app.modules.kb.files.process import Limits, run_bounded
from app.modules.kb.files.sandbox import run_parser, run_tool, sealed_snapshot
from app.modules.kb.files.scanner import ClamAV
from app.modules.kb.files.storage import MAX_BYTES

pytestmark = pytest.mark.file_acceptance
INFECTED = b"MVP020-SYNTHETIC-ANTIVIRUS-FIXTURE"


class SyntheticClamAV(ClamAV):
    def _database(self) -> tuple[str, int]:
        # Real clamscan engine, synthetic ndb only; never production authority.
        epoch = int((self.database / "test.ndb").stat().st_mtime)
        self.require_fresh(epoch)
        return "synthetic-only", epoch


@pytest.fixture
def pipeline(tmp_path: Path) -> FilePipeline:
    database = tmp_path / "signatures"
    database.mkdir()
    (database / "test.ndb").write_text(f"MVP020.Safe.Test:0:*:{INFECTED.hex()}\n")
    root = Path(os.environ.get("MVP020_ACCEPTANCE_FILES", str(tmp_path / "files")))
    root.mkdir(mode=0o700, exist_ok=True)
    return FilePipeline(root, _scanner=SyntheticClamAV(database))


def pdf() -> bytes:
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "data,name,mime",
    [
        ("Текст UTF-8".encode(), "a.txt", "text/plain"),
        (b"# Synthetic\n\n**text**", "a.md", "text/markdown"),
        (pdf(), "a.pdf", "application/pdf"),
        (b"x" * MAX_BYTES, "a.txt", "text/plain"),
    ],
    ids=["utf8", "markdown", "pdf", "25MiB"],
)
def test_real_clean(pipeline: FilePipeline, data: bytes, name: str, mime: str) -> None:
    first = pipeline.accept(io.BytesIO(data), filename=name, mime=mime)
    assert first.checksum_sha256 == hashlib.sha256(data).hexdigest()
    assert first.size_bytes == len(data)
    assert first.scan.database_version == "synthetic-only"
    assert (
        pipeline.root / "documents" / "verified" / first.file_key
    ).read_bytes() == data
    with pipeline.prepare(first.file_key) as (metadata, descriptor):
        assert metadata.checksum_sha256 == first.checksum_sha256
        result = json.loads(run_parser(descriptor))
        assert result == {"size": len(data), "sha256": first.checksum_sha256}
    second = pipeline.accept(
        io.BytesIO(b"another synthetic"), filename="a.txt", mime="text/plain"
    )
    assert second.file_key != first.file_key


def test_real_infected_and_production_rejects_test_db(pipeline: FilePipeline) -> None:
    before = set((pipeline.root / "documents" / "verified").glob("*"))
    with pytest.raises(FileRejected, match="SCANNER_INFECTED"):
        pipeline.accept(io.BytesIO(INFECTED), filename="a.txt", mime="text/plain")
    assert set((pipeline.root / "documents" / "verified").iterdir()) == before
    with pytest.raises(FileRejected):
        ClamAV(pipeline.scanner.database)._database()


@pytest.mark.parametrize(
    "task",
    ["network", "filesystem", "write", "fork", "cpu", "memory", "timeout", "output"],
)
def test_sandbox_negative(task: str) -> None:
    descriptor = sealed_snapshot(b"synthetic")
    try:
        limits = Limits(
            cpu=1,
            memory=128 * 1024**2,
            seconds=1 if task == "timeout" else 3,
            output=4096,
        )
        expected = {
            "timeout": "PROCESS_TIMEOUT",
            "output": "PROCESS_OUTPUT_LIMIT",
        }.get(task, "PARSER_REJECTED")
        with pytest.raises(FileRejected, match=expected):
            run_parser(descriptor, task, limits=limits)
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.read(descriptor, 99) == b"synthetic"
    finally:
        os.close(descriptor)


def test_corrupt_pdf_real_parser() -> None:
    for data in (
        b"%PDF-1.4\nstartxref\n0\n%%EOF",
        pdf().replace(b"/MediaBox", b"/WrongBox"),
    ):
        with pytest.raises(FileRejected):
            validate(data, "a.pdf", "application/pdf")


def test_modified_verified_bytes(pipeline: FilePipeline) -> None:
    result = pipeline.accept(
        io.BytesIO(b"synthetic original"), filename="a.txt", mime="text/plain"
    )
    path = pipeline.root / "documents" / "verified" / result.file_key
    path.chmod(0o600)
    path.write_bytes(b"synthetic replaced")
    path.chmod(0o400)
    with pytest.raises(FileRejected):
        with pipeline.prepare(result.file_key):
            pytest.fail("must not yield changed bytes")


def test_mutation_during_scan_cannot_change_snapshot(
    pipeline: FilePipeline, monkeypatch: MonkeyPatch
) -> None:
    result = pipeline.accept(
        io.BytesIO(b"synthetic original"), filename="a.txt", mime="text/plain"
    )
    with pytest.raises(FileRejected):
        with pipeline.prepare(result.file_key) as (_, descriptor):
            path = pipeline.root / "documents" / "verified" / result.file_key
            path.chmod(0o600)
            path.write_bytes(b"synthetic changed")
            path.chmod(0o400)
            assert (
                json.loads(run_parser(descriptor))["sha256"] == result.checksum_sha256
            )


@pytest.mark.parametrize(
    "program,code",
    [
        ("/missing-clamscan", "SCANNER_FAILURE"),
        ("timeout", "PROCESS_TIMEOUT"),
        ("crash", "SCANNER_FAILURE"),
        ("output", "PROCESS_OUTPUT_LIMIT"),
        ("fake-clean", "SCANNER_UNCONFIRMED"),
    ],
)
def test_real_scanner_fault_process(
    pipeline: FilePipeline, monkeypatch: MonkeyPatch, program: str, code: str
) -> None:
    actual = run_tool

    def fault(command: list[str], *, fds: tuple[int, ...], limits: Limits) -> bytes:
        if command[0] == "/usr/bin/clamscan":
            command = (
                ["/missing-clamscan"]
                if program.startswith("/")
                else [
                    sys.executable,
                    "-c",
                    {
                        "timeout": "import time; time.sleep(60)",
                        "crash": "import os; os.kill(os.getpid(),9)",
                        "output": "print('x'*20000)",
                        "fake-clean": "print('OK')",
                    }[program],
                ]
            )
            limits = Limits(cpu=1, memory=256 * 1024**2, seconds=2)
        return actual(command, fds=fds, limits=limits)

    monkeypatch.setattr(scanner_module, "run_tool", fault)
    with pytest.raises(FileRejected, match=code):
        pipeline.accept(io.BytesIO(b"synthetic"), filename="a.txt", mime="text/plain")


def assert_terminated(children: list[int]) -> None:
    # killpg returns after queuing SIGKILL, not after every descendant finishes
    # exiting. Require observed disappearance/Z for EVERY PID within 0.5 s;
    # merely having sent the signal is never accepted as termination evidence.
    deadline = time.monotonic() + 0.5
    pending = set(children)
    observed: dict[int, list[str]] = {}
    while pending:
        for child in list(pending):
            try:
                fields = Path(f"/proc/{child}/stat").read_text().split()
            except FileNotFoundError:
                pending.remove(child)
                continue
            observed[child] = fields[2:6]  # state, ppid, pgrp, session
            if fields[2] == "Z":
                pending.remove(child)
        if pending:
            assert time.monotonic() < deadline, f"Live descendants: {observed}"
            time.sleep(0.001)


def test_process_tree_killed(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    script = (
        "import os,time; p=os.fork(); "
        + f"open({str(marker)!r},'w').write(str(p)) if p else None; time.sleep(60)"
    )
    with pytest.raises(FileRejected, match="PROCESS_TIMEOUT"):
        run_bounded([sys.executable, "-c", script], limits=Limits(seconds=2))
    child = int(marker.read_text())
    assert_terminated([child])


def test_entire_process_group_with_grandchildren_killed(tmp_path: Path) -> None:
    script = f"""
import os,time,json
from pathlib import Path
root=Path({str(tmp_path)!r})
for _ in range(2):
    if os.fork() == 0:
        os.fork()
        pid=os.getpid()
        (root/str(pid)).write_text(json.dumps([pid,os.getpgrp(),os.getsid(0)]))
        time.sleep(60)
        os._exit(0)
(root/'group').write_text(str(os.getpgrp()))
time.sleep(60)
"""
    with pytest.raises(FileRejected, match="PROCESS_TIMEOUT"):
        run_bounded([sys.executable, "-c", script], limits=Limits(seconds=2))
    group = int((tmp_path / "group").read_text())
    descendants = [
        json.loads(path.read_text())
        for path in tmp_path.iterdir()
        if path.name != "group"
    ]
    assert len(descendants) == 4  # two children, each with one grandchild
    assert all(pgrp == group and sid == group for _, pgrp, sid in descendants)
    assert_terminated([pid for pid, _, _ in descendants])


def test_stream_failure_no_verified_result(pipeline: FilePipeline) -> None:
    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("synthetic private path must not leak")

    with pytest.raises(FileRejected, match="STREAM_FAILURE") as error:
        pipeline.accept(Broken(), filename="private.txt", mime="text/plain")
    assert str(error.value) == "STREAM_FAILURE"


def test_sandbox_enforcement_evidence() -> None:
    descriptor = sealed_snapshot(b"synthetic")
    try:
        limits = Limits(cpu=2, memory=128 * 1024**2)
        report = json.loads(run_parser(descriptor, "probe", limits=limits))
        assert report["uid"] != 0
        assert report["cpu"] == 2 and report["memory"] == 128 * 1024**2
        assert set(report["environment"]) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
        for operation in ("filesystem", "network", "fork"):
            assert report[operation] == 1  # EPERM, not missing files/network
        # RLIMIT_FSIZE=0 may reject the write before the memfd seal does.
        assert report["input_write"] in (1, 27)  # EPERM or EFBIG
    finally:
        os.close(descriptor)


def test_competing_pipeline_keys(
    pipeline: FilePipeline, monkeypatch: MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(
        "app.modules.kb.files.pipeline.secrets.token_hex", lambda _: "f" * 64
    )

    def accept() -> bool:
        try:
            pipeline.accept(
                io.BytesIO(b"synthetic conflict"), filename="a.txt", mime="text/plain"
            )
            return True
        except FileRejected:
            return False

    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(lambda _: accept(), range(2))) == [False, True]
    assert (
        pipeline.root / "documents" / "verified" / ("f" * 64)
    ).read_bytes() == b"synthetic conflict"


def test_real_stale_database_blocks_acceptance(pipeline: FilePipeline) -> None:
    database = pipeline.scanner.database / "test.ndb"
    os.utime(database, (1, 1))
    with pytest.raises(FileRejected, match="SIGNATURES_STALE"):
        pipeline.accept(io.BytesIO(b"synthetic"), filename="a.txt", mime="text/plain")


def test_scan_input_is_immutable(
    pipeline: FilePipeline, monkeypatch: MonkeyPatch
) -> None:
    original_scan = pipeline.scanner.scan

    def scan(descriptor: int) -> scanner_module.ScanStamp:
        with pytest.raises(PermissionError):
            os.write(descriptor, b"replacement")
        return original_scan(descriptor)

    monkeypatch.setattr(pipeline.scanner, "scan", scan)
    result = pipeline.accept(
        io.BytesIO(b"synthetic unchanged"), filename="a.txt", mime="text/plain"
    )
    assert result.checksum_sha256 == hashlib.sha256(b"synthetic unchanged").hexdigest()


def test_forged_manifest_cannot_authorize_infected_bytes(
    pipeline: FilePipeline,
) -> None:
    result = pipeline.accept(
        io.BytesIO(b"synthetic clean"), filename="a.txt", mime="text/plain"
    )
    target = pipeline.root / "documents" / "verified" / result.file_key
    evidence = pipeline.root / "documents" / "receipts" / result.file_key
    manifest = json.loads(evidence.read_bytes())
    manifest["size_bytes"] = len(INFECTED)
    manifest["checksum_sha256"] = hashlib.sha256(INFECTED).hexdigest()
    manifest["scan"] = {"checked_at": int(time.time()), "clean": True}
    for path, data in ((target, INFECTED), (evidence, json.dumps(manifest).encode())):
        path.chmod(0o600)
        path.write_bytes(data)
        path.chmod(0o400)
    with pytest.raises(FileRejected, match="SCANNER_INFECTED"):
        with pipeline.prepare(result.file_key):
            pytest.fail("forged evidence must never authorize use")


def test_real_sigtool_rejects_unsigned_cvd(tmp_path: Path) -> None:
    database = tmp_path / "unsigned"
    database.mkdir()
    for name in ("main.cvd", "daily.cvd", "bytecode.cvd"):
        (database / name).write_bytes(
            b"ClamAV-VDB:synthetic:1:1:1:fake:fake:test:1".ljust(512, b" ")
        )
    with pytest.raises(FileRejected):
        ClamAV(database)._database()
