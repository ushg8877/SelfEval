"""Contest organizer's .seexam builder. The shared file format lives in exam.py."""
import argparse
from copy import deepcopy
import getpass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
import tempfile
from threading import Thread


def write_exam(spec_path, output):
    from exam import build_exam
    spec_path = Path(spec_path)
    protect_output(output, read_draft(spec_path), spec_path.parent, spec_path)
    build_exam(spec_path, Path(output))


def material_fields(spec):
    yield spec, "pdf"
    for problem in spec["problems"]:
        yield problem, "pdf"
        yield problem, "data"


def protect_output(output, spec, base, draft_path=None):
    target = Path(output).resolve()
    sources = [(base / Path(row[key]).expanduser()) for row, key in material_fields(spec) if row.get(key)]
    if draft_path is not None:
        sources.append(draft_path)
    for source in sources:
        if target == source.resolve() or (target.exists() and source.exists() and target.samefile(source)):
            raise ValueError(f"输出文件与草稿或引用的材料重名，请另选文件：{source}")


def draft_paths(spec, base, relative_to=None):
    """Make a draft's material references absolute, or rebase them for saving."""
    spec = deepcopy(spec)
    for entry, key in material_fields(spec):
        if not entry.get(key):
            continue
        path = (base / Path(entry[key]).expanduser()).resolve()
        entry[key] = str(path)
        if relative_to is not None:
            try:
                entry[key] = Path(os.path.relpath(path, relative_to)).as_posix()
            except ValueError:  # A Windows draft may reference another drive.
                pass
    return spec


def read_draft(path):
    from exam import contest_style
    draft = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(draft, dict) or not isinstance(draft.get("problems"), list):
        raise ValueError("草稿应是含 problems 列表的组赛 JSON 文件。")
    draft["style"] = contest_style(draft.get("style", "standard"))
    for key in ("title", "pdf", "starts_at"):
        if not isinstance(draft.get(key, ""), str):
            raise ValueError(f"草稿字段 {key} 应为文本。")
    if type(draft.get("duration_minutes", "")) not in (int, str):
        raise ValueError("草稿字段 duration_minutes 应为分钟数。")
    for i, row in enumerate(draft["problems"], 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 道题的资料格式有误。")
        for key in ("id", "title", "pdf", "data", "english_title", "basename"):
            if not isinstance(row.get(key, ""), str):
                raise ValueError(f"第 {i} 道题的 {key} 应为文本。")
        for key in ("id", "title", "pdf", "data"):
            row.setdefault(key, "")
    return draft_paths(draft, path.parent)


def save_draft(path, spec, base):
    path = path.resolve()
    protect_output(path, spec, base)
    portable = draft_paths(spec, base, relative_to=path.parent)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".selfeval-", suffix=".tmp",
                                     delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        temporary.write_text(json.dumps(portable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_spec(draft):
    from exam import contest_duration, contest_start, identifiers
    spec = deepcopy(draft)
    if not spec.get("title", "").strip():
        raise ValueError("请填写比赛名称。")
    if not spec["problems"]:
        raise ValueError("请至少添加一道题。")
    if len(spec["problems"]) > 100:
        raise ValueError("每场比赛最多支持 100 道题。")
    if "duration_minutes" in spec:
        spec["duration_minutes"] = contest_duration(spec["duration_minutes"])
    if "starts_at" in spec:
        spec["starts_at"] = contest_start(spec["starts_at"], allow_local=True).isoformat()
    def require_file(entry, key, label):
        if not entry.get(key) or not Path(entry[key]).is_file():
            raise ValueError(f"{label}缺失，请选择文件。")
    require_file(spec, "pdf", "整场题册")
    for number, row in enumerate(spec["problems"], 1):
        label = row.get("id") or f"第 {number} 道题"
        try:
            identifiers([row])
        except ValueError as exc:
            raise ValueError(f"{label}：{exc}") from exc
        require_file(row, "pdf", f"{label} 的题面 PDF")
        require_file(row, "data", f"{label} 的数据 ZIP")
    identifiers(spec["problems"])
    return spec


def configure_gui(root):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from exam import contest_start
    from gui import install_picker_geometry

    install_picker_geometry(root)
    root.title("SelfEval Studio 组赛器")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{min(940, int(sw*.94))}x{min(660, int(sh*.88))}")
    root.minsize(min(sw, 620), min(sh, 460))
    pane = ttk.Frame(root, padding=18)
    pane.pack(fill="both", expand=True)
    pane.columnconfigure(1, weight=1)
    pane.rowconfigure(6, weight=1)
    title, booklet = tk.StringVar(), tk.StringVar()
    minutes, starts = tk.StringVar(), tk.StringVar()
    style = tk.StringVar(value="standard")
    status = tk.StringVar(value="")
    rows = []
    current = None
    extra = {}
    saved = None
    completions = Queue()
    busy = False

    def base():
        return current.parent if current else Path.cwd()

    def snapshot():
        spec = dict(extra, title=title.get().strip(), pdf=booklet.get().strip(),
                    style=style.get(), problems=deepcopy(rows))
        duration, start = minutes.get().strip(), starts.get().strip()
        if duration:
            spec["duration_minutes"] = int(duration) if duration.isascii() and duration.isdecimal() and len(duration) < 16 else duration
        if start:
            try:
                start = contest_start(start, allow_local=True).isoformat()
            except (ValueError, OSError, OverflowError):
                pass  # An unfinished field can be saved and corrected later.
            spec["starts_at"] = start
        return spec

    def update_title(*_):
        dirty = snapshot() != saved
        name = current.name if current else "新比赛"
        root.title(f"{name}{' *' if dirty else ''} · SelfEval Studio 组赛器")

    def install(draft, path):
        nonlocal current, saved, extra
        current = path
        extra = {k: v for k, v in draft.items() if k not in
                 ("title", "pdf", "style", "problems", "duration_minutes", "starts_at")}
        title.set(draft.get("title", ""))
        booklet.set(draft.get("pdf", ""))
        minutes.set(str(draft.get("duration_minutes", "")))
        starts.set(draft.get("starts_at", ""))
        style.set(draft.get("style", "standard"))
        rows[:] = draft.get("problems", [])
        saved = snapshot()
        show_style()
        refresh(0)
        status.set("")
        update_title()

    def save(as_new=False):
        nonlocal current, saved
        if busy:
            return False
        dest = current
        if dest is None or as_new:
            chosen = filedialog.asksaveasfilename(parent=root, title="保存组赛草稿", initialdir=str(base()),
                     initialfile=current.name if current else "contest.json", defaultextension=".json",
                     filetypes=[("组赛草稿", "*.json")])
            if not chosen:
                return False
            dest = Path(chosen).resolve()
        try:
            draft = draft_paths(snapshot(), base())
            if dest.suffix.lower() != ".json":
                raise ValueError("请使用 .json 扩展名保存可编辑草稿。")
            save_draft(dest, draft, base())
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("保存失败", str(error), parent=root)
            return False
        current = dest
        booklet.set(draft["pdf"])
        rows[:] = draft["problems"]
        saved = snapshot()
        update_title()
        status.set(f"草稿已保存到 {dest}")
        return True

    def may_leave():
        if busy:
            return False
        if snapshot() == saved:
            return True
        choice = messagebox.askyesnocancel("保存草稿", "当前草稿有修改，要先保存吗？", parent=root)
        return choice is False or (choice is True and save())

    def new_project():
        if may_leave():
            install({}, None)

    def open_project():
        if busy:
            return
        chosen = filedialog.askopenfilename(parent=root, title="打开组赛草稿", initialdir=str(base()),
                                            filetypes=[("组赛草稿", "*.json"), ("全部文件", "*")])
        if not chosen:
            return
        path = Path(chosen).resolve()
        try:
            draft = read_draft(path)
        except (ValueError, OSError, RuntimeError) as error:
            messagebox.showerror("打开失败", str(error), parent=root)
            return
        if may_leave():
            # Saving the current draft may have updated the same destination.
            try:
                if current == path:
                    draft = read_draft(path)
            except (ValueError, OSError, RuntimeError) as error:
                messagebox.showerror("打开失败", str(error), parent=root)
                return
            install(draft, path)

    def file_entry(parent, row, label, variable, extension):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        def choose():
            chosen = filedialog.askopenfilename(parent=parent, title=label,
                    filetypes=[(extension.upper(), "*." + extension), ("全部文件", "*")])
            if chosen:
                variable.set(chosen)
        ttk.Button(parent, text="浏览…", command=choose).grid(row=row, column=2, padx=(8, 0))

    toolbar = ttk.Frame(pane)
    toolbar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
    for caption, action in (("新建", new_project), ("打开草稿…", open_project), ("保存草稿", save),
                            ("另存为…", lambda: save(True))):
        ttk.Button(toolbar, text=caption, command=action).pack(side="left", padx=(0, 5))
    ttk.Label(pane, text="比赛名称").grid(row=1, column=0, sticky="w")
    ttk.Entry(pane, textvariable=title).grid(row=1, column=1, columnspan=2, sticky="ew")
    file_entry(pane, 2, "整场题册", booklet, "pdf")
    ttk.Label(pane, text="比赛时长（分钟）").grid(row=3, column=0, sticky="w", pady=4)
    duration_row = ttk.Frame(pane)
    duration_row.grid(row=3, column=1, columnspan=2, sticky="w")
    ttk.Entry(duration_row, textvariable=minutes, width=8).pack(side="left")
    ttk.Label(duration_row, text="留空表示不限时").pack(side="left", padx=8)
    ttk.Label(pane, text="固定开赛时间").grid(row=4, column=0, sticky="w", pady=4)
    start_row = ttk.Frame(pane)
    start_row.grid(row=4, column=1, columnspan=2, sticky="w")
    ttk.Entry(start_row, textvariable=starts, width=28).pack(side="left")
    ttk.Label(start_row, text="如 2026-11-21 09:00，留空可随时开始").pack(side="left", padx=8)
    columns = ("id", "title", "english_title", "basename", "pdf", "data")
    table = ttk.Frame(pane)
    table.grid(row=6, column=0, columnspan=3, sticky="nsew")
    table.rowconfigure(0, weight=1)
    table.columnconfigure(0, weight=1)
    tree = ttk.Treeview(table, show="headings", columns=columns, selectmode="browse", height=6)
    for key, label, width in zip(columns, ["题号", "题目名", "英文显示题名", "源文件名", "题面 PDF", "数据 ZIP"],
                                 [52, 128, 150, 100, 150, 120]):
        tree.heading(key, text=label)
        tree.column(key, width=width, minwidth=44, stretch=key != "id")
    tree.grid(row=0, column=0, sticky="nsew")
    sx = ttk.Scrollbar(table, orient="horizontal", command=tree.xview)
    sy = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
    tree.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
    sx.grid(row=1, column=0, sticky="ew")
    sy.grid(row=0, column=1, sticky="ns")
    def refresh(selected):
        tree.delete(*tree.get_children())
        for i, item in enumerate(rows):
            tree.insert("", "end", iid=str(i), values=(item["id"], item["title"], item.get("english_title", ""),
                        item.get("basename", "自动"),
                        Path(item["pdf"]).name, Path(item["data"]).name))
        if 0 <= selected < len(rows):
            tree.selection_set(str(selected))
            tree.see(str(selected))

    def edit(add=False):
        if busy or (not add and not tree.selection()):
            return
        index = len(rows) if add else int(tree.selection()[0])
        ids = {r["id"].casefold() for r in rows}
        number = 0
        while True:
            suggestion = chr(ord("A") + number) if number < 26 else str(number + 1)
            if suggestion.casefold() not in ids:
                break
            number += 1
        initial = ({"id": suggestion,
                    "title": "", "pdf": "", "data": ""} if add else rows[index])
        dialog = tk.Toplevel(root)
        dialog.title("添加题目" if add else "编辑题目")
        dialog.transient(root)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)
        fields = {key: tk.StringVar(value=initial.get(key, "")) for key in columns}
        for r, (key, caption) in enumerate((("id", "题号"), ("title", "题目名"),
                                            ("english_title", "英文显示题名（选填）"))):
            ttk.Label(form, text=caption).grid(row=r, column=0, sticky="w")
            ttk.Entry(form, width=46, textvariable=fields[key]).grid(row=r, column=1, columnspan=2, sticky="ew", pady=4)
        file_entry(form, 3, "本题题面", fields["pdf"], "pdf")
        file_entry(form, 4, "本题数据", fields["data"], "zip")
        def import_data():
            from studio_data import choose_data_package
            def check_destination(path):
                draft = snapshot()
                # This row can rebuild its own ZIP. Other rows may reference a shared ZIP.
                if not add:
                    draft["problems"][index]["data"] = ""
                draft["problems"].append({"pdf": fields["pdf"].get().strip()})
                protect_output(path, draft, base(), current)
            chosen = choose_data_package(dialog, problem_id=fields["id"].get(), initialdir=base(),
                                         check_destination=check_destination)
            if chosen:
                fields["data"].set(str(chosen))
        ttk.Button(form, text="从数据目录制作…", command=import_data).grid(row=5, column=1, sticky="w", pady=3)
        if style.get() in ("oi", "ioi"):
            ttk.Label(form, text="源文件名（不含 .cpp）").grid(row=6, column=0, sticky="w")
            ttk.Entry(form, textvariable=fields["basename"]).grid(row=6, column=1, columnspan=2, sticky="ew")
            ttk.Label(form, text="留空时使用数据包的文件读写名，或使用题号。").grid(
                row=7, column=1, columnspan=2, sticky="w")
        def accept():
            item = dict(initial, **{key: v.get().strip() for key, v in fields.items()})
            for key in ("basename", "english_title"):
                if not item[key]:
                    del item[key]
            if not item["id"]:
                messagebox.showerror("资料不完整", "请填写题号。", parent=dialog)
                return
            if any(i != index and x["id"].casefold() == item["id"].casefold() for i, x in enumerate(rows)):
                messagebox.showerror("题号重复", item["id"], parent=dialog)
                return
            if add:
                rows.append(item)
            else:
                rows[index] = item
            refresh(index)
            update_title()
            dialog.destroy()
        ttk.Button(form, text="确定", command=accept).grid(row=8, column=1, sticky="e", pady=(10, 0))
        ttk.Button(form, text="取消", command=dialog.destroy).grid(row=8, column=2, pady=(10, 0))

    def remove():
        if not busy and tree.selection():
            index = int(tree.selection()[0])
            rows.pop(index)
            refresh(min(index, len(rows) - 1))
            update_title()
    def move(delta):
        if busy or not tree.selection():
            return
        old = int(tree.selection()[0])
        new = old + delta
        if 0 <= new < len(rows):
            rows.insert(new, rows.pop(old))
            refresh(new)
            update_title()
    bar = ttk.Frame(pane)
    bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 6))
    for label, method in (("添加题目", lambda: edit(True)), ("编辑", edit), ("移除", remove),
                          ("上移", lambda: move(-1)), ("下移", lambda: move(1))):
        ttk.Button(bar, text=label, width=-1, command=method).pack(side="left", padx=(0, 5))
    def show_style():
        visible = columns if style.get() in ("oi", "ioi") else tuple(c for c in columns if c != "basename")
        tree.configure(displaycolumns=visible)
    for value, caption in (("standard", "ICPC"), ("ioi", "IOI"), ("oi", "OI")):
        ttk.Radiobutton(bar, text=caption, variable=style, value=value,
                        command=show_style).pack(side="right", padx=(8, 0))
    show_style()
    tree.bind("<Double-1>", lambda event: edit())

    def poll():
        nonlocal busy
        try:
            ok, detail = completions.get_nowait()
        except Empty:
            root.after(120, poll)
            return
        busy = False
        go.configure(state="normal")
        status.set(detail)
        if not ok:
            messagebox.showerror("生成失败", detail, parent=root)

    def generate():
        nonlocal busy
        if busy:
            return
        try:
            spec = export_spec(draft_paths(snapshot(), base()))
        except (ValueError, OverflowError, OSError, RuntimeError) as exc:
            messagebox.showerror("请检查比赛资料", str(exc), parent=root)
            return
        destination = filedialog.asksaveasfilename(parent=root, title="导出比赛包", initialdir=str(base()),
                        defaultextension=".seexam", filetypes=[("比赛包", "*.seexam")])
        if not destination:
            return
        if Path(destination).suffix.lower() != ".seexam":
            messagebox.showerror("文件名有误", "请使用 .seexam 扩展名导出比赛包。", parent=root)
            return
        try:
            protect_output(destination, spec, base(), current)
        except (ValueError, OSError, RuntimeError) as error:
            messagebox.showerror("导出路径有误", str(error), parent=root)
            return
        def worker():
            try:
                with tempfile.TemporaryDirectory(prefix="selfeval-studio-") as work:
                    manifest = Path(work) / "spec.json"
                    manifest.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
                    write_exam(manifest, destination)
            except Exception as exc:
                completions.put((False, str(exc)))
            else:
                completions.put((True, f"已生成 {destination}"))
        busy = True
        go.configure(state="disabled")
        status.set("正在检查资料并加密…")
        Thread(target=worker, daemon=False).start()
        root.after(120, poll)

    def close():
        if busy:
            messagebox.showinfo("正在生成", "请等待当前比赛包写入完成。", parent=root)
        elif may_leave():
            root.destroy()

    go = ttk.Button(pane, text="导出比赛包…", command=generate)
    go.grid(row=9, column=2, sticky="e", pady=8)
    ttk.Label(pane, textvariable=status, wraplength=530).grid(row=9, column=0, columnspan=2, sticky="w", pady=8)
    root.protocol("WM_DELETE_WINDOW", close)
    for variable in (style, title, minutes, starts, booklet):
        variable.trace_add("write", update_title)
    install({}, None)
    for sequence, action in (("<Control-n>", new_project), ("<Control-o>", open_project), ("<Control-s>", save)):
        root.bind(sequence, lambda event, invoke=action: invoke())


def build_gui():
    import tkinter as tk
    root = tk.Tk(className="SelfEvalStudio")
    configure_gui(root)
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="制作本地评测比赛包")
    sub = parser.add_subparsers(dest="action")
    build = sub.add_parser("build", help="从 JSON 组赛清单生成比赛包")
    build.add_argument("spec", type=Path)
    build.add_argument("output", type=Path)
    upgrade = sub.add_parser("enable-local", help="为旧赛包启用本机正式评测")
    upgrade.add_argument("exam", type=Path)
    upgrade.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            write_exam(args.spec, args.output)
            print(f"已生成 {args.output}")
        elif args.action == "enable-local":
            from exam import enable_local
            if args.output.exists():
                raise ValueError("请指定一个新的文件名保存转换后的赛包。")
            enable_local(args.exam, args.output, getpass.getpass("原赛包的教师口令："))
            print(f"已生成 {args.output}")
        else:
            if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
                parser.error("无图形桌面，请用 studio.py build spec.json output.seexam")
            build_gui()
    except (EOFError, KeyboardInterrupt):
        return 130
    except Exception as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
