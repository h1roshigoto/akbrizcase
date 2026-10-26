"""Trusted isolated bootstrap, invoked by absolute path with Python -I.

No document-supplied code, commands, paths or environment. Linux x86_64/aarch64,
libseccomp is required; unsupported hosts fail closed.
"""

import ctypes
import errno
import hashlib
import io
import json
import os
import resource
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pypdf


def restrictions(cpu: int, memory: int, *, tool: bool = False) -> None:
    if os.geteuid() == 0:
        raise RuntimeError("nonroot required")
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    size_limit = 128 * 1024**2 if tool else 0
    resource.setrlimit(resource.RLIMIT_FSIZE, (size_limit, size_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise RuntimeError("no_new_privs")


def network_filter(*, parser: bool) -> None:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x00050000 | errno.EPERM if parser else 0x7FFF0000)
    if not context:
        raise RuntimeError("seccomp")
    blocked = [
        "socket",
        "socketpair",
        "connect",
        "bind",
        "listen",
        "accept",
        "accept4",
        "sendto",
        "sendmsg",
        "sendmmsg",
        "recvfrom",
        "recvmsg",
        "recvmmsg",
        "setsid",
        "setpgid",
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
        "pidfd_getfd",
        "mount",
        "unshare",
        "setns",
        "bpf",
        "io_uring_setup",
        "open_by_handle_at",
        "userfaultfd",
    ]
    allowed = [
        "read",
        "write",
        "readv",
        "writev",
        "pread64",
        "close",
        "lseek",
        "fstat",
        "dup",
        "dup2",
        "dup3",
        "fcntl",
        "brk",
        "mmap",
        "mremap",
        "munmap",
        "mprotect",
        "madvise",
        "futex",
        "rt_sigaction",
        "rt_sigprocmask",
        "rt_sigreturn",
        "sigaltstack",
        "clock_gettime",
        "clock_nanosleep",
        "nanosleep",
        "getrandom",
        "getpid",
        "gettid",
        "getuid",
        "geteuid",
        "getgid",
        "getegid",
        "getrusage",
        "sched_yield",
        "restart_syscall",
        "exit",
        "exit_group",
    ]
    # Parser: default deny, including every pathname, network, exec and process
    # creation syscall. Only already-loaded code and inherited sealed input fd.
    names = allowed if parser else blocked
    action = 0x7FFF0000 if parser else 0x00050000 | errno.EPERM
    try:
        for name in names:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(context, action, number, 0):
                raise RuntimeError("seccomp rule")
        if lib.seccomp_load(context):
            raise RuntimeError("seccomp load")
    finally:
        lib.seccomp_release(context)


def main() -> None:
    task, descriptor, cpu, memory = sys.argv[1:5]
    restrictions(int(cpu), int(memory), tool=task == "exec")
    if task == "exec":
        network_filter(parser=False)
        os.execv(sys.argv[5], sys.argv[5:])
    observed = {
        "uid": os.geteuid(),
        "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0],
        "memory": resource.getrlimit(resource.RLIMIT_AS)[0],
        "environment": sorted(os.environ),
    }
    network_filter(parser=True)
    source = int(descriptor)
    if task == "pdf":
        with os.fdopen(os.dup(source), "rb") as file:
            reader = pypdf.PdfReader(file, strict=True)
            if reader.is_encrypted or len(reader.pages) < 1:
                raise ValueError("pdf")
            # Touch page dictionaries; no text extraction or actions are executed.
            for page in reader.pages:
                if page.get("/Type") != "/Page" or "/MediaBox" not in page:
                    raise ValueError("pdf")
    elif task == "inspect":
        data = os.read(source, 26214401)
        print(
            json.dumps({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        )
    elif task == "probe":
        operations: dict[str, Callable[[], object]] = {
            "filesystem": lambda: os.open("/etc/passwd", os.O_RDONLY),
            "network": socket.socket,
            "fork": os.fork,
            "input_write": lambda: os.write(source, b"changed"),
        }
        for name, operation in operations.items():
            try:
                operation()
                observed[name] = 0
            except OSError as error:
                observed[name] = error.errno
        print(json.dumps(observed))
    elif task == "network":
        socket.socket()
    elif task == "filesystem":
        Path("/run/secrets/runtime_password").read_bytes()
    elif task == "write":
        os.write(source, b"changed")
    elif task == "fork":
        os.fork()
    elif task == "cpu":
        while True:
            pass
    elif task == "memory":
        io.BytesIO(b"x" * (int(memory) * 2))
    elif task == "timeout":
        time.sleep(60)
    elif task == "output":
        while True:
            print("x" * 4096, flush=True)
    else:
        raise ValueError("task")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # Library warnings are bounded by the parent; never forwarded to logs.
        sys.exit(70)
