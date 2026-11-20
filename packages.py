"""Read traditional QOJ test packages without executing code from the archive."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path, PurePosixPath
import re
from shutil import copyfileobj
from zipfile import ZipFile


class PackageError(ValueError):
    """The package is malformed or uses an unsupported judging rule."""


_C_CHECKER = "fd51fefb04f9a1cabb8fdda148ca40ce2e8060295cb09cec30e78af3d3b16ca0"
_C_HEADER = "743822d8120213a3482f4a6e8b9846219b84218350be548a42bdfdb056fba9e3"
# testlib isBlanks deliberately excludes vertical tab and form feed.
_WORD = re.compile(rb"[^ \t\r\n]+")
_INTEGER = re.compile(rb"0|-?[1-9][0-9]{0,18}")
COMPILERS = {"gnu++17": ("-std=gnu++17", "-O2", "-pipe"),
             "c++14": ("-O2", "-std=c++14", "-static")}
FILE_BASE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_KEY = re.compile(
    r"use_builtin_(?:judger|checker)|n_(?:tests|ex_tests|sample_tests|subtasks|aux_files)"
    r"|time_limit(?:_ms)?(?:_[1-9]\d*)?|(?:memory|output)_limit(?:_[1-9]\d*)?"
    r"|(?:input|output)_(?:pre|suf)|problem_name|full_score|score_precision"
    r"|subtask_(?:end|score|type)_[1-9]\d*|aux_file_[1-9]\d*"
    r"|(?:checker|standard|validator)_(?:time|memory|output)_limit"
    r"|selfeval_(?:checker|file_io|compiler)"
)


def _decimal(value, name):
    # Limit the representation, not just the value, before arithmetic/quantize.
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,9})?", value):
        raise PackageError(f"{name} 的数值无效：{value!r}")
    return Decimal(value)


def _integer(value, name, low, high):
    if not re.fullmatch(r"[0-9]{1,10}", value) or not low <= int(value) <= high:
        raise PackageError(f"{name} 需在 {low} 至 {high} 范围内")
    return int(value)


def _words(stream):
    # Bound memory for a long line, e.g. one million integers in a single answer.
    carry = b""
    while True:
        block = stream.read(65536)
        if not block:
            if carry:
                yield carry
            return
        data = carry + block
        carry = b""
        for match in _WORD.finditer(data):
            token = match.group()
            if match.end() == len(data):
                # 21 bytes already exceeds every supported signed integer.
                carry = token[:21]
            else:
                yield token


def _noi_lines(stream):
    pending_empty = 0
    for line in stream:
        if line.endswith(b"\n"):
            line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
        line = line.rstrip(b" ")
        if not line:
            pending_empty += 1
            continue
        for _ in range(pending_empty):
            yield b""
        pending_empty = 0
        yield line


def _valid_number(token, bits):
    if token is None or _INTEGER.fullmatch(token) is None:
        return False
    if len(token) < (10 if bits == 32 else 19):
        return True
    return -(1 << (bits - 1)) <= int(token) < 1 << (bits - 1)


def _show(token):
    if token is None:
        return "输出已结束"
    return repr(token[:32])[1:] + ("…" if len(token) > 32 else "")


class Package:
    def __init__(self, path):
        self.archive = ZipFile(path)
        try:
            self._load()
        except BaseException:
            self.archive.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.archive.close()

    def _load(self):
        entries = self.archive.infolist()
        if len(entries) > 20000:
            raise PackageError("该 ZIP 的文件数超过本地评测支持范围")
        names = [i.filename for i in entries if not i.is_dir()]
        if len(names) != len(set(names)):
            raise PackageError("ZIP 中有重名文件")
        self._prefix = ""
        if "problem.conf" not in names:
            parents = {name.split("/", 1)[0] for name in names}
            if len(parents) != 1:
                raise PackageError("未找到单题 QOJ ZIP 根目录的 problem.conf")
            self._prefix = next(iter(parents)) + "/"
        files = {name[len(self._prefix):] for name in names}
        if "problem.conf" not in files:
            raise PackageError("未找到单题 QOJ ZIP 根目录的 problem.conf")
        if any(name.startswith("require/") for name in files):
            raise PackageError("本地版暂不支持附带 require/ 运行或编译文件的题包")
        for name in names:
            if "\\" in name or ":" in name or name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise PackageError("ZIP 包含不安全路径")
        config = self._read_small("problem.conf", 128 * 1024).decode("utf-8-sig")
        self.config = {}
        for line in config.splitlines():
            line = line.partition("#")[0].strip()
            if not line:
                continue
            pair = line.split()
            if len(pair) != 2 or pair[0] in self.config:
                raise PackageError(f"无效或重复的配置：{line}")
            key, value = pair
            if key in {"interaction_mode", "submit_answer", "with_implementer", "domjudge_multipass"} and value == "off":
                continue
            if _KEY.fullmatch(key) is None:
                raise PackageError(f"本地版暂不支持配置 {key}")
            self.config[key] = value
        c = self.config
        if c.get("use_builtin_judger") != "on":
            raise PackageError("仅支持 use_builtin_judger on 的传统题")
        self.file_io = c.get("selfeval_file_io")
        if self.file_io is not None and not FILE_BASE.fullmatch(self.file_io):
            raise PackageError("文件读写需要简单的英文文件名前缀")
        self.compiler = c.get("selfeval_compiler", "gnu++17")
        if self.compiler not in COMPILERS:
            raise PackageError("不支持此编译参数")
        local_checker = c.get("selfeval_checker")
        if local_checker is not None and (local_checker != "noi-text" or "use_builtin_checker" in c):
            raise PackageError("本地检查器配置无效")
        self.checker = local_checker or c.get("use_builtin_checker")
        if not self.checker and {"chk.cpp", "testlib.h"} <= files:
            # Only this audited checker and its audited library use the local C adapter.
            checker = self._read_small("chk.cpp", 64 * 1024).replace(b"\r\n", b"\n")
            header = self._read_small("testlib.h", 2 << 20).replace(b"\r\n", b"\n")
            if sha256(checker).hexdigest() == _C_CHECKER and sha256(header).hexdigest() == _C_HEADER:
                self.checker = "yunagi-c-int32"
        if self.checker not in {"ncmp", "noi-text", "yunagi-c-int32"}:
            raise PackageError("该题的自定义检查器尚未支持")
        self.n_tests = _integer(c.get("n_tests", ""), "n_tests", 1, 10000)
        self.n_ex_tests = _integer(c.get("n_ex_tests", "0"), "n_ex_tests", 0, 10000)
        self.n_samples = _integer(c.get("n_sample_tests", "0"), "n_sample_tests", 0, self.n_ex_tests)
        self._full = _decimal(c.get("full_score", "100"), "full_score")
        if self._full <= 0:
            raise PackageError("full_score 必须为正数")
        self.full_score = float(self._full)
        self._precision = None
        if "score_precision" in c:
            self._precision = _integer(c["score_precision"], "score_precision", 0, 9)
        self.groups = []
        n_groups = _integer(c.get("n_subtasks", "0"), "n_subtasks", 0, self.n_tests)
        begin = 1
        for i in range(1, n_groups + 1):
            kind = c.get(f"subtask_type_{i}", "min")
            if kind not in {"min", "packed"}:
                raise PackageError(f"不支持的子任务类型：{kind}")
            end = _integer(c.get(f"subtask_end_{i}", ""), f"subtask_end_{i}", begin, self.n_tests)
            score = _decimal(c.get(f"subtask_score_{i}", ""), f"subtask_score_{i}")
            self.groups.append((begin, end, score))
            begin = end + 1
        if self.groups and (begin != self.n_tests + 1 or sum(g[2] for g in self.groups) != self._full):
            raise PackageError("子任务应覆盖全部正式点，分值之和应为满分")
        for extra, count in ((False, self.n_tests), (True, self.n_ex_tests)):
            for i in range(1, count + 1):
                for name in self._names(i, extra):
                    if name not in files:
                        raise PackageError(f"数据文件缺失：{name}")
                    if self.archive.getinfo(self._prefix + name).file_size > 2 << 30:
                        raise PackageError(f"数据文件超过本地单文件 2 GiB 限制：{name}")
                self.limits(i, extra)

    def _read_small(self, name, limit):
        entry = self.archive.getinfo(self._prefix + name)
        if entry.file_size > limit:
            raise PackageError(f"题包配置或检查器过大：{name}")
        return self.archive.read(entry)

    def _names(self, i, extra):
        c = self.config
        prefix = "ex_" if extra else ""
        return tuple(f"{prefix}{c.get(kind + '_pre', c.get('problem_name', ''))}{i}.{c.get(kind + '_suf', suffix)}"
                     for kind, suffix in (("input", "in"), ("output", "ans")))

    def limits(self, i, extra=False):
        if not 1 <= i <= (self.n_ex_tests if extra else self.n_tests):
            raise PackageError(f"测试点编号超出范围：{i}")
        c = self.config
        point = f"_{i}" if not extra else None
        def value(key, default):
            return c.get(key + point, c.get(key, default)) if point else c.get(key, default)

        if point and "time_limit_ms" + point in c:
            seconds = _integer(c["time_limit_ms" + point], "time_limit_ms", 1, 1200000) / 1000
        elif point and "time_limit" + point in c:
            seconds = float(_decimal(c["time_limit" + point], "time_limit"))
        elif "time_limit_ms" in c:
            seconds = _integer(c["time_limit_ms"], "time_limit_ms", 1, 1200000) / 1000
        else:
            seconds = float(_decimal(c.get("time_limit", "1"), "time_limit"))
        if not 0 < seconds <= 1200:
            raise PackageError("时限须为正且不超过 1200 秒")
        return (seconds, _integer(value("memory_limit", "256"), "memory_limit", 1, 6144),
                _integer(value("output_limit", "64"), "output_limit", 1, 2048))

    def extract(self, i, directory, extra=False):
        self.limits(i, extra)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths = (directory / "input.txt", directory / "answer.txt")
        for name, path in zip(self._names(i, extra), paths):
            with self.archive.open(self._prefix + name) as src, path.open("wb") as dst:
                copyfileobj(src, dst, 1 << 18)
        return paths

    def compare(self, expected_path, actual_path):
        bits = 32 if self.checker == "yunagi-c-int32" else 64
        with Path(expected_path).open("rb") as answer, Path(actual_path).open("rb") as output:
            if self.checker == "noi-text":
                for i, (e, a) in enumerate(zip_longest(_noi_lines(answer), _noi_lines(output)), 1):
                    if e != a:
                        return "WA", f"第 {i} 行应为 {_show(e)}，实际为 {_show(a)}"
                return "AC", ""
            expected = _words(answer)
            actual = _words(output)
            if bits == 32:
                # The original C checker reads only the first reference answer.
                v = next(expected, None)
                if not _valid_number(v, bits):
                    return "ERROR", "参考答案不是一个 32 位有符号整数"
                expected = iter((v,))
            for i, (e, a) in enumerate(zip_longest(expected, actual), 1):
                if e is not None and not _valid_number(e, bits):
                    return "ERROR", f"参考答案的第 {i} 项不是有效的 {bits} 位有符号整数"
                if e is None:
                    return "WA", f"从第 {i} 项起有多余输出，读到 {_show(a)}"
                if a is None or a != e:
                    return "WA", f"第 {i} 项应为 {_show(e)}，实际为 {_show(a)}"
        return "AC", ""

    def score(self, results, extra_results=None):
        if set(results) != set(range(1, self.n_tests + 1)):
            raise PackageError("正式点没有全部评测，不能计算正式分数")
        if any(s == "ERROR" for s in results.values()):
            raise PackageError("评测错误，无法计分")
        if self.groups:
            points = sum((s for lo, hi, s in self.groups if all(results[i] == "AC" for i in range(lo, hi+1))), Decimal(0))
        else:
            points = self._full * sum(v == "AC" for v in results.values()) / self.n_tests
        if extra_results is not None and all(v == "AC" for v in results.values()):
            if set(extra_results) != set(range(1, self.n_ex_tests + 1)):
                raise PackageError("额外测试点的结果不完整")
            if "ERROR" in extra_results.values():
                raise PackageError("额外测试点评测错误，无法计分")
            if any(v != "AC" for v in extra_results.values()):
                points = max(Decimal(0), points - 3)
        if self._precision is not None:
            points = points.quantize(Decimal(1).scaleb(-self._precision), rounding=ROUND_HALF_UP)
        return float(points)

    def score_parts(self, results, total):
        """Individual units whose best scores can be combined across submissions."""
        # A global penalty for failing an extra test couples all formal points.
        if self.checker != "noi-text" and self.n_ex_tests:
            return {"task": total}
        if self.groups:
            units = [(f"subtask-{i}", score, lo, hi)
                     for i, (lo, hi, score) in enumerate(self.groups, 1)]
        else:
            share = self._full / self.n_tests
            units = [(f"test-{i}", share, i, i) for i in range(1, self.n_tests + 1)]
        if self._precision is not None:
            quantum = Decimal(1).scaleb(-self._precision)
            if any(score != score.quantize(quantum) for _, score, _, _ in units):
                # The package rounds its total, not its individual subtasks.
                return {"task": total}
        return {key: float(score) if all(results.get(i) == "AC" for i in range(lo, hi + 1)) else 0.0
                for key, score, lo, hi in units}
