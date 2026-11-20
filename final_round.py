"""Persist OI final reports and IOI best scores across app sessions."""
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import stat
import tempfile

from candidate import _state_dir, contest_ended


def record_ioi(exam, pid, result):
    if exam.style != "ioi" or pid not in exam.ids:
        raise ValueError("请选择本场 IOI 比赛的有效题号。")
    if result["status"] in ("ERROR", "CANCELLED"):
        return
    parts = result["score_parts"]
    if any(not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0
           for score in parts.values()):
        raise ValueError("本次提交的计分项无效。")
    state = _state_dir(exam)
    state.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state / "ioi.sqlite3")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS best "
                   "(problem TEXT, part TEXT, score REAL NOT NULL, PRIMARY KEY (problem, part))")
        db.executemany("INSERT INTO best VALUES (?, ?, ?) ON CONFLICT(problem, part) "
                       "DO UPDATE SET score = max(best.score, excluded.score)",
                       ((pid, part, score) for part, score in parts.items()))


def ioi_scores(exam):
    parts = {pid: [] for pid in exam.ids}
    path = _state_dir(exam) / "ioi.sqlite3"
    if path.is_file():
        with closing(sqlite3.connect(path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'best'").fetchone():
                for pid, score in db.execute("SELECT problem, score FROM best"):
                    parts[pid].append(score)
    return {row["id"]: min(row["points"], math.fsum(parts[row["id"]])) for row in exam.problems}


def _results(db):
    return {pid: json.loads(result) for pid, result in db.execute(
        "SELECT problem, result FROM answers WHERE result IS NOT NULL")}


def saved_final(exam):
    path = _state_dir(exam) / "final.sqlite3"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'answers'").fetchone():
            return None
        results = _results(db)
    return results if len(results) == len(exam.ids) else None


def _snapshot(ws, row):
    pid = row["id"]
    # The original competition can be reopened against a different directory.
    # Read the chosen tree exactly once, including which answers were missing.
    try:
        source = ws.source_path(pid)
        if not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError("代码路径不是普通文件。")
        with source.open("rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("代码超过 1 MiB。")
        if not payload.strip():
            raise ValueError("代码文件为空。")
        return pid, payload, None
    except FileNotFoundError:
        reason = "未找到代码文件。"
    except ValueError as exc:
        reason = str(exc)
    return pid, None, json.dumps(dict(status="MISSING", summary=reason, score=0,
                                       max_score=row["points"]), ensure_ascii=False)


def grade_final(workspace, judge):
    exam = workspace.exam
    if exam.style != "oi" or not contest_ended(exam):
        raise ValueError("OI 比赛结束后才能评定最终成绩。")
    state = workspace.state
    state.mkdir(parents=True, exist_ok=True)
    # The first transaction publishes an entire immutable set of answers. The
    # second serializes grading, while other readers may still inspect the db.
    with closing(sqlite3.connect(state / "final.sqlite3", timeout=86400)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS answers "
                       "(problem TEXT PRIMARY KEY, source BLOB, result TEXT)")
            if not db.execute("SELECT 1 FROM answers LIMIT 1").fetchone():
                db.executemany("INSERT INTO answers VALUES (?, ?, ?)",
                               (_snapshot(workspace, row) for row in exam.problems))
        error = None
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM answers WHERE result IS NULL LIMIT 1").fetchone():
                return _results(db)
            with tempfile.TemporaryDirectory(prefix="selfeval-final-") as scratch:
                sources = {}
                for pid, payload in db.execute("SELECT problem, source FROM answers WHERE result IS NULL"):
                    folder = Path(scratch) / pid
                    folder.mkdir()
                    target = folder / (exam.basenames.get(pid, pid) + ".cpp")
                    target.write_bytes(payload)
                    sources[pid] = target
                graded = judge(exam, sources)
                for pid in sources:
                    verdict = graded.get(pid) or {}
                    score = verdict.get("score")
                    if (verdict.get("status") in ("ERROR", "CANCELLED")
                            or not isinstance(score, (float, int)) or not math.isfinite(score)):
                        error = verdict.get("summary") or "终场评测未完成，请重试。"
                        continue
                    report = {key: verdict.get(key) for key in ("status", "summary", "score", "max_score")}
                    db.execute("UPDATE answers SET result = ? WHERE problem = ?",
                               (json.dumps(report, ensure_ascii=False), pid))
        if error:
            raise ValueError(error)
        return _results(db)
