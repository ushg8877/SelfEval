"""Read-only exam window for Linux with Python 3.8+ and Tk."""

import os
from math import ceil
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Event, Thread
from time import strftime, time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from candidate import CandidateWorkspace, contest_ended, last_gui_session, last_workspace, legacy_gui_session, remember_gui_session
from exam import Exam, judge_exam, judge_final_oi
from final_round import grade_final, ioi_scores, saved_final


def install_picker_geometry(root):
    # Unix Tk calls PlaceWindow on every chooser opening, including when it
    # reuses a withdrawn dialog. Apply our geometry after Tk's own placement.
    def fit(command, code, result, operation):
        if int(code) != 0:
            return
        dialog = root.tk.splitlist(command)[1]
        if root.tk.call("winfo", "class", dialog) not in ("TkChooseDir", "TkFDialog", "TkMotifFDialog"):
            return
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if root.winfo_width() == 1 or root.winfo_height() == 1:
            root.update_idletasks()
        parent_w, parent_h = root.winfo_width(), root.winfo_height()
        if parent_w <= 1 or parent_h <= 1:
            return  # Let Tk place the chooser if the parent has no layout yet.
        border = max(0, root.winfo_rootx() - root.winfo_x())
        titlebar = max(0, root.winfo_rooty() - root.winfo_y())
        available_w, available_h = max(1, sw - 2 * border), max(1, sh - titlebar - border)
        width, height = min(parent_w, available_w), min(parent_h, available_h)
        x = max(0, min(root.winfo_x() + (parent_w - width) // 2, available_w - width))
        y = max(0, min(root.winfo_y() + (parent_h - height) // 2, available_h - height))
        root.tk.call("wm", "geometry", dialog, f"{width}x{height}+{x}+{y}")

    root.tk.call("trace", "add", "execution", "::tk::PlaceWindow", "leave", root.register(fit))


class SelfEval:
    def __init__(self, root, initial=None):
        self.root = root
        install_picker_geometry(root)
        self.exam = None
        self.path = None
        self.problems = []
        self.workspace = None
        self.compiler = None
        self.pdfs = tempfile.TemporaryDirectory(prefix="selfeval-pdf-")
        self.pdf_index = 0
        self.history_data = {}
        self.displayed_row = None
        self.active = False
        self.final_busy = False
        self.final_presented = False
        self.final_error = False
        self.final_queue = Queue()
        self.final_score = None
        self.ioi_best = {}
        self.dialog = None
        self.checks = {}
        self.clock_job = None
        self.waiting_for_start = False
        self.icpc_lobby = False
        self.closing = False
        self.cancel = Event()
        self.events = Queue()
        self.exam_name = tk.StringVar(value="尚未打开考试")
        self.question = tk.StringVar()
        self.count = tk.StringVar(value="本次会话已提交 0 题次")
        self.summary = tk.StringVar(value="打开考试后，选择试题开始评测。")
        self.build()
        self.initial_job = self.root.after_idle(self.open_initial, initial)

    def open_initial(self, initial):
        self.initial_job = None
        if self.exam is not None:
            return
        if initial is not None:
            self.load(Path(initial))
            return
        session = last_gui_session()
        if session:
            path = Path(session["exam_path"])
            if path.is_absolute() and path.is_file():
                self.load(path, session)
            else:
                self.prepare_hint.configure(text="上次使用的比赛文件已移动，请点击「打开考试」重新选择。")
            return
        default = Path(__file__).resolve().with_name("contest.seexam")
        if default.is_file():
            previous = last_workspace()
            if previous is None:
                self.load(default)
            else:
                try:
                    migrated = legacy_gui_session(Exam(default), previous)
                except (OSError, ValueError):
                    return
                if migrated:
                    self.load(default, migrated)

    def remember(self):
        if self.exam is not None and self.workspace is not None:
            pid = self.problem_id() if self.prob_box.current() >= 0 else None
            entered = bool(self.workspace.ready and not self.icpc_lobby and not self.waiting_for_start)
            rows = [row for row in self.history.get_children() if row in self.history_data]
            selected = self.history.selection()
            cases = self.details.selection()
            records = [dict(self.history_data[row], verdict=self.history.item(row, "values")[-1]) for row in rows]
            try:
                remember_gui_session(self.exam, self.workspace, entered, pid, self.view.get(),
                                     records=records, selected=rows.index(selected[0]) if selected and selected[0] in rows else None,
                                     selected_case=cases[0] if cases else None)
            except OSError as error:
                print(f"无法保存最近考试：{error}", file=sys.stderr)

    def cancel_initial(self):
        if self.initial_job is not None:
            self.root.after_cancel(self.initial_job)
            self.initial_job = None

    def build(self):
        root = self.root
        root.title("SelfEval by Kamome")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        width = min(sw, max(510, sw // 2))
        height = min(sh, max(345, sh // 2))
        root.geometry(f"{width}x{height}+{(sw-width)//2}+{(sh-height)//2}")
        root.minsize(min(340, width), min(220, height))
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=6)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        top.columnconfigure(0, weight=1)
        self.heading = font.nametofont("TkDefaultFont").copy()
        self.heading.configure(weight="bold")
        heading_row = ttk.Frame(top)
        heading_row.grid(row=0, column=0, columnspan=5, sticky="ew", pady=(0, 4))
        heading_row.columnconfigure(0, weight=1)
        title = ttk.Label(heading_row, textvariable=self.exam_name, font=self.heading, width=1)
        title.grid(row=0, column=0, sticky="ew")
        title.bind("<Configure>", lambda event: title.configure(wraplength=max(1, event.width - 4)))
        self.clock_label = ttk.Label(heading_row, font=self.heading, anchor="e")
        self.clock_label.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.clock_label.grid_remove()
        self.open_btn = ttk.Button(top, text="打开考试…", width=-1, command=self.open_file)
        self.open_btn.grid(row=1, column=1)
        self.book_btn = ttk.Button(top, text="查看题册", width=-1, command=self.view_book)
        self.book_btn.grid(row=1, column=2, padx=(6, 0))
        self.stop_btn = ttk.Button(top, text="停止", width=-1, command=self.stop)
        self.stop_btn.grid(row=1, column=3, padx=(6, 0))
        self.go_btn = ttk.Button(top, text="提交评测…", width=-1, command=self.choose_test)
        self.go_btn.grid(row=1, column=4, padx=(6, 0))
        self.finish_btn = ttk.Button(top, text="交卷", width=-1, command=self.manual_finish)
        self.finish_btn.grid(row=1, column=0, sticky="w")
        self.finish_btn.grid_remove()

        self.preparation = ttk.Frame(frame, padding=16)
        self.preparation.grid(row=3, column=0)
        self.prepare_hint = ttk.Label(self.preparation, justify="center", anchor="center")
        self.prepare_hint.grid(row=0, column=0)
        self.begin_btn = ttk.Button(self.preparation, text="开始比赛", command=self.begin_contest)
        self.begin_btn.grid(row=1, column=0, pady=(12, 0))

        results_area = ttk.Frame(frame)
        results_area.grid(row=3, column=0, sticky="nsew")
        results_area.columnconfigure(0, weight=1)
        results_area.rowconfigure(1, weight=1)
        self.view_tabs = ttk.Frame(results_area)
        self.view_tabs.grid(row=0, column=0, sticky="w")
        self.view = tk.StringVar(value="history")
        for value, name in (("history", "评测记录"), ("details", "详细信息")):
            ttk.Radiobutton(self.view_tabs, text=name, variable=self.view, value=value,
                            command=self.layout_results).pack(side="left", padx=(0, 6))
        columns = ttk.Panedwindow(results_area, orient="horizontal")
        columns.grid(row=1, column=0, sticky="nsew")
        left = ttk.Labelframe(columns, text="评测结果", padding=6)
        right = ttk.Labelframe(columns, text="详细信息", padding=6)
        columns.add(left, weight=1)
        columns.add(right, weight=2)
        self.columns, self.left, self.right = columns, left, right
        results_area.bind("<Configure>", lambda _event: self.layout_results())
        for panel in (left, right):
            panel.columnconfigure(0, weight=1)
            panel.rowconfigure(1, weight=1)
        ttk.Label(left, textvariable=self.count).grid(row=0, column=0, sticky="w", pady=(0, 6))
        body_font = font.nametofont("TkDefaultFont")
        style = ttk.Style(root)
        style.configure("SelfEval.Treeview", font=body_font,
                        rowheight=body_font.metrics("linespace") + root.winfo_pixels("5p"))
        _, self.history = self.table(left, [("时间", 74), ("试题", 140), ("结果", 94)])
        self.history.bind("<<TreeviewSelect>>", self.select_history)
        self.history.bind("<Double-Button-1>", self.open_details)
        paper = ttk.Frame(right)
        paper.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        paper.columnconfigure(1, weight=1)
        ttk.Label(paper, text="试题").grid(row=0, column=0, sticky="w")
        self.prob_box = ttk.Combobox(paper, textvariable=self.question, width=8, state="readonly")
        self.prob_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.prob_box.bind("<<ComboboxSelected>>", self.change_problem)
        self.paper_btn = ttk.Button(paper, text="查看题面", command=self.view_paper)
        self.paper_btn.grid(row=0, column=2)
        self.details_frame, self.details = self.table(right, [("样例", 72), ("结果", 68), ("用时", 76), ("内存", 76)])
        self.details.tag_configure("pass", foreground="#205d3c")
        self.details.tag_configure("fail", foreground="#9b331c")
        self.details.bind("<<TreeviewSelect>>", self.select_case)
        self.summary_label = ttk.Label(right, textvariable=self.summary, wraplength=380)
        self.summary_label.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        right.bind("<Configure>", lambda event: self.summary_label.configure(wraplength=max(1, event.width - 20)))

        self.note = tk.Text(frame, wrap="word", width=1, height=2, font="TkFixedFont",
                            state="disabled", padx=6, pady=4)
        self.note.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.note.grid_remove()
        self.contest_widgets = (self.book_btn, self.go_btn, results_area)
        self.refresh_controls()

    def layout_results(self):
        narrow = self.root.winfo_width() < self.left.winfo_pixels("430p")
        if narrow:
            self.view_tabs.grid()
            chosen = (self.left,) if self.view.get() == "history" else (self.right,)
        else:
            self.view_tabs.grid_remove()
            chosen = (self.left, self.right)
        if tuple(map(str, self.columns.panes())) != tuple(map(str, chosen)):
            for pane in self.columns.panes():
                self.columns.forget(pane)
            for pane in chosen:
                self.columns.add(pane, weight=1 if pane == self.left else 2)

    def open_details(self, _event=None):
        self.view.set("details")
        self.layout_results()

    @staticmethod
    def table(panel, columns):
        table_frame = ttk.Frame(panel)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        names = [name for name, _ in columns]
        table = ttk.Treeview(table_frame, columns=names, show="headings", height=7, selectmode="browse",
                             style="SelfEval.Treeview")
        text_font = font.nametofont("TkDefaultFont")
        for name, width in columns:
            table.heading(name, text=name, anchor="w")
            minimum = text_font.measure(name) + panel.winfo_pixels("10p")
            table.column(name, width=max(width, minimum), minwidth=minimum, anchor="w")
        table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scrollbar.set)
        return table_frame, table

    def refresh_controls(self):
        busy = self.active or self.final_busy
        entered = (self.workspace is not None and self.workspace.ready and not self.waiting_for_start
                   and (not self.icpc_lobby or self.active))
        ended = bool(self.workspace and self.workspace.deadline is not None and time() >= self.workspace.deadline)
        if entered and self.exam.style == "oi" and self.workspace.deadline is None:
            ended = contest_ended(self.exam)
        self.open_btn.configure(state="disabled" if busy else "normal")
        if self.active and not ended:
            self.stop_btn.grid()
        else:
            self.stop_btn.grid_remove()
        self.stop_btn.configure(state="normal" if self.active else "disabled")
        self.go_btn.configure(state="normal" if entered and not busy and (not ended or self.final_error) else "disabled",
                              text="重试总评" if ended and self.final_error else "提交评测…")
        if entered and self.exam.style == "oi" and self.exam.duration_minutes is None and not ended:
            self.finish_btn.grid()
        else:
            self.finish_btn.grid_remove()
        self.book_btn.configure(state="normal" if entered else "disabled")
        self.paper_btn.configure(state="normal" if entered else "disabled")
        self.prob_box.configure(state="readonly" if entered and not busy else "disabled")
        if entered:
            self.preparation.grid_remove()
            for widget in self.contest_widgets:
                widget.grid()
            if not self.prob_box.cget("values"):
                self.prob_box.configure(values=[self.exam.problem_label(p["id"]) for p in self.problems])
                self.prob_box.current(0)
                self.change_problem()
            return

        for widget in self.contest_widgets:
            widget.grid_remove()
        self.question.set("")
        self.prob_box.configure(values=())
        self.set_note("")
        self.preparation.grid()
        button_label = "开始比赛"
        if not self.exam:
            hint = "请先打开考试文件。"
        elif not self.workspace:
            hint = "请重新打开考试并选择比赛目录。"
        elif self.waiting_for_start:
            hint = "比赛将在开赛时自动开放。"
        elif self.icpc_lobby and self.workspace.ready and not ended:
            button_label = "继续比赛"
            hint = "本轮练习正在进行，点击「继续比赛」接着作答。"
        elif self.icpc_lobby and ended:
            hint = "上一轮练习已结束，点击「开始比赛」开启新一轮。"
        else:
            hint = "点击「开始比赛」后即可查看题目并开始作答。"
        self.prepare_hint.configure(text=hint)
        if self.exam and self.exam.starts_at is None:
            self.begin_btn.configure(text=button_label, state="normal" if self.workspace and not busy else "disabled")
            self.begin_btn.grid()
        else:
            self.begin_btn.grid_remove()

    def begin_contest(self):
        if (not self.workspace or self.active or self.final_busy or self.exam.starts_at is not None
                or (self.workspace.ready and not self.icpc_lobby)):
            return
        try:
            self.workspace.start()
        except (ValueError, OSError) as error:
            messagebox.showerror("开始比赛失败", str(error), parent=self.root)
            return
        self.icpc_lobby = False
        self.reset_clock()
        self.refresh_controls()
        self.remember()

    def open_file(self):
        self.cancel_initial()
        path = filedialog.askopenfilename(parent=self.root, title="打开考试",
                                          filetypes=[("考试文件", "*.seexam")])
        if path:
            self.load(Path(path))

    def load(self, path, session=None):
        self.cancel_initial()
        if self.active or self.final_busy:
            return
        try:
            exam = Exam(path)
            problems = list(exam.problems)
            if not problems:
                raise ValueError("考试中没有试题。")
        except Exception as error:
            messagebox.showerror("打开考试失败", str(error), parent=self.root)
            return
        try:
            if session and session["fingerprint"] != exam.fingerprint:
                session = None
            saved = Path(session["directory"]) if session else None
            if saved and saved.is_absolute() and saved.is_dir():
                directory = saved
            else:
                directory = filedialog.askdirectory(parent=self.root, title=f"选择比赛目录 · {exam.title}",
                                                    initialdir=str(last_workspace() or Path.home()), mustexist=True)
            if not directory:
                return
            workspace = CandidateWorkspace(exam, Path(directory))
            if session and saved != workspace.root:
                session = None
        except Exception as error:
            messagebox.showerror("打开比赛目录失败", str(error), parent=self.root)
            return
        same_exam = (self.exam is not None and self.exam.fingerprint == exam.fingerprint
                     and self.workspace is not None and self.workspace.root == workspace.root)
        if not same_exam:
            self.final_presented = False
            self.final_score = None
        self.final_error = False
        self.exam, self.path, self.problems = exam, path.resolve(), problems
        self.workspace = workspace
        self.icpc_lobby = exam.style == "standard" and exam.starts_at is None
        if (self.icpc_lobby and session and session.get("entered") is True and workspace.ready
                and session.get("deadline") == workspace.deadline and not contest_ended(exam)):
            self.icpc_lobby = False
        self.exam_name.set(getattr(exam, "title", None) or path.name)
        self.ioi_best = ioi_scores(exam) if exam.style == "ioi" else {}
        self.show_case_table(exam.style != "standard")
        self.details.heading("样例", text="样例" if exam.style == "oi" else "测试点")
        if not same_exam:
            self.prob_box.configure(values=())
            self.question.set("")
            self.clear_history()
        restored = []
        if session:
            final = saved_final(exam) if exam.style == "oi" and contest_ended(exam) else None
            for record in session.get("records", []):
                if not isinstance(record, dict) or record.get("pid") not in exam.ids:
                    continue
                if not all(key in record for key in ("time", "mode", "verdict", "summary", "cases", "note", "failure")):
                    continue
                if record["mode"] == "final" and not final:
                    continue
                pid = record["pid"]
                row = self.history.insert("", "end", values=(record["time"], exam.problem_label(pid), record["verdict"]))
                self.history_data[row] = record
                restored.append(row)
            if final and {rec["pid"] for rec in self.history_data.values() if rec["mode"] == "final"} == set(exam.ids):
                self.final_presented = True
                total = sum(result["score"] for result in final.values())
                maximum = sum(result["max_score"] for result in final.values())
                self.final_score = f"赛后总分  {total:g} / {maximum:g}"
            self.update_count()
        self.waiting_for_start = False
        self.reset_clock()
        self.refresh_controls()
        if session:
            pid = session.get("pid")
            if pid in exam.ids and self.prob_box.cget("values"):
                self.prob_box.current(exam.ids.index(pid))
                self.change_problem()
            if session.get("view") in ("history", "details"):
                self.view.set(session["view"])
                self.layout_results()
            index = session.get("selected")
            if type(index) is int and 0 <= index < len(restored) and self.prob_box.current() >= 0:
                self.history.selection_set(restored[index])
                self.history.see(restored[index])
                self.select_history()
                case = session.get("selected_case")
                if case in self.details.get_children():
                    self.details.selection_set(case)
                    self.details.see(case)
                    self.select_case()
        if exam.style == "ioi":
            self.update_count()
            if self.prob_box.current() >= 0 and not self.history.selection():
                self.change_problem()
        if not session or "records" not in session:
            self.remember()

    def clear_history(self):
        self.history_data.clear()
        self.history.delete(*self.history.get_children())
        self.details.delete(*self.details.get_children())
        if self.prob_box.current() >= 0:
            self.change_problem()
        else:
            self.summary.set("这道题还没有评测记录。")
            self.set_note("")
        self.update_count()

    def reset_clock(self):
        if self.clock_job is not None:
            self.root.after_cancel(self.clock_job)
        self.update_clock()

    def update_clock(self):
        self.clock_job = None
        now = time()
        start = self.exam.starts_at
        was_waiting = self.waiting_for_start
        self.waiting_for_start = start is not None and now < start
        release = (start is not None and not self.waiting_for_start and
                   self.workspace is not None and not self.workspace.ready)
        if release:
            try:
                self.workspace.prepare()
            except (ValueError, OSError) as error:
                self.workspace = None
                messagebox.showerror("准备比赛目录失败", str(error), parent=self.root)
        if release or was_waiting != self.waiting_for_start:
            self.refresh_controls()

        minutes = self.exam.duration_minutes
        deadline = self.workspace.deadline if self.workspace else None
        if self.exam.style == "standard" and deadline is not None and now >= deadline:
            self.close_dialog()
            self.icpc_lobby = start is None
            self.refresh_controls()
        ended = (self.exam.style == "oi" and self.workspace and self.workspace.ready and
                 contest_ended(self.exam))
        if ended and not (self.final_busy or self.final_presented or self.final_error):
            self.begin_final()
        if self.waiting_for_start:
            seconds, label = ceil(start - now), "距开赛"
        elif ended and minutes is None:
            self.clock_label.configure(text="比赛已结束")
            self.clock_label.grid()
            return
        elif minutes is None:
            self.clock_label.grid_remove()
            return
        elif start is None and deadline is None:
            seconds, label = minutes * 60, "待开始"
        elif self.icpc_lobby and now >= deadline and not self.active:
            seconds, label = minutes * 60, "新一轮时长"
            deadline = None
        else:
            if start is not None:
                deadline = start + minutes * 60
            seconds = max(0, ceil(deadline - now))
            label = "剩余时间" if seconds else "比赛已结束"
        if self.waiting_for_start or (deadline is not None and seconds):
            self.clock_job = self.root.after(1000, self.update_clock)
        elif self.exam.style == "ioi" and deadline is not None and seconds == 0:
            self.close_dialog()
            self.refresh_controls()
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        self.clock_label.configure(text=f"{label}  {hours:02d}:{minutes:02d}:{seconds:02d}")
        self.clock_label.grid()

    def problem_id(self):
        return self.problems[self.prob_box.current()]["id"]

    def manual_finish(self):
        if not self.workspace or not self.workspace.ready or self.final_busy:
            return
        if messagebox.askyesno("交卷", "交卷后将收取所有题目当前保存的代码，并开始正式评测。确认交卷吗？",
                               parent=self.root):
            try:
                self.workspace.finish()
            except (OSError, ValueError) as error:
                messagebox.showerror("交卷失败", str(error), parent=self.root)
                return
            self.reset_clock()

    def begin_final(self):
        if self.final_busy or self.final_presented:
            return
        self.close_dialog()
        self.final_busy, self.final_error = True, False
        if self.active:
            self.cancel.set()
        self.count.set("已收卷，正在评测全部试题…")
        self.summary.set("正在进行赛后总评…")
        self.refresh_controls()
        workspace = self.workspace

        def run_final():
            try:
                report = grade_final(workspace, judge_final_oi)
                self.final_queue.put((report, None))
            except Exception as error:
                self.final_queue.put((None, str(error)))

        Thread(target=run_final, daemon=True).start()
        self.root.after(60, self.poll_final)

    def poll_final(self):
        try:
            report, error = self.final_queue.get_nowait()
        except Empty:
            self.root.after(100, self.poll_final)
            return
        self.final_busy = False
        if error is not None:
            self.final_error = True
            self.count.set("总评未完成，可重试")
            self.set_note(error)
        else:
            self.final_presented = True
            total = sum(item["score"] for item in report.values())
            maximum = sum(item["max_score"] for item in report.values())
            self.final_score = f"赛后总分  {total:g} / {maximum:g}"
            for index, pid in enumerate(self.exam.ids):
                result = report[pid]
                score = f"{result['score']:g}/{result['max_score']:g}"
                row = self.history.insert("", index, values=("总评", self.exam.problem_label(pid), score))
                self.history_data[row] = dict(time="总评", pid=pid, mode="final", cases=[],
                                              summary=f"{score} 分　{result['summary']}", note="", failure=None)
            self.history.selection_set(self.history.get_children()[0])
            self.view.set("history")
            self.layout_results()
            self.update_count()
            self.select_history()
        self.refresh_controls()
        if self.closing and not self.active:
            self.close()

    def change_problem(self, _event=None):
        pid = self.problem_id()
        for row in self.history.get_children():
            if self.history_data[row]["pid"] == pid:
                self.history.selection_set(row)
                return
        selected = self.history.selection()
        if selected:
            self.history.selection_remove(*selected)
        self.details.delete(*self.details.get_children())
        if self.exam.style == "ioi":
            self.summary.set(f"{self.exam.problem_label(pid)}  历史最佳 {self.ioi_best.get(pid, 0):g} 分")
        else:
            self.summary.set("这道题还没有评测记录。")
        self.show_case_table(self.exam.style != "standard")
        self.set_note("")

    def choose_test(self):
        if (not self.workspace or not self.workspace.ready or self.active or self.final_busy
                or self.waiting_for_start or self.icpc_lobby):
            return
        if self.exam.style == "standard" and self.workspace.deadline is not None and time() >= self.workspace.deadline:
            self.reset_clock()
            return
        if self.exam.style == "oi" and contest_ended(self.exam):
            if not self.final_presented:
                self.begin_final()
            return
        if self.exam.style == "ioi" and contest_ended(self.exam):
            return
        if self.dialog is not None:
            self.dialog.lift()
            return
        box = tk.Toplevel(self.root)
        self.dialog = box
        box.title("提交评测")
        box.transient(self.root)
        box.protocol("WM_DELETE_WINDOW", self.close_dialog)
        form = ttk.Frame(box, padding=14)
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(1, weight=1)
        ttk.Label(form, text="勾选本轮要评测的试题").grid(row=0, column=0, sticky="w", pady=(0, 6))
        canvas = tk.Canvas(form, highlightthickness=0, borderwidth=0)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(form, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        rows = ttk.Frame(canvas)
        window = canvas.create_window(0, 0, window=rows, anchor="nw")
        rows.columnconfigure(1, weight=1)
        rows.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        box.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"))
        box.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"))
        first = None
        titles = []
        for i, problem in enumerate(self.problems):
            pid = problem["id"]
            self.checks[pid] = tk.BooleanVar(master=box, value=pid == self.problem_id())
            check = ttk.Checkbutton(rows, text=pid, variable=self.checks[pid])
            check.grid(row=i, column=0, sticky="w", pady=2)
            if first is None:
                first = check
            title = ttk.Label(rows, text=self.exam.names[pid], anchor="w", justify="left", wraplength=400)
            title.grid(row=i, column=1, sticky="ew", padx=8, pady=4)
            title.bind("<Button-1>", lambda _event, button=check: button.invoke())
            titles.append(title)
        def resize_titles(event):
            canvas.itemconfigure(window, width=event.width)
            label_space = max(1, event.width - rows.grid_bbox(column=0, row=0)[2] - 24)
            for title in titles:
                title.configure(wraplength=label_space)
        canvas.bind("<Configure>", resize_titles)
        rows.update_idletasks()
        sw, sh = box.winfo_screenwidth(), box.winfo_screenheight()
        longest = max(self.heading.measure(self.exam.names[p["id"]]) for p in self.problems)
        width = min(sw * 4 // 5, max(sw // 2, 440, longest + first.winfo_reqwidth() + 80))
        height = min(sh * 3 // 4, max(sh // 3, 250, rows.winfo_reqheight() + 150))
        box.geometry(f"{width}x{height}+{(sw-width)//2}+{(sh-height)//2}")
        canvas.configure(width=1, height=1)
        if self.exam.style == "oi":
            ttk.Label(form, text="公开样例自测").grid(row=2, column=0, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.close_dialog).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="提交", command=self.start).pack(side="left")
        box.bind("<Escape>", lambda _event: self.close_dialog())
        box.grab_set()
        first.focus_set()

    def close_dialog(self):
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None
            self.checks.clear()

    def view_book(self):
        self.open_pdf(None)

    def view_paper(self):
        if self.prob_box.current() >= 0:
            self.open_pdf(self.problem_id())

    def open_pdf(self, problem_id):
        if not self.workspace or not self.workspace.ready or self.waiting_for_start or self.icpc_lobby:
            return
        try:
            self.exam.require_started()
            self.pdf_index += 1
            pdf = Path(self.pdfs.name) / f"statement-{self.pdf_index}.pdf"
            self.exam.export_pdf(pdf, problem_id)
            viewer = subprocess.Popen(["xdg-open", str(pdf)], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            self.root.after(350, self.check_viewer, viewer)
        except Exception as error:
            messagebox.showerror("打开题面失败", str(error), parent=self.root)

    def check_viewer(self, viewer):
        status = viewer.poll()
        if status is None:
            self.root.after(1500, self.check_viewer, viewer)
        elif status != 0:
            messagebox.showerror("打开题面失败", "请检查 Linux 桌面的默认 PDF 阅读器。",
                                 parent=self.root)

    def start(self):
        submitted_at = time()
        if (self.active or self.final_busy or not self.workspace or not self.workspace.ready
                or self.waiting_for_start or self.icpc_lobby):
            return
        if (self.workspace.deadline is not None and submitted_at >= self.workspace.deadline
                or contest_ended(self.exam, now=submitted_at)):
            self.close_dialog()
            self.reset_clock()
            return
        selected = [pid for pid, checked in self.checks.items() if checked.get()]
        if not selected:
            messagebox.showwarning("无法提交", "请至少勾选一道题。", parent=self.dialog or self.root)
            return
        try:
            self.exam.require_started()
            sources = [(pid, self.workspace.submission(pid).resolve()) for pid in selected]
        except (ValueError, OSError) as error:
            messagebox.showwarning("无法提交", str(error), parent=self.dialog or self.root)
            return
        mode = "samples" if self.exam.style == "oi" else "full"
        if mode == "samples":
            self.compiler = shutil.which(self.compiler or os.environ.get("CXX") or "g++")
        if mode == "samples" and not self.compiler:
            choice = filedialog.askopenfilename(title="选择 C++ 编译器", parent=self.dialog or self.root,
                                                initialdir="/usr/bin")
            if not choice:
                return
            if not os.access(choice, os.X_OK):
                messagebox.showerror("编译器", "所选文件不能执行。", parent=self.root)
                return
            self.compiler = choice
        snapshot = None
        try:
            snapshot = tempfile.TemporaryDirectory(prefix="selfeval-submit-")
            frozen = []
            for index, (pid, source) in enumerate(sources):
                folder = Path(snapshot.name) / str(index)
                folder.mkdir()
                target = folder / source.name
                shutil.copyfile(source, target)
                frozen.append((pid, target))
        except OSError as error:
            if snapshot is not None:
                snapshot.cleanup()
            messagebox.showwarning("无法保存本轮提交", str(error), parent=self.dialog or self.root)
            return
        self.close_dialog()
        clock = strftime("%H:%M:%S")
        jobs = []
        for index, (pid, source) in enumerate(frozen):
            row = self.history.insert("", index, values=(clock, self.exam.problem_label(pid), "正在评测"))
            self.history_data[row] = dict(time=clock, pid=pid, mode=mode, cases=[], summary="正在评测",
                                          note="", failure=None)
            jobs.append((row, pid, source))
        self.active = True
        self.history.selection_set(jobs[0][0])
        self.history.see(jobs[0][0])
        self.select_history()
        self.cancel.clear()
        self.update_count()
        self.refresh_controls()
        Thread(target=self.run, args=(self.path, jobs, mode, snapshot,
                                      submitted_at if self.exam.style != "oi" else None), daemon=True).start()
        self.root.after(50, self.poll)

    def run(self, exam, jobs, mode, snapshot, submitted_at=None):
        results = []
        try:
            with snapshot:
                for row, pid, source in jobs:
                    if self.cancel.is_set():
                        result = {"status": "CANCELLED", "summary": "已停止"}
                    else:
                        try:
                            kwargs = {"submitted_at": submitted_at} if submitted_at is not None else {}
                            result = judge_exam(exam, pid, source, mode, cancel=self.cancel, compiler=self.compiler,
                                                **kwargs)
                        except Exception as error:
                            result = {"status": "ERROR", "summary": str(error)}
                    results.append((row, result))
        finally:
            self.events.put(results)

    def update_count(self):
        if self.exam and self.exam.style == "ioi":
            possible = sum(problem["points"] for problem in self.problems)
            self.count.set(f"当前总分  {sum(self.ioi_best.values()):g} / {possible:g}")
        else:
            self.count.set(self.final_score or f"本次会话已提交 {len(self.history_data)} 题次")

    def selected_record(self):
        row = self.history.selection()
        return self.history_data.get(row[0]) if row else None

    def show_case_table(self, visible):
        if visible:
            self.details_frame.grid()
            self.summary_label.grid_configure(row=2, sticky="ew")
        else:
            self.details_frame.grid_remove()
            self.summary_label.grid_configure(row=1, sticky="nw")

    def select_history(self, _event=None):
        record = self.selected_record()
        selection = self.history.selection()
        row_id = selection[0] if selection else None
        previous = self.details.selection() if row_id == self.displayed_row else ()
        self.displayed_row = row_id
        self.details.delete(*self.details.get_children())
        if self.exam and self.exam.style == "standard":
            self.show_case_table(bool(not self.active and record and record["cases"]))
        if self.active:
            self.summary.set("正在评测")
            self.set_note("")
            return
        if not record:
            return
        if self.prob_box.current() < 0:
            return
        if record["pid"] != self.problem_id():
            self.prob_box.current(next(i for i, p in enumerate(self.problems) if p["id"] == record["pid"]))
        self.details.heading("样例", text="样例" if record["mode"] == "samples" else "测试点")
        for idx, case in enumerate(record["cases"]):
            self.display_case(idx, case)
        self.show_summary(record)
        self.set_note(record["note"])
        if previous and previous[0] in self.details.get_children():
            row = previous[0]
        else:
            row = str(record["failure"])
        if row in self.details.get_children():
            self.details.selection_set(row)
            self.details.see(row)

    def display_case(self, idx, case):
        ms, kb = case.get("time_ms"), case.get("memory_kb")
        self.details.insert("", "end", iid=str(idx), tags=("pass" if case["status"] == "AC" else "fail",),
                            values=(case["number"], case["status"], "—" if ms is None else f"{ms} ms",
                                    "—" if kb is None else f"{kb / 1024:.1f} MiB"))

    def select_case(self, _event=None):
        selection = self.details.selection()
        record = self.selected_record()
        if record and selection:
            index = int(selection[0])
            if index < len(record["cases"]):
                c = record["cases"][index]
                unit = "样例" if record["mode"] == "samples" else "测试点"
                self.set_note(c.get("message") or f"{unit} {c['number']}　{c['status']}")

    def set_note(self, text):
        if not text:
            self.note.grid_remove()
            return
        self.note.grid()
        self.note.configure(state="normal")
        self.note.delete("1.0", "end")
        self.note.insert("1.0", text)
        self.note.configure(state="disabled")

    def show_summary(self, record):
        mode = {"samples": " · 样例", "final": " · 赛后总评", "full": ""}[record["mode"]]
        text = f"{self.exam.problem_label(record['pid'])}{mode}　{record['summary']}"
        if self.exam.style == "ioi":
            text += f"　历史最佳 {self.ioi_best.get(record['pid'], 0):g} 分"
        self.summary.set(text)

    def poll(self):
        try:
            results = self.events.get_nowait()
        except Empty:
            self.root.after(60, self.poll)
            return
        for row, result in results:
            self.finish(row, result)
        self.active = False
        if self.icpc_lobby:
            self.reset_clock()
        if self.exam.style == "ioi":
            self.ioi_best = ioi_scores(self.exam)
            self.update_count()
        if self.closing and not self.final_busy:
            self.close()
            return
        self.refresh_controls()
        self.select_history()

    def finish(self, row, result):
        record = self.history_data[row]
        status = result.get("status", "ERROR")
        record["note"] = result.get("diagnostic", "") if status == "CE" else ""
        record["summary"] = result.get("summary") or status
        record["cases"] = result.get("cases", [])
        record["failure"] = next((i for i, case in enumerate(record["cases"])
                                  if case["status"] != "AC"), None)
        label = status
        if self.exam.style == "ioi" and isinstance(result.get("score"), (int, float)):
            label = f"{status}  {result['score']:g} 分"
        self.history.item(row, values=(record["time"], self.exam.problem_label(record["pid"]), label))

    def stop(self):
        self.cancel.set()
        self.stop_btn.configure(state="disabled")

    def close(self):
        if self.active or self.final_busy:
            self.closing = True
            if self.active:
                self.stop()
        else:
            self.cancel_initial()
            self.remember()
            if self.clock_job is not None:
                self.root.after_cancel(self.clock_job)
            self.pdfs.cleanup()
            self.root.destroy()


def main(initial=None):
    if not sys.platform.startswith("linux"):
        raise SystemExit("请在 Linux 中运行自评测。")
    root = tk.Tk(className="SelfEval")
    SelfEval(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) == 2 else None)
