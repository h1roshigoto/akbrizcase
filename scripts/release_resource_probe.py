"""Closed numeric evidence for the six-client release resource rehearsal.

Process arguments are inspected only to select a fixed role; they never leave
this collector. Role samples are non-atomic observations, not allocation traces.
"""

import os
import threading
from pathlib import Path


def resource_snapshot(root=Path("/sys/fs/cgroup")):
    result = {}
    for name in (
        "memory.current",
        "memory.peak",
        "memory.max",
        "memory.swap.current",
        "memory.swap.peak",
        "memory.swap.max",
        "pids.current",
        "pids.peak",
        "pids.max",
    ):
        try:
            value = (root / name).read_text().strip()
            result[name] = int(value) if value != "max" else value
        except FileNotFoundError:
            result[name] = None
    for name in ("memory.events", "pids.events"):
        result[name] = {
            k: int(v)
            for k, v in (
                line.split() for line in (root / name).read_text().splitlines()
            )
        }
    result["cpu.max"] = tuple(int(n) for n in (root / "cpu.max").read_text().split())
    return result


def require_resource_bounds(value):
    # Unsupported/malformed evidence cannot silently certify this Linux gate.
    assert value["memory.max"] == 512 * 1024**2
    assert value["pids.max"] == 64
    quota, period = value["cpu.max"]
    assert quota == period and period > 0
    assert 0 < value["memory.peak"] < value["memory.max"]
    assert value["memory.events"]["max"] == 0
    for key in ("oom", "oom_kill", "oom_group_kill"):
        assert value["memory.events"].get(key, 0) == 0
    assert value["pids.events"]["max"] == 0
    assert 0 < value["pids.current"] <= value["pids.max"]
    if value["pids.peak"] is not None:
        assert value["pids.peak"] <= value["pids.max"]
    if value["memory.swap.max"] == 0:
        assert value["memory.swap.current"] in (None, 0)
        assert value.get("memory.swap.peak") in (None, 0)
    else:
        assert value["memory.swap.current"] == 0
        assert value["memory.swap.peak"] == 0


def forkserver_command(args):
    return (
        len(args) >= 3
        and args[1] == b"-c"
        and args[2].startswith(b"from multiprocessing.forkserver import main;")
    )


def role_memory():
    roles = {}
    own = os.getpid()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            args = process.joinpath("cmdline").read_bytes().split(b"\0")
            comm = process.joinpath("comm").read_text().strip()
            if int(process.name) == own:
                role = "stress_driver"
            elif comm == "clamscan":
                role = "clamav"
            elif forkserver_command(args):
                parent = next(
                    line.split()[1]
                    for line in process.joinpath("status").read_text().splitlines()
                    if line.startswith("PPid:")
                )
                parent_args = (
                    process.parent.joinpath(parent, "cmdline").read_bytes().split(b"\0")
                )
                role = (
                    "api_worker" if forkserver_command(parent_args) else "api_forkserver"
                )
            elif b"--multiprocessing-fork" in args:
                role = "api_worker"
            elif any(a.endswith(b"/files/worker.py") for a in args):
                role = "sandbox_bootstrap"
            elif any(b"resource_tracker" in a for a in args):
                role = "resource_tracker"
            elif comm == "docker-init":
                role = "init"
            elif comm == "uvicorn" or any(
                a.endswith(b"/browser_media_server.py") for a in args
            ):
                role = "api_supervisor"
            else:
                role = "other_or_health_probe"
            metrics = {}
            for line in process.joinpath("smaps_rollup").read_text().splitlines():
                key, _, raw = line.partition(":")
                if key in ("Rss", "Pss", "SwapPss"):
                    metrics[key] = int(raw.split()[0]) * 1024
            group = roles.setdefault(
                role, {"count": 0, "rss": 0, "pss": 0, "swap_pss": 0}
            )
            group["count"] += 1
            for target, source in (
                ("rss", "Rss"),
                ("pss", "Pss"),
                ("swap_pss", "SwapPss"),
            ):
                group[target] += metrics.get(source, 0)
        except (OSError, ProcessLookupError):
            # A process can exit during a sample; kernel cgroup peaks/events remain authoritative.
            continue
    return roles


class ResourceProbe:
    def __init__(self):
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.samples = 0
        self.errors = 0
        self.peak_sample = None
        self.role_peaks = {}
        self.swap_peak = 0

    def sample_once(self):
        snapshot = resource_snapshot()
        roles = role_memory()
        self.last_roles = roles
        self.samples += 1
        if (
            self.peak_sample is None
            or snapshot["memory.current"] > self.peak_sample["memory.current"]
        ):
            self.peak_sample = {
                "memory.current": snapshot["memory.current"],
                "roles": roles,
            }
        for name, values in roles.items():
            peak = self.role_peaks.setdefault(name, {k: 0 for k in values})
            for key, value in values.items():
                peak[key] = max(peak[key], value)
        self.swap_peak = max(self.swap_peak, snapshot["memory.swap.current"] or 0)

    def sample(self):
        while not self.done.wait(0.05):
            try:
                self.sample_once()
            except Exception:
                self.errors += 1

    def start(self):
        self.before = resource_snapshot()
        require_resource_bounds(self.before)
        self.sample_once()
        self.thread.start()

    def finish(self):
        self.done.set()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()
        self.sample_once()
        after = resource_snapshot()
        assert self.errors == 0 and self.swap_peak == 0
        require_resource_bounds(after)
        assert not {"clamav", "sandbox_bootstrap"} & self.last_roles.keys()
        return {
            "before": self.before,
            "after": after,
            "samples": self.samples,
            "roles_at_sampled_peak": self.peak_sample,
            "independent_role_peaks": self.role_peaks,
            "sampled_swap_peak_bytes": self.swap_peak,
        }
