"""Import local input/answer pairs into a data ZIP understood by Package."""
import os
from pathlib import Path
from queue import Empty, Queue
import re
import stat
from tempfile import NamedTemporaryFile
from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile

from packages import FILE_BASE, Package


def _order(stem):
    # Compare digit runs numerically, then use the original spelling to break ties.
    return [int(part) if i % 2 else part.casefold()
            for i, part in enumerate(re.split(r"([0-9]+)", stem))], stem


def _pairs(folder):
    files = {}
    for entry in folder.iterdir():
        role = entry.suffix.lower()
        if role not in (".in", ".out", ".ans"):
            continue
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"数据文件须为普通文件：{entry}")
        if info.st_size > 2 << 30:
            raise ValueError(f"数据文件超过 2 GiB：{entry}")
        key = "input" if role == ".in" else "answer"
        pair = files.setdefault(entry.stem, {})
        if key in pair:
            raise ValueError(f"{entry.name} 和 {pair[key].name} 同时匹配，请为每个测试点只保留一份输入和答案。")
        pair[key] = entry
    if not files:
        raise ValueError(f"未找到测试点：{folder}。请选择直接存放 .in 和对应 .ans 或 .out 的目录。")
    pairs = []
    for name in sorted(files, key=_order):
        pair = files[name]
        if "input" not in pair:
            raise ValueError(f"缺少输入：{folder / (name + '.in')}")
        if "answer" not in pair:
            raise ValueError(f"{pair['input'].name} 缺少同名的 .ans 或 .out 答案。")
        pairs.append((pair["input"], pair["answer"]))
    return pairs


def build_data_package(output, tests, samples=None, *, seconds=1, memory_mib=1024,
                       full_score=100, scoring="per-test", file_io=None):
    """Atomically write a ZIP from two separately selected flat directories."""
    from decimal import Decimal
    output = Path(output).expanduser().absolute()
    if output.suffix.lower() != ".zip":
        raise ValueError("请将题目数据包保存为 .zip 文件。")
    tests = Path(tests).expanduser().resolve(strict=True)
    samples = Path(samples).expanduser().resolve(strict=True) if samples else None
    if samples is not None and samples.samefile(tests):
        raise ValueError("正式数据和公开样例须分别选择不同的目录。")
    seconds = str(seconds).strip()
    if not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,3})?", seconds) or not 0 < Decimal(seconds) <= 1200:
        raise ValueError("时限须在 0.001 至 1200 秒之间，最多保留三位小数。")
    memory_mib = str(memory_mib).strip()
    if not re.fullmatch(r"[0-9]{1,4}", memory_mib) or not 1 <= int(memory_mib) <= 6144:
        raise ValueError("空间须为 1 至 6144 MiB 的整数。")
    full_score = str(full_score).strip()
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,9})?", full_score) or Decimal(full_score) <= 0:
        raise ValueError("题目满分须为正数（至多 10 位整数、9 位小数）。")
    if file_io is not None and not FILE_BASE.fullmatch(file_io):
        raise ValueError("读写文件名请填写小写字母开头的英文标识，可含小写字母、数字和下划线，最多 32 个字符。")
    if scoring not in ("per-test", "all"):
        raise ValueError("请选择逐点等分或全部通过计分。")
    formal = _pairs(tests)
    public = _pairs(samples) if samples else []
    # The exam writer duplicates public pairs in its standalone sample ZIP.
    if 2 * (len(formal) + len(public)) + 1 > 20000 or 4 * len(public) + 1 > 20000:
        raise ValueError("测试点数量超出题包可容纳的文件数。")
    config = ["use_builtin_judger on", "selfeval_checker noi-text",
              f"n_tests {len(formal)}", f"n_ex_tests {len(public)}", f"n_sample_tests {len(public)}",
              f"time_limit_ms {int(Decimal(seconds) * 1000)}", f"memory_limit {memory_mib}",
              f"full_score {full_score}"]
    if file_io:
        config.append(f"selfeval_file_io {file_io}")
    if scoring == "all":
        config += ["n_subtasks 1", f"subtask_end_1 {len(formal)}", f"subtask_score_1 {full_score}"]
    with NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as file:
        stage = Path(file.name)
    try:
        with ZipFile(stage, "w", ZIP_DEFLATED, compresslevel=3, strict_timestamps=False) as archive:
            archive.writestr("problem.conf", "\n".join(config) + "\n")
            for prefix, pairs in (("", formal), ("ex_", public)):
                for i, (inp, ans) in enumerate(pairs, 1):
                    archive.write(inp, f"{prefix}{i}.in")
                    archive.write(ans, f"{prefix}{i}.ans")
        with Package(stage):
            pass
        os.replace(stage, output)
    finally:
        stage.unlink(missing_ok=True)
    return output


def choose_data_package(parent, problem_id="", initialdir=None, check_destination=None):
    """Modal organizer dialog. Return the saved ZIP path, or None on cancel."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    win = tk.Toplevel(parent)
    win.title("从数据目录制作题包")
    win.transient(parent)
    form = ttk.Frame(win, padding=16)
    form.pack(fill="both", expand=True)
    form.columnconfigure(1, weight=1)
    fields = {}
    for row, (key, label, default) in enumerate((
            ("tests", "正式数据目录", ""), ("samples", "公开样例目录（选填）", ""),
            ("seconds", "时间限制（秒）", "1"), ("memory_mib", "空间限制（MiB）", "1024"),
            ("full_score", "题目满分", "100"))):
        var = fields[key] = tk.StringVar(win, default)
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(form, textvariable=var, width=44).grid(row=row, column=1, sticky="ew", pady=4)
        if key in ("tests", "samples"):
            def browse(variable=var, caption=label):
                chosen = filedialog.askdirectory(parent=win, title=caption, mustexist=True,
                        initialdir=variable.get() or initialdir or str(Path.home()))
                if chosen:
                    variable.set(chosen)
            ttk.Button(form, text="浏览…", command=browse).grid(row=row, column=2, padx=(8, 0))
    score = tk.StringVar(win, "逐点等分")
    ttk.Label(form, text="计分方式").grid(row=5, column=0, sticky="w", pady=4)
    ttk.Combobox(form, textvariable=score, values=("逐点等分", "全部通过才得分"),
                 state="readonly").grid(row=5, column=1, sticky="ew")
    file_io = tk.StringVar(win)
    ttk.Label(form, text="读写文件名（选填）").grid(row=6, column=0, sticky="w", pady=4)
    ttk.Entry(form, textvariable=file_io).grid(row=6, column=1, sticky="ew")
    ttk.Label(form, text="留空使用标准输入输出。填写 travel 时读 travel.in、写 travel.out。").grid(
        row=7, column=0, columnspan=3, sticky="w", pady=(2, 8))
    ttk.Label(form, text="每个 .in 配同名 .ans 或 .out，按文件名中的数字顺序编号。答案逐行比较。").grid(
        row=8, column=0, columnspan=3, sticky="w")
    status = tk.StringVar(win)
    ttk.Label(form, textvariable=status).grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
    buttons = ttk.Frame(form)
    buttons.grid(row=10, column=0, columnspan=3, sticky="e", pady=(8, 0))
    outcome = Queue()
    saved = None
    busy = False

    def close():
        if not busy:
            win.destroy()

    def poll():
        nonlocal busy, saved
        try:
            path, error = outcome.get_nowait()
        except Empty:
            win.after(100, poll)
            return
        busy = False
        if error:
            save_button.state(["!disabled"])
            cancel_button.state(["!disabled"])
            status.set("")
            messagebox.showerror("无法生成题目数据包", error, parent=win)
        else:
            saved = str(path)
            close()

    def save():
        nonlocal busy
        values = {k: var.get().strip() for k, var in fields.items()}
        if not values["tests"]:
            messagebox.showerror("资料不完整", "请选择正式数据目录。", parent=win)
            return
        # The proposed name is a leaf even while the parent dialog is being edited.
        stem = problem_id if re.fullmatch(r"[A-Za-z0-9_-]{1,36}", problem_id) else "data"
        destination = filedialog.asksaveasfilename(parent=win, title="保存题目数据包",
                initialdir=initialdir or str(Path(values["tests"]).parent), initialfile=stem + ".zip",
                defaultextension=".zip", filetypes=[("数据包", "*.zip")])
        if not destination:
            return
        if check_destination is not None:
            try:
                check_destination(Path(destination))
            except (ValueError, OSError, RuntimeError) as error:
                messagebox.showerror("保存路径有误", str(error), parent=win)
                return
        values.update(scoring="per-test" if score.get() == "逐点等分" else "all",
                      file_io=file_io.get().strip() or None)
        def generate():
            try:
                path = build_data_package(destination, **values)
            except Exception as exc:
                outcome.put((None, str(exc)))
            else:
                outcome.put((path, None))
        busy = True
        status.set("正在写入测试数据…")
        save_button.state(["disabled"])
        cancel_button.state(["disabled"])
        Thread(target=generate, daemon=False).start()
        win.after(100, poll)

    cancel_button = ttk.Button(buttons, text="取消", command=close)
    save_button = ttk.Button(buttons, text="保存为 ZIP…", command=save)
    cancel_button.pack(side="left", padx=(0, 6))
    save_button.pack(side="left")
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Escape>", lambda _event: close())
    previous = parent.grab_current()
    win.update_idletasks()
    win.grab_set()
    win.wait_window()
    if previous is not None and previous.winfo_exists():
        previous.grab_set()
    return saved
