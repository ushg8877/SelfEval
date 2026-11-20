"""Student's terminal interface. The desktop and terminal open the same contest."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import select
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time

from candidate import CandidateWorkspace, contest_ended, last_workspace, require_contest_started
from exam import Exam, judge_exam, judge_final_oi
from final_round import grade_final, ioi_scores, saved_final


def open_workspace(exam, directory, prompt, submitting=False):
    if directory is None:
        previous = CandidateWorkspace.previous(exam) if submitting else last_workspace()
        if prompt:
            hint = f"（回车使用 {previous}）" if previous else ""
            directory = input(f"请选择一个已有的选手目录{hint}\n> ").strip() or previous
        else:
            directory = previous
        if not directory:
            raise ValueError("请先用 --workspace DIR 指定一个已有的选手目录。")
    return CandidateWorkspace(exam, directory)


def submit_batch(exam, submissions, *, submitted_at=None):
    print("正在评测…", flush=True)
    mode = "samples" if exam.style == "oi" else "full"
    options = {"submitted_at": submitted_at} if exam.style != "oi" else {}
    results = [(pid, judge_exam(exam.path, pid, source, mode, **options))
               for pid, source in submissions]
    if exam.style == "ioi":
        best = ioi_scores(exam)
    code = 0
    for pid, result in results:
        if exam.style == "ioi" and result.get("score") is not None:
            print(f"{exam.problem_label(pid)}　{result['summary']}　历史最佳 {best.get(pid, 0):g}")
        else:
            print(f"{exam.problem_label(pid)}　{result['summary']}")
        if exam.style in ("oi", "ioi"):
            for case in result["cases"]:
                memory = "—" if case["memory_kb"] is None else f"{case['memory_kb']} KiB"
                print(f"{case['number']:>5}  {case['status']:5}  {case['time_ms']:8} ms  "
                      f"{memory:>12}  {case.get('message', '')}")
        if result.get("diagnostic"):
            print(result["diagnostic"])
        if result["status"] != "AC":
            code = max(code, 2 if result["status"] == "ERROR" else 1)
    if exam.style == "ioi":
        print(f"当前总分  {sum(best.values()):g}")
    return code


def final_report(workspace):
    exam = workspace.exam
    if exam.style == "ioi":
        scores = ioi_scores(exam)
        print("终场成绩")
        for row in exam.problems:
            print(f"{exam.problem_label(row['id'])}　{scores.get(row['id'], 0):g}/{row['points']:g}")
        print(f"总分  {sum(scores.values()):g}/{sum(row['points'] for row in exam.problems):g}")
        return 0
    results = saved_final(exam)
    if results is None:
        print("比赛已结束，正在评测全部试题…", flush=True)
        results = grade_final(workspace, judge_final_oi)
    print("终场成绩")
    for pid in exam.ids:
        result = results[pid]
        print(f"{exam.problem_label(pid)}　{result['score']:g}/{result['max_score']:g}　{result['status']}　{result['summary']}")
    total = sum(r["score"] for r in results.values())
    maximum = sum(r["max_score"] for r in results.values())
    print(f"总分  {total:g}/{maximum:g}")
    return 0


def submit_questions(workspace, ids):
    exam = workspace.exam
    sources = [(pid, workspace.submission(pid)) for pid in dict.fromkeys(ids)]
    if exam.style == "ioi":
        submitted_at = time.time()
        with TemporaryDirectory(prefix="selfeval-submit-") as scratch:
            batch = []
            for pid, source in sources:
                snapshot = Path(scratch) / (pid + source.suffix)
                shutil.copyfile(source, snapshot)
                batch.append((pid, snapshot))
            return submit_batch(exam, batch, submitted_at=submitted_at)
    if exam.style != "oi" or workspace.deadline is None:
        return submit_batch(exam, sources, submitted_at=time.time())

    # Freeze final sources on time even if a sample compilation is still running.
    done = Event()

    def on_deadline():
        while not contest_ended(exam):
            if done.wait(min(1, max(0, workspace.deadline - time.time()))):
                return
        grade_final(workspace, judge_final_oi)

    with ThreadPoolExecutor(max_workers=1) as pool:
        watcher = pool.submit(on_deadline)
        try:
            status = submit_batch(exam, sources)
        finally:
            done.set()
            watcher.result()
    return status


def interactive_menu(exam, workspace):
    prompt = "输入本轮题号，多题用空格分隔（回车退出）"
    if exam.style == "oi" and workspace.deadline is None:
        prompt += "，输入 /finish 交卷并评测全部试题"
    while True:
        if exam.style == "standard" and workspace.deadline is not None and time.time() >= workspace.deadline:
            print("本轮练习已结束，可重新打开比赛并使用 --start 开始新一轮。" if exam.starts_at is None else "比赛已结束。")
            return 0
        if exam.style != "standard" and contest_ended(exam):
            return final_report(workspace)
        print(prompt + "\n> ", end="", flush=True)
        if workspace.deadline is not None:
            while time.time() < workspace.deadline:
                seconds = max(0, workspace.deadline - time.time())
                if select.select([sys.stdin], [], [], min(seconds, 1))[0]:
                    break
            if time.time() >= workspace.deadline:
                print()
                continue
        selection = input().strip()
        if not selection:
            return 0
        words = selection.split()
        try:
            if selection == "/finish" and exam.style == "oi" and workspace.deadline is None:
                workspace.finish()
                return final_report(workspace)
            submit_questions(workspace, words)
        except (OSError, ValueError) as exc:
            print(exc, file=sys.stderr)


def main(argv=None):
    default = Path(__file__).resolve().with_name("contest.seexam")
    ap = argparse.ArgumentParser(description="Linux 选手自测")
    ap.add_argument("--exam", default=str(default), help="比赛文件 .seexam")
    ap.add_argument("items", nargs="*", metavar="题号", help="本轮提交的若干题号")
    ap.add_argument("--workspace", type=Path, metavar="DIR", help="选手目录，以后可只输入题号")
    ap.add_argument("--start", action="store_true", help="开始自由练习赛")
    ap.add_argument("--finish", action="store_true", help="不限时 OI 比赛交卷，评测全部试题")
    ap.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pdf", type=Path, help="导出整册 PDF；给出题号则导出该题")
    a = ap.parse_args(argv)
    try:
        if not default.exists() and Path(a.exam) == default:
            ap.error("请用 --exam FILE.seexam 指定要打开的赛包。")
        e = Exam(a.exam)
        if a.pdf:
            if a.workspace or len(a.items) > 1 or a.start or a.finish or a.full:
                ap.error("--pdf 仅与题号及 --exam 一起使用")
            e.export_pdf(a.pdf, a.items[0] if a.items else None)
            return 0
        if e.style == "oi" and a.full:
            raise ValueError("OI 比赛在赛中只评测公开样例，交卷后统一评测正式数据。")
        if a.finish and (a.items or a.start or a.full):
            ap.error("--finish 应单独交卷")
        print(e.title)
        interactive = sys.stdin.isatty()
        action = bool(a.items) or a.finish or (e.style in ("oi", "ioi") and contest_ended(e) and
                                               CandidateWorkspace.previous(e) is not None)
        ws = open_workspace(e, a.workspace, interactive and not action, submitting=action)
        if a.start:
            ws.start()
        if a.finish:
            ws.finish()
        if e.style in ("oi", "ioi") and contest_ended(e):
            return final_report(ws)
        ids = list(dict.fromkeys(a.items))
        if ids:
            require_contest_started(e)
            result = submit_questions(ws, ids)
            if e.style in ("oi", "ioi") and contest_ended(e):
                return final_report(ws)
            return result
        if not ws.ready and e.starts_at is not None:
            local_start = datetime.fromtimestamp(e.starts_at).astimezone()
            print(f"开赛时间  {local_start:%Y-%m-%d %H:%M:%S %z}", flush=True)
            if not interactive:
                return 0
            while not ws.prepare():
                time.sleep(min(1, max(0, e.starts_at - time.time())))
        free_icpc = e.style == "standard" and e.starts_at is None
        if free_icpc and not a.start:
            action_label = "继续比赛" if ws.ready and not contest_ended(e) else "开始比赛"
            if not interactive:
                print(f"已准备，使用 --start {action_label}。")
                return 0
            if input(f"输入 start {action_label}（回车退出）\n> ").strip().lower() != "start":
                return 0
            ws.start()
        elif not ws.ready:
            if not interactive:
                print("已准备，使用 --start 开始比赛。")
                return 0
            if input("输入 start 开始比赛（回车退出）\n> ").strip().lower() != "start":
                return 0
            ws.start()
        if e.style == "standard" and contest_ended(e):
            print("比赛已结束。")
            return 0
        if ws.deadline is not None:
            finish = datetime.fromtimestamp(ws.deadline).astimezone()
            print(f"结束时间  {finish:%Y-%m-%d %H:%M:%S %z}")
        for row in e.problems:
            print(f"  {e.problem_label(row['id'])}")
        if interactive:
            return interactive_menu(e, ws)
        return 0
    except (EOFError, KeyboardInterrupt):
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
