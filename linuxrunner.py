"""Single-case Linux runner for trusted submissions, compatible with Python 3.8."""

import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time


_meter_lock = threading.Lock()
_meter_dir = None


def _meter_binary():
    """Build the trusted wait4 helper once per player process, for this Linux ABI."""
    global _meter_dir
    with _meter_lock:
        if _meter_dir is None:
            compiler = next((path for name in ("cc", "gcc", "g++")
                             if (path := shutil.which(name, path="/usr/bin:/bin"))), None)
            if not compiler:
                raise RuntimeError("找不到系统 C 编译器，无法构建资源计量器。")
            folder = tempfile.TemporaryDirectory(prefix="selfeval-meter-", dir=os.environ.get("SELFEVAL_TEMP") or None)
            try:
                subprocess.run([compiler, "-x", "c", "-std=c11", "-O2",
                                str(Path(__file__).resolve().with_name("meter.c")),
                                "-o", str(Path(folder.name) / "meter")], check=True, timeout=60,
                               cwd=folder.name, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            except (OSError, subprocess.SubprocessError) as exc:
                folder.cleanup()
                raise RuntimeError("本机资源计量器编译失败，请检查系统编译器和临时目录。") from exc
            _meter_dir = folder
        return str(Path(_meter_dir.name) / "meter")


class _Meter:
    """Receive the actual exec child's host PID, then its kernel peak at exit."""

    def __init__(self, argv, boxed, sender, receiver):
        binary = _meter_binary()
        self.receiver = receiver
        self.receiver.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.receiver.setblocking(False)
        self.pid = None
        self.peak = None
        self.error = None
        args = list(argv)
        fd = str(sender.fileno())
        if boxed:
            # Put the mount before bwrap remounts the sandbox root read-only.
            split = args.index("--")
            mount = args.index("--remount-ro") if "--remount-ro" in args[:split] else split
            args[split + 1:split + 1] = ["/selfeval-meter", fd]
            args[mount:mount] = ["--ro-bind", binary, "/selfeval-meter"]
        else:
            args = [binary, fd] + args
        self.argv = args

    def receive(self):
        # SCM_CREDENTIALS translates the sender PID into the observer's namespace.
        # The native helper closes its private socket in the child before exec.
        credentials = struct.Struct("3i")
        while True:
            try:
                msg, control, _, _ = self.receiver.recvmsg(80, socket.CMSG_SPACE(credentials.size))
            except BlockingIOError:
                return
            if msg == b"S":
                for level, kind, data in control:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                        self.pid = credentials.unpack_from(data)[0]
            elif msg.startswith(b"R ") and msg[2:].isdigit():
                self.peak = int(msg[2:])
                self.pid = None
            elif msg.startswith(b"E ") and msg[2:].isdigit():
                self.error = os.strerror(int(msg[2:]))


def _tree_usage(pid, ticks_per_second):
    """Sample CPU and RSS of a normal fork/wait tree, as seen in procfs."""
    pending = [pid]
    seen = set()
    ticks = 0
    rss_pages = 0
    while pending:
        here = pending.pop()
        if here in seen:
            continue
        seen.add(here)
        try:
            data = Path("/proc/{}/stat".format(here)).read_bytes()
            # The command name (field 2) can contain spaces and parentheses.
            fields = data[data.rfind(b")") + 2:].split()
            # utime, stime, cutime, cstime: include already waited-for children.
            ticks += sum(int(value) for value in fields[11:15])
            rss_pages += max(0, int(fields[21]))
            children = Path("/proc/{}/task/{}/children".format(here, here))
            pending.extend(map(int, children.read_bytes().split()))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue  # the task already exited
    return ticks / ticks_per_second, rss_pages


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _exit_result(code, diagnostic, boxed):
    # bubblewrap's supervising process reports a child's signal as 128+signal.
    if boxed and 128 < code < 128 + signal.NSIG:
        code = 128 - code
    if code == -signal.SIGXCPU:
        return "TLE", "CPU 时间超过限制"
    if code == -signal.SIGXFSZ:
        return "OLE", "写入文件超过大小限制"
    if code == 0:
        return "OK", ""
    if diagnostic.startswith(b"prlimit:") or (boxed and diagnostic.startswith(b"bwrap:")):
        return "ERROR", diagnostic.decode("utf-8", errors="replace").strip()
    if code == -signal.SIGABRT and b"std::bad_alloc" in diagnostic:
        return "MLE", "程序因未捕获的 std::bad_alloc 退出（内存分配失败）"
    if code < 0:
        return "RE", "程序被信号 {} 终止".format(-code)
    return "RE", "程序退出码 {}".format(code)


class _Relay:
    """Expose anonymous pipes to a private submission, never host file FDs."""

    def __init__(self, proc, source, output, errors, cap):
        self.selector = selectors.DefaultSelector()
        self.overflow = False
        self.pending = b""
        self.source = source
        self.stdin = proc.stdin
        self.sinks = {proc.stdout: output, proc.stderr: errors}
        self.remaining = {proc.stdout: cap, proc.stderr: cap}
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_WRITE if stream == proc.stdin
                                   else selectors.EVENT_READ)

    def _close(self, stream):
        self.selector.unregister(stream)
        stream.close()

    def transfer(self, timeout):
        for key, _ in self.selector.select(timeout):
            stream = key.fileobj
            try:
                if stream == self.stdin:
                    if not self.pending:
                        self.pending = self.source.read(64 * 1024)
                    if not self.pending:
                        self._close(stream)
                        continue
                    sent = os.write(stream.fileno(), self.pending)
                    self.pending = self.pending[sent:]
                else:
                    data = os.read(stream.fileno(), 64 * 1024)
                    if not data:
                        self._close(stream)
                        continue
                    take = min(len(data), self.remaining[stream])
                    self.sinks[stream].write(data[:take])
                    self.remaining[stream] -= take
                    if take != len(data):
                        self.overflow = True
                        self._close(stream)
            except BrokenPipeError:
                self._close(stream)
            except BlockingIOError:
                continue

    def finish(self):
        try:
            if not self.stdin.closed:
                self._close(self.stdin)
            deadline = time.monotonic() + 1
            while self.selector.get_map() and time.monotonic() < deadline:
                self.transfer(0.01)
        finally:
            for key in list(self.selector.get_map().values()):
                self._close(key.fileobj)
            self.selector.close()


def _boxed_process_limit():
    import resource

    # Linux charges RLIMIT_NPROC for every thread under the real UID, including
    # the desktop session. Reserve room for the submission above that baseline.
    uid = os.getuid()
    threads = 0
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            status = (process / "status").read_bytes().splitlines()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        owner = None
        count = 0
        for line in status:
            if line.startswith(b"Uid:"):
                owner = int(line.split()[1])
            elif line.startswith(b"Threads:"):
                count = int(line.split()[1])
        if owner == uid:
            threads += count
    inherited = resource.getrlimit(resource.RLIMIT_NPROC)
    limits = [threads + 128]
    limits.extend(value for value in inherited if value != resource.RLIM_INFINITY)
    return min(limits)


def _run(argv, cwd, stdin_path, stdout_path, stderr_path, time_limit, memory_mb, output_mb, stack_by_memory):
    if not argv or not all(math.isfinite(v) and v > 0 for v in (time_limit, memory_mb, output_mb)):
        raise ValueError("程序与资源限制无效")
    boxed = Path(argv[0]).name == "bwrap"
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with sender, receiver:
        meter = _Meter(argv, boxed, sender, receiver)
        return _run_metered(meter, sender, boxed, cwd, stdin_path, stdout_path, stderr_path,
                            time_limit, memory_mb, output_mb, stack_by_memory)


def _run_metered(meter, sender, boxed, cwd, stdin_path, stdout_path, stderr_path,
                 time_limit, memory_mb, output_mb, stack_by_memory):
    # Ubuntu's util-linux prlimit performs setup in a native child and then
    # execs the submission. Tk's worker thread never executes Python after fork.
    prlimit = shutil.which("prlimit")
    if not prlimit:
        raise RuntimeError("缺少 prlimit，请安装 Linux 的 util-linux 软件包。")
    memory_bytes = int(memory_mb * 1024 * 1024)
    output_bytes = int(output_mb * 1024 * 1024)
    cpu_soft_limit = max(1, math.ceil(time_limit))
    wall_limit = 3 * time_limit + 1
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    # Allow one sentinel byte to distinguish exact-limit output from truncation.
    command = [prlimit, "--cpu={}:{}".format(cpu_soft_limit, cpu_soft_limit + 1),
               "--as={}".format(memory_bytes), "--fsize={}".format(output_bytes + 1),
               "--core=0", "--"] + [os.fspath(arg) for arg in meter.argv]
    if stack_by_memory:
        import resource
        hard = resource.getrlimit(resource.RLIMIT_STACK)[1]
        stack = memory_bytes if hard == resource.RLIM_INFINITY else min(memory_bytes, hard)
        command.insert(1, "--stack={}".format(stack))
    status, message = "OK", ""
    max_rss_kb = 0
    max_cpu = 0
    # Focal's bubblewrap predates --clearenv. Give its process an allowlisted
    # environment at spawn rather than inheriting service credentials at all.
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
           "LANG": "C", "LC_ALL": "C"} if boxed else None
    if boxed:
        command.insert(1, "--nproc={}".format(_boxed_process_limit()))
    with open(stdin_path, "rb") as inp, open(stdout_path, "wb", buffering=0) as out, open(stderr_path, "wb", buffering=0) as err:
        started = time.monotonic()
        stdio = [subprocess.PIPE] * 3 if boxed else [inp, out, err]
        proc = subprocess.Popen(command, cwd=cwd, stdin=stdio[0], stdout=stdio[1], stderr=stdio[2],
                                start_new_session=True, close_fds=True, restore_signals=True,
                                env=env, bufsize=0, pass_fds=(sender.fileno(),))
        sender.close()
        relay = None
        try:
            if boxed:
                relay = _Relay(proc, inp, out, err, output_bytes)
            # WNOWAIT pins the group leader's PID until its children are killed.
            wait_flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
            next_sample = started
            while os.waitid(os.P_PID, proc.pid, wait_flags) is None:
                now = time.monotonic()
                if now >= next_sample:
                    meter.receive()
                    if meter.pid:
                        cpu, rss_pages = _tree_usage(meter.pid, ticks_per_second)
                        max_cpu = max(cpu, max_cpu)
                        max_rss_kb = max(max_rss_kb, rss_pages * page_kb)
                    next_sample = now + 0.01
                if max_cpu > time_limit:
                    status, message = "TLE", "CPU 时间超过限制"
                elif max_rss_kb * 1024 > memory_bytes:
                    status, message = "MLE", "观测到常驻内存超过限制"
                elif (relay and relay.overflow) or max(os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > output_bytes:
                    status, message = "OLE", "输出超过大小限制"
                elif now - started > wall_limit:
                    status, message = "TLE", "实际等待时间超过 {:g} 秒".format(wall_limit)
                if status != "OK":
                    break
                if relay is not None:
                    relay.transfer(0.01)
                else:
                    time.sleep(0.01)
        finally:
            _kill_group(proc.pid)
            _, raw_status, usage = os.wait4(proc.pid, 0)
            if os.WIFEXITED(raw_status):
                proc.returncode = os.WEXITSTATUS(raw_status)
            else:
                proc.returncode = -os.WTERMSIG(raw_status)
            if relay is not None:
                relay.finish()
        cpu_seconds = max(max_cpu, usage.ru_utime + usage.ru_stime)
        meter.receive()
        if meter.peak is not None:
            max_rss_kb = max(max_rss_kb, meter.peak)
        if status == "OK":
            if (relay and relay.overflow) or max(os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > output_bytes:
                status, message = "OLE", "输出超过大小限制"
            elif cpu_seconds > time_limit:
                status, message = "TLE", "CPU 时间超过限制"
            elif max_rss_kb * 1024 > memory_bytes:
                status, message = "MLE", "观测到常驻内存超过限制"
            else:
                with open(stderr_path, "rb") as check:
                    diagnostic = check.read(2048)
                status, message = _exit_result(proc.returncode, diagnostic, boxed)
                if meter.error:
                    status, message = "ERROR", "资源计量器无法启动测试程序：" + meter.error
                elif status == "OK" and meter.peak is None:
                    status, message = "ERROR", "未收到测试程序的内存计量结果。"
    measured_kb = max_rss_kb if meter.peak is not None or max_rss_kb else None
    return dict(status=status, time_ms=round(cpu_seconds * 1000, 2),
                memory_kb=measured_kb, message=message)


def run_program(argv, cwd, stdin_path, stdout_path, stderr_path, time_limit, memory_mb, output_mb,
                *, stack_by_memory=False):
    """Return status, CPU time_ms, peak memory_kb, message for one execution."""
    try:
        return _run(argv, cwd, stdin_path, stdout_path, stderr_path,
                    time_limit, memory_mb, output_mb, stack_by_memory)
    except (OSError, RuntimeError, ValueError, OverflowError) as exc:
        return dict(status="ERROR", time_ms=0, memory_kb=None, message=str(exc))
