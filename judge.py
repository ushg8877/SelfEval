"""Local practice judge. Only run your own trusted C++ sources."""
import argparse
from contextlib import contextmanager
from io import BytesIO
import json
import locale
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4
from zipfile import BadZipFile

from packages import COMPILERS, Package


_ENV_REASONS = {
    "process-limit": "Linux 暂时无法创建进程，请检查当前用户的进程数和进程限额。",
    "namespace": "Linux 未允许 bubblewrap 创建隔离环境，请检查用户命名空间和 AppArmor 策略。",
    "mount": "bubblewrap 无法建立隔离文件系统，请检查系统挂载权限和临时目录。",
    "exec": "bubblewrap 无法执行沙箱内的程序，请检查系统工具链及临时目录的执行权限。",
    "version": "当前 bubblewrap 不支持所需参数，请检查安装版本。",
    "limits": "prlimit 无法设置评测资源限制，请检查当前用户的资源限额。",
    "missing-bwrap": "本机缺少 bubblewrap，请安装后重试。",
    "missing-prlimit": "本机缺少 prlimit，请安装 util-linux 后重试。",
    "compiler": "未找到可用的系统 C++ 编译器，请检查 /usr/bin/g++。",
    "meter": "无法运行本机资源计量器，请检查系统编译器和临时目录的执行权限。",
    "platform": "请在 Linux 上运行正式评测。",
    "runner": "本机评测环境未能启动，请检查评测依赖和系统权限。",
    "data": "无法完成正式数据校验，请联系组题人检查赛包。",
}
_ENV_PHASES = {"prepare": "准备评测", "compile": "编译", "run": "运行测试程序"}


def environment_summary(phase, reason):
    if phase in _ENV_PHASES and reason in _ENV_REASONS:
        return _ENV_PHASES[phase] + "时发生环境错误。" + _ENV_REASONS[reason]
    return "本机评测环境发生错误，请检查运行环境后重试。"


def environment_reason(message):
    # Only the category leaves the judge. A process can write arbitrary text
    # (including what looks like a bwrap error) to its stderr.
    line = message.partition("\n")[0].lower()
    if line.startswith("bwrap:"):
        if "resource temporarily unavailable" in line:
            return "process-limit"
        if any(word in line for word in ("namespace", "uid map", "gid map", "loopback")):
            return "namespace"
        if "unknown option" in line:
            return "version"
        if any(word in line for word in ("mount", "bind", "symlink")):
            return "mount"
        if "exec" in line:
            return "exec"
    if line.startswith("[errno 11]"):
        return "process-limit"
    if line.startswith("prlimit:"):
        return "limits"
    if line.startswith("本机缺少 bubblewrap"):
        return "missing-bwrap"
    if line.startswith("缺少 prlimit"):
        return "missing-prlimit"
    if "资源计量器" in line or "内存计量结果" in line:
        return "meter"
    if line.startswith(("找不到 g++", "本机正式评测需要 /usr/bin")):
        return "compiler"
    if line.startswith("请在 linux 上运行"):
        return "platform"
    return "runner"


def resolve_package(name):
    path = Path(name)
    if path.is_file():
        return path.resolve()
    candidate = Path(__file__).resolve().parent / "data" / (str(name) + ".zip")
    if candidate.is_file():
        return candidate.resolve()
    raise ValueError(f"找不到题包 {name}，请给出 ZIP 的路径。")


def read_diagnostic(path):
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        data = stream.read(8192)
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gb18030"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace").strip()


@contextmanager
def scratch(parent=None):
    parent = Path(parent or os.environ.get("SELFEVAL_TEMP") or tempfile.gettempdir()).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="selfeval-", dir=parent) as directory:
        yield Path(directory)


def evaluate(package, source, mode, *, on_event=None, cancel=None, compiler=None,
             case_number=None, save_failure=None, boxed=False):
    event = on_event or (lambda _event: None)
    result = {"status": "ERROR", "summary": "", "score": None, "max_score": None,
              "cases": [], "diagnostic": ""}
    phase = "prepare"

    def finish(status, summary):
        if boxed and status == "ERROR":
            reason = result.get("environment_reason") or environment_reason(summary)
            result.update(environment_phase=phase, environment_reason=reason, diagnostic="")
            summary = environment_summary(phase, reason)
        result.update(status=status, summary=summary)
        return result

    try:
        import sys
        if not sys.platform.startswith("linux"):
            return finish("ERROR", "请在 Linux 上运行这个自测工具。")
        from linuxrunner import run_program
        if boxed:
            from boxed import boxed_argv
        source = Path(source).resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in {".cpp", ".cc", ".cxx", ".c++"} and source.suffix != ".C":
            return finish("ERROR", "请选择 C++ 源文件（.cpp、.cc 或 .cxx）。")
        if mode not in {"samples", "full"}:
            return finish("ERROR", "请选择样例或正式数据。")
        cc = shutil.which(str(compiler or os.environ.get("CXX") or "g++"))
        if not cc:
            return finish("ERROR", "找不到 g++。请设置 PATH 或将 CXX 设为编译器的完整路径。")
        data = package if isinstance(package, BytesIO) else resolve_package(package)
        with Package(data) as pkg, scratch() as tmp:
            if mode == "full":
                result["score"] = 0
                result["max_score"] = pkg.full_score
                result["score_parts"] = pkg.score_parts({}, 0)
            if pkg.compiler == "c++14" and source.stat().st_size > 100 * 1024:
                return finish("CE", "源文件超过本题的 100 KiB 限制。")
            limit = pkg.n_samples if mode == "samples" else pkg.n_tests
            if not limit:
                return finish("ERROR", "这个题包没有该模式的数据。")
            if case_number is not None and not 1 <= case_number <= limit:
                return finish("ERROR", f"测试点编号应在 1 至 {limit} 之间。")
            full_scoring = mode == "full" and case_number is None
            if full_scoring:
                result["max_score"] = pkg.full_score
            root = Path(tmp)
            builddir = root / "build"
            builddir.mkdir()
            executable = builddir / "solution"
            empty = root / "empty"
            empty.touch()
            event({"kind": "progress", "message": f"正在编译 {pkg.compiler.upper()}…"})
            build_log = root / "compile.err"
            phase = "compile"
            command = [cc, *COMPILERS[pkg.compiler], "./" + source.name, "-o", str(executable)]
            if boxed:
                command = boxed_argv(command, source.parent, "compile", executable)
            build = run_program(command, source.parent, empty, root / "compile.out",
                                build_log, 60, 2048, 16)
            if build["status"] != "OK" or not executable.is_file():
                if build["status"] == "ERROR":
                    return finish("ERROR", build.get("message") or "无法启动本地编译器。")
                result["diagnostic"] = read_diagnostic(build_log) or build.get("message", "")
                return finish("CE", "编译失败。")

            phase = "run"
            outputs, extra_outputs = {}, {}
            failed = None
            def check(number, extra):
                nonlocal failed
                with scratch(root) as case_dir:
                    case_dir = Path(case_dir)
                    inp, expected = pkg.extract(number, case_dir, extra=extra)
                    work = case_dir / "run"
                    work.mkdir()
                    actual, err = case_dir / "actual.txt", case_dir / "stderr.txt"
                    stdin, stdout = inp, actual
                    if pkg.file_io:
                        inp = inp.rename(work / (pkg.file_io + ".in"))
                        actual = work / (pkg.file_io + ".out")
                        actual.touch()
                        stdin, stdout = empty, case_dir / "stdout.txt"
                    command = [str(executable)]
                    if boxed:
                        command = boxed_argv(command, work, "run", executable, pkg.file_io)
                    limits = pkg.limits(number, extra=extra)
                    item = run_program(command, work, stdin, stdout, err, *limits,
                                       stack_by_memory=pkg.compiler == "c++14")
                    if boxed and item["status"] == "ERROR":
                        reason = environment_reason(item.get("message", ""))
                        result["environment_reason"] = reason
                        item["message"] = environment_summary(phase, reason)
                    if item["status"] == "OK":
                        if not actual.is_file():
                            item.update(status="WA", message="找不到指定的输出文件")
                        elif actual.stat().st_size > limits[2] * (1 << 20):
                            item.update(status="OLE", message="输出文件超过大小限制")
                        else:
                            item["status"], item["message"] = pkg.compare(expected, actual)
                            if boxed and item["status"] == "ERROR":
                                result["environment_reason"] = "data"
                                item["message"] = environment_summary(phase, "data")
                    if item["status"] == "RE" and err.is_file():
                        diag = read_diagnostic(err)
                        if diag:
                            item["message"] += "\n" + diag
                    label = str(number)
                    if extra and mode == "full":
                        label = f"附加 {number}"
                    item.update(number=label, extra=extra)
                    if item["status"] != "AC" and failed is None:
                        failed = item
                        if save_failure:
                            folder = Path(save_failure).resolve()
                            folder.mkdir(parents=True, exist_ok=True)
                            dest = folder / f"case-{number}-{uuid4().hex[:12]}"
                            dest.mkdir()
                            for f, name in ((inp, "input.txt"), (expected, "answer.txt"),
                                            (actual, "output.txt"), (err, "stderr.txt")):
                                if f.is_file():
                                    shutil.copyfile(f, dest / name)
                            result["failure_dir"] = str(dest)
                    result["cases"].append(item)
                    event(dict(item, kind="case"))
                    return item["status"]

            def stopped():
                return cancel is not None and cancel.is_set()

            main_ids = [case_number] if case_number is not None else range(1, limit + 1)
            for number in main_ids:
                if stopped():
                    return finish("CANCELLED", "评测已停止。")
                outputs[number] = check(number, mode == "samples")
                if outputs[number] == "ERROR":
                    return finish("ERROR", f"测试点 {number} 评测失败，请查看详情。")
            qoj_extras = pkg.checker != "noi-text"
            if full_scoring and qoj_extras and all(s == "AC" for s in outputs.values()):
                for number in range(1, pkg.n_ex_tests + 1):
                    if stopped():
                        return finish("CANCELLED", "评测已停止。")
                    extra_outputs[number] = check(number, True)
                    if extra_outputs[number] == "ERROR":
                        return finish("ERROR", f"附加测试点 {number} 评测失败，请查看详情。")
            if full_scoring:
                result["score"] = pkg.score(outputs, extra_results=extra_outputs if qoj_extras else None)
                result["score_parts"] = pkg.score_parts(outputs, result["score"])
            passed = sum(s == "AC" for s in outputs.values())
            label = "正式点" if mode == "full" else "样例"
            summary = f"{label}通过 {passed}/{len(outputs)}"
            if extra_outputs:
                summary += f"，附加点通过 {sum(v == 'AC' for v in extra_outputs.values())}/{len(extra_outputs)}"
            if full_scoring:
                summary += f"，得分 {result['score']:g}/{pkg.full_score:g}"
            if failed:
                summary += f"。首个未通过的点为 {failed['number']}（{failed['status']}）"
            return finish(failed["status"] if failed else "AC", summary + "。")
    except (ValueError, OSError, RuntimeError, BadZipFile) as exc:
        return finish("ERROR", str(exc))


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地自测 · C++")
    ap.add_argument("package", help="题包 ZIP 路径或内置题号 A/B/C")
    ap.add_argument("source", type=Path, help="选手的 C++ 源文件")
    ap.add_argument("--full", action="store_true", help="跑正式数据并计分（默认跑样例）")
    ap.add_argument("--case", type=int, dest="number", help="只测本模式中指定的一个点")
    ap.add_argument("--compiler", help="g++ 或 clang++ 的路径")
    ap.add_argument("--save-failure", type=Path, metavar="DIR", help="保存首个未通过点的输入、输出及答案")
    ap.add_argument("--json", type=Path, metavar="FILE", help="将完整结果写入 JSON 文件")
    args = ap.parse_args(argv)
    def display(e):
        if e["kind"] == "progress":
            print(e["message"], flush=True)
        else:
            memory = "—" if e["memory_kb"] is None else f"{e['memory_kb']} KiB"
            print(f"{str(e['number']):>5}  {e['status']:5}  {e['time_ms']:7} ms  "
                  f"{memory:>13}  {e.get('message','')}", flush=True)
    result = evaluate(args.package, args.source, "full" if args.full else "samples",
                      on_event=display, compiler=args.compiler, case_number=args.number,
                      save_failure=args.save_failure)
    print(result["summary"])
    if result["diagnostic"]:
        print(result["diagnostic"])
    if "failure_dir" in result:
        print("首错文件目录", result["failure_dir"])
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "AC" else (2 if result["status"] == "ERROR" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
