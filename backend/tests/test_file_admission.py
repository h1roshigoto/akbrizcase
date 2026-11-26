"""Cross-thread and cross-process bounded admission tests for heavy file work."""

import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.modules.kb.files.admission import FileAdmission
from app.modules.kb.files.process import FileRejected


def _hold_in_child(root_str: str, ready_evt: object) -> None:
    admission = FileAdmission(Path(root_str), max_concurrent=1, wait_timeout=10.0)
    with admission.acquire():
        ready_evt.set()  # type: ignore[attr-defined]
        time.sleep(30)


def _compete_in_child(
    root_str: str,
    barrier: object,
    counter: object,
    max_observed: object,
    lock: object,
) -> int:
    admission = FileAdmission(Path(root_str), max_concurrent=2, wait_timeout=10.0)
    barrier.wait(timeout=10)  # type: ignore[attr-defined]
    with admission.acquire():
        with lock:  # type: ignore[attr-defined]
            counter.value += 1  # type: ignore[attr-defined]
            if counter.value > max_observed.value:  # type: ignore[attr-defined]
                max_observed.value = counter.value  # type: ignore[attr-defined]
        time.sleep(0.08)
        with lock:  # type: ignore[attr-defined]
            counter.value -= 1  # type: ignore[attr-defined]
    return 1


def test_admission_rejects_invalid_config(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    for bad_max in (0, -1, 5):
        with pytest.raises(ValueError):
            FileAdmission(root, max_concurrent=bad_max)
    for bad_wait in (0.0, -0.5, 31.0):
        with pytest.raises(ValueError):
            FileAdmission(root, wait_timeout=bad_wait)
    for bad_poll in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            FileAdmission(root, poll_interval=bad_poll)


def test_admission_enforces_bound_across_threads_and_releases_on_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    admission = FileAdmission(root, max_concurrent=2, wait_timeout=5.0)
    active = 0
    peak = 0
    guard = threading.Lock()

    def work(idx: int) -> None:
        nonlocal active, peak
        with admission.acquire():
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            if idx == 1:
                raise RuntimeError("synthetic failure")

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(work, i) for i in range(6)]
    assert peak <= 2
    assert active == 0
    # Wait exhaustion returns PIPELINE_BUSY, then releases cleanly
    with admission.acquire():
        with admission.acquire():
            busy = FileAdmission(
                root, max_concurrent=2, wait_timeout=0.05, poll_interval=0.01
            )
            with pytest.raises(FileRejected) as rejected:
                with busy.acquire():
                    pass
            assert rejected.value.code == "PIPELINE_BUSY"
    # Slot is reusable after exception and busy refusal
    with admission.acquire():
        pass
    assert sum(1 for f in futures if f.exception() is None) == 5


def test_admission_enforces_bound_across_processes_and_recovers_on_kill(
    tmp_path: Path,
) -> None:
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("spawn")

    # 1. Bound across 4 separate processes with max_concurrent=2
    barrier = ctx.Barrier(4)
    counter = ctx.Value("i", 0)
    max_observed = ctx.Value("i", 0)
    lock = ctx.Lock()
    workers = [
        ctx.Process(
            target=_compete_in_child,
            args=(str(root), barrier, counter, max_observed, lock),
        )
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
    assert max_observed.value <= 2

    # 2. Killed holder process releases flock automatically
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_in_child, args=(str(root), ready))
    holder.start()
    try:
        assert ready.wait(timeout=10)
        busy = FileAdmission(
            root, max_concurrent=1, wait_timeout=0.05, poll_interval=0.01
        )
        with pytest.raises(FileRejected) as rejected:
            with busy.acquire():
                pass
        assert rejected.value.code == "PIPELINE_BUSY"
        holder.kill()
        holder.join(timeout=5)
        with busy.acquire():
            pass
    finally:
        if holder.is_alive():
            holder.kill()
            holder.join(timeout=5)


def test_admission_rejects_bad_permissions_and_symlink_locks(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    locks_dir = root / "locks"
    locks_dir.mkdir(mode=0o755)
    admission = FileAdmission(root, max_concurrent=1, wait_timeout=0.5)
    with pytest.raises(FileRejected) as bad_perm:
        with admission.acquire():
            pass
    assert bad_perm.value.code == "STORAGE_PERMISSIONS"

    locks_dir.chmod(0o700)
    target = tmp_path / "outside.lock"
    target.write_text("x")
    os.symlink(target, locks_dir / "slot-0.lock")
    with pytest.raises(FileRejected) as symlink_err:
        with admission.acquire():
            pass
    assert symlink_err.value.code == "STORAGE_FAILURE"
