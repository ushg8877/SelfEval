"""Remember the directory chosen at open and prepare materials at contest start."""
import errno
from contextlib import closing
from io import BytesIO
import json
import math
import os
import sqlite3
from pathlib import Path
import stat
import time
from tempfile import NamedTemporaryFile
from zipfile import ZipFile


def _state_root():
    xdg = Path(os.environ.get("XDG_STATE_HOME", ""))
    base = xdg if xdg.is_absolute() else Path.home() / ".local" / "state"
    return base / "selfeval"


def _state_dir(exam):
    return _state_root() / exam.fingerprint


def _remember(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as f:
        stage = Path(f.name)
    try:
        stage.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
        stage.replace(path)
    finally:
        stage.unlink(missing_ok=True)


def _recall(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return Path(value) if isinstance(value, str) and value else None
    except (FileNotFoundError, ValueError):
        return None


def last_gui_session():
    try:
        value = json.loads((_state_root() / "last-session.json").read_text(encoding="utf-8"))
        if isinstance(value, dict) and all(isinstance(value.get(key), str) for key in
                                           ("exam_path", "fingerprint", "directory")):
            return value
    except (OSError, ValueError):
        pass
    return None


def remember_gui_session(exam, workspace, entered, pid, view, **feedback):
    _remember(_state_root() / "last-session.json",
              dict(exam_path=str(exam.path), fingerprint=exam.fingerprint, directory=str(workspace.root),
                   entered=entered, deadline=workspace.deadline, pid=pid, view=view, **feedback))


def last_workspace():
    state = _state_root()
    latest = state / "last-directory.json"
    if latest.exists():
        return _saved_directory(latest)
    # Older clients recorded each selection under its contest fingerprint.
    records = (path for path in state.glob("*/directory.json") if len(path.parent.name) == 64)
    for record in sorted(records, key=lambda path: path.stat().st_mtime_ns, reverse=True):
        saved = _saved_directory(record)
        if saved:
            return saved
    return None


def _saved_directory(record):
    saved = _recall(record)
    return saved if saved and saved.is_absolute() and saved.is_dir() else None


def legacy_gui_session(exam, directory):
    # Older releases saved a clock and workspace for each fingerprint, but no package path.
    records = [entry for entry in _state_root().glob("*/directory.json")
               if len(entry.parent.name) == 64 and all(c in "0123456789abcdef" for c in entry.parent.name)]
    if records != [_state_dir(exam) / "directory.json"] or _saved_directory(records[0]) != directory:
        return None
    start = _contest_start(exam)
    deadline = start + exam.duration_minutes * 60 if start is not None and exam.duration_minutes is not None else None
    return dict(exam_path=str(exam.path), fingerprint=exam.fingerprint, directory=str(directory),
                entered=start is not None, deadline=deadline)


def _saved_start(exam):
    try:
        value = float((_state_dir(exam) / "started-at.json").read_text(encoding="ascii"))
        return value if math.isfinite(value) and value > 0 else None
    except (FileNotFoundError, ValueError):
        return None


def _practice_clock(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='clock'").fetchone():
        row = db.execute("SELECT started FROM clock WHERE id=1").fetchone()
        if row:
            return row[0]
    return None


def _contest_start(exam):
    if exam.starts_at is not None:
        return exam.starts_at
    path = _state_dir(exam) / "practice.sqlite3"
    if exam.style == "standard" and path.is_file():
        with closing(sqlite3.connect(path)) as db:
            start = _practice_clock(db)
            if start is not None:
                return start
    return _saved_start(exam)


def require_contest_started(exam):
    exam.require_started()
    if _contest_start(exam) is None:
        raise ValueError("请先选择选手目录，并点击「开始比赛」。")


def contest_ended(exam, now=None):
    started = _contest_start(exam)
    if started is None:
        return False
    if exam.duration_minutes is not None:
        return (time.time() if now is None else now) >= started + exam.duration_minutes * 60
    return (_state_dir(exam) / "ended-at.json").is_file()


def _missing_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"这里需要一个普通文件，请检查路径：{path}")
    return False


def _supply(path, contents):
    # Publish a complete file without replacing any existing contestant work.
    with NamedTemporaryFile(dir=path.parent, delete=False) as f:
        stage = Path(f.name)
    try:
        stage.write_bytes(contents)
        try:
            os.link(stage, path)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                _missing_file(path)
            elif exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
                # Some removable and shared filesystems do not support hard links.
                try:
                    stream = path.open("xb")
                except FileExistsError:
                    _missing_file(path)
                else:
                    with stream:
                        try:
                            stream.write(contents)
                            stream.flush()
                        except OSError:
                            stream.close()
                            path.unlink(missing_ok=True)
                            raise
            else:
                raise
    finally:
        stage.unlink(missing_ok=True)


def _check_source(path):
    if not path.is_file():
        raise ValueError(f"找不到本题的代码文件：{path}")
    if path.suffix.lower() not in (".cpp", ".cc", ".cxx", ".c++") and path.suffix != ".C":
        raise ValueError(f"请选择 C++ 源文件：{path}")


_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".C")


def _title_names(problem):
    names = [problem.get("basename", "")]
    for title in (problem["title"], problem.get("english_title")):
        if title:
            words = title.split()
            names.extend((title.strip(), "_".join(words), "-".join(words), "".join(words)))
    return names


def _icpc_source(directory, stems, exact_id=None):
    # Prefer the exact spelling on case-sensitive filesystems, then a case
    # variant. Compare entries instead of building a path from a contest title.
    entries = {}
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if exact_id and entry.stem.casefold() == exact_id.casefold() and entry.stem != exact_id:
            continue
        if entry.suffix in _CPP_SUFFIXES or entry.suffix.lower() in _CPP_SUFFIXES[:-1]:
            entries.setdefault(entry.name.casefold(), []).append(entry)
    for stem in stems:
        for suffix in _CPP_SUFFIXES:
            name = stem + suffix
            matches = entries.get(name.casefold(), ())
            for path in sorted(matches, key=lambda p: p.name != name):
                if path.is_file():
                    return path
    return None


class CandidateWorkspace:
    def __init__(self, exam, directory):
        self.exam = exam
        self.root = Path(directory).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"请选择一个比赛目录：{self.root}")
        self.state = _state_dir(exam)
        self.ready = False
        self.deadline = None
        if exam.starts_at is not None and exam.duration_minutes is not None:
            self.deadline = exam.starts_at + exam.duration_minutes * 60
        self.prepare()
        if self.previous(exam) != self.root:
            _remember(self.state / "directory.json", str(self.root))
        recent = _state_root() / "last-directory.json"
        if _saved_directory(recent) != self.root:
            _remember(recent, str(self.root))

    def start(self):
        self.exam.require_started()
        if self.exam.style == "standard" and self.exam.starts_at is None:
            self.state.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.state / "practice.sqlite3")) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS clock(id INTEGER PRIMARY KEY, started REAL NOT NULL)")
                started = _practice_clock(db)
                if started is None:
                    started = _saved_start(self.exam)
                now = time.time()
                minutes = self.exam.duration_minutes
                if started is None or (minutes is not None and now >= started + minutes * 60):
                    started = now
                db.execute("INSERT OR REPLACE INTO clock VALUES(1, ?)", (started,))
        elif _contest_start(self.exam) is None:
            self.state.mkdir(parents=True, exist_ok=True)
            clock = self.state / "started-at.json"
            _supply(clock, str(time.time()).encode("ascii"))
            if _contest_start(self.exam) is None:
                raise ValueError("本场比赛的本地开赛时间记录无效。")
        return self.prepare()

    def prepare(self):
        started = _contest_start(self.exam)
        if started is None or (self.exam.starts_at is not None and time.time() < started):
            return False
        if not self.ready and self.exam.style in ("oi", "ioi") and not contest_ended(self.exam):
            self._prepare_oi()
        if self.exam.duration_minutes is not None:
            self.deadline = started + self.exam.duration_minutes * 60
        self.ready = True
        return True

    def finish(self):
        require_contest_started(self.exam)
        if self.exam.style != "oi" or self.exam.duration_minutes is not None:
            raise ValueError("只有不限时 OI 比赛可以手动交卷。")
        _supply(self.state / "ended-at.json", str(time.time()).encode("ascii"))

    @staticmethod
    def previous(exam):
        return _saved_directory(_state_dir(exam) / "directory.json")

    def _problem_dir(self, pid):
        if pid not in self.exam.ids:
            raise ValueError(f"没有题号 {pid}。")
        name = self.exam.basenames.get(pid, pid)
        folder = self.root / name
        if self.exam.style in ("oi", "ioi") and folder.is_symlink():
            raise ValueError(f"试题目录是符号链接，请换一个目录：{folder}")
        return folder

    def _prepare_oi(self):
        booklet = self.root / "problemset.pdf"
        if _missing_file(booklet):
            _supply(booklet, self.exam.public("booklet.pdf"))
        for row in self.exam.problems:
            pid = row["id"]
            folder = self._problem_dir(pid)
            folder.mkdir(exist_ok=True)
            for suffix in (".pdf", ".cpp"):
                target = folder / (folder.name + suffix)
                if _missing_file(target):
                    _supply(target, self.exam.public(f"pdf/{pid}.pdf") if suffix == ".pdf" else b"")
            # public_samples() stores these under ex_N; the paper cites N.in / N.ans.
            missing = []
            for i in range(1, row["samples"] + 1):
                for suffix in (".in", ".ans"):
                    target = folder / (str(i) + suffix)
                    if _missing_file(target):
                        missing.append(target)
            if missing:
                with ZipFile(BytesIO(self.exam.public(f"samples/{pid}.zip"))) as samples:
                    for target in missing:
                        _supply(target, samples.read("ex_" + target.name))

    def source_path(self, pid):
        folder = self._problem_dir(pid)
        if self.exam.style in ("oi", "ioi"):
            return folder / (folder.name + ".cpp")
        problem = self.exam.problems[self.exam.ids.index(pid)]
        # An alias shared by two tasks must never select the other task's code.
        reserved = {other.casefold() for other in self.exam.ids if other != pid}
        exact_id = pid if pid.casefold() in reserved else None
        for other in self.exam.problems:
            if other["id"] != pid:
                reserved.update(name.casefold() for name in _title_names(other))
        ids = [pid] if exact_id else [pid.lower(), pid]
        names = list(dict.fromkeys(ids + [name for name in _title_names(problem)
                                   if name and name.casefold() not in reserved]))
        found = _icpc_source(self.root, names, exact_id)
        if found:
            return found
        by_name = {}
        for item in sorted(self.root.iterdir(), key=lambda path: path.name):
            by_name.setdefault(item.name.casefold(), []).append(item)
        for name in names:
            for subdir in sorted(by_name.get(name.casefold(), ()), key=lambda p: p.name != name):
                if exact_id and subdir.name.casefold() == pid.casefold() and subdir.name != pid:
                    continue
                if subdir.is_dir():
                    found = _icpc_source(subdir, [subdir.name] + names, exact_id)
                    if found:
                        return found
        return self.root / (ids[0] + ".cpp")

    def submission(self, pid):
        require_contest_started(self.exam)
        if (self.deadline is not None and time.time() >= self.deadline) or contest_ended(self.exam):
            if self.exam.style == "standard" and self.exam.starts_at is None:
                raise ValueError("本轮练习已结束，请点击「开始比赛」或使用 --start 开始新一轮。")
            raise ValueError("比赛已结束，已停止接收提交。")
        self.prepare()
        path = self.source_path(pid)
        if self.exam.style in ("oi", "ioi"):
            _missing_file(path)
        _check_source(path)
        if not path.stat().st_size:
            raise ValueError(f"代码文件是空的，请先编写并保存：{path}")
        return path
