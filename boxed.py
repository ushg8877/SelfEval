"""Build a fail-closed bubblewrap command for the teacher-side Linux worker."""

from pathlib import Path
import os
import shutil
import stat
from packages import COMPILERS, FILE_BASE


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("隔离评测仅接受普通文件，不能使用链接。")


def boxed_argv(argv, cwd, phase, executable, file_io=None):
    # Only search system paths. The caller cannot choose the isolation tool.
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if not bwrap:
        raise RuntimeError("本机缺少 bubblewrap，请安装后重试。")
    if phase not in ("compile", "run"):
        raise ValueError("未知隔离阶段")
    executable = Path(executable).absolute()
    args = [bwrap, "--unshare-all", "--unshare-user", "--unshare-net",
            "--cap-drop", "ALL", "--die-with-parent", "--new-session",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--dir", "/usr", "--dir", "/etc"]
    # Mount runtime/toolchain paths, not /, /home, /var, /usr/local or /etc.
    system_paths = ["/usr/lib", "/usr/lib64", "/lib", "/lib64", "/etc/ld.so.cache"]
    if phase == "compile":
        system_paths += ["/usr/bin", "/bin", "/usr/include"]
        # Alpine and some Fedora toolchains place cc1plus here.
        if Path("/usr/libexec/gcc").is_dir():
            args += ["--dir", "/usr/libexec"]
            system_paths.append("/usr/libexec/gcc")
    for name in system_paths:
        path = Path(name)
        if path.is_symlink():
            args += ["--symlink", os.readlink(name), name]
        elif path.exists():
            args += ["--ro-bind", name, name]

    if phase == "compile":
        if len(argv) != 7 or tuple(argv[1:4]) not in COMPILERS.values():
            raise ValueError("隔离编译参数无效")
        if argv[5] != "-o" or Path(argv[6]).absolute() != executable or executable.name != "solution":
            raise ValueError("隔离编译目标无效")
        compiler = Path(argv[0]).resolve(strict=True)
        if compiler.parent != Path("/usr/bin") or not os.access(str(compiler), os.X_OK):
            raise ValueError("本机正式评测需要 /usr/bin 内的系统 C++ 编译器。")
        source_name = os.fspath(argv[4])
        if not source_name.startswith("./") or "/" in source_name[2:]:
            raise ValueError("隔离编译需要单个源文件")
        source = Path(cwd).resolve(strict=True) / source_name[2:]
        _regular(source)
        if executable.parent.is_symlink() or any(executable.parent.iterdir()):
            raise ValueError("隔离编译需要全新的产物目录")
        args += ["--dir", "/src", "--ro-bind", str(source), "/src/main.cpp",
                 "--bind", str(executable.parent), "/build", "--chdir", "/src",
                 "--", str(compiler), *argv[1:4],
                 "/src/main.cpp", "-o", "/build/solution"]
    else:
        if len(argv) != 1 or Path(argv[0]).absolute() != executable:
            raise ValueError("隔离运行目标无效")
        _regular(executable)
        run_dir = "/tmp"
        if file_io is not None:
            if not FILE_BASE.fullmatch(file_io):
                raise ValueError("文件名前缀无效")
            run_dir = "/work"
            args += ["--dir", run_dir]
            for suffix, mount in ((".in", "--ro-bind"), (".out", "--bind")):
                name = file_io + suffix
                path = Path(cwd) / name
                _regular(path)
                args += [mount, str(path), run_dir + "/" + name]
        args += ["--ro-bind", str(executable), "/program", "--remount-ro", "/tmp",
                 "--remount-ro", "/dev", "--remount-ro", "/",
                 "--chdir", run_dir, "--", "/program"]
    return args
