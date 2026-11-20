"""Test portable drafts and the organizer's interactive export workflow."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio
from exam import Exam


class StudioTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.draft = self.folder / "contest.json"
        self.pdf = self.folder / "booklet.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        with ZipFile(self.folder / "tests.zip", "w") as archive:
            archive.writestr("problem.conf", "use_builtin_judger on\nselfeval_checker noi-text\nn_tests 1\n")
            archive.writestr("1.in", "")
            archive.writestr("1.ans", "contestant output\n")
        self.spec = dict(title="训练", style="ioi", duration_minutes=120, pdf="booklet.pdf",
                         problems=[dict(id=i, title=name, english_title=name, basename=name.lower(),
                                        pdf="booklet.pdf", data="tests.zip")
                                   for i, name in (("A", "Alpha"), ("B", "Beta"))])
        studio.save_draft(self.draft, self.spec, self.folder)

    def test_save_as_from_a_different_directory(self):
        other = self.folder / "第二份"
        other.mkdir()
        target = other / "contest.json"
        loaded = studio.read_draft(self.draft)
        studio.save_draft(target, loaded, self.folder)
        portable = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(portable["pdf"], "../booklet.pdf")
        self.assertEqual(portable["problems"][1]["data"], "../tests.zip")
        self.assertEqual(studio.read_draft(target), loaded)
        self.assertEqual(studio.export_spec(loaded)["duration_minutes"], 120)

    def test_incomplete_draft_and_failed_export(self):
        self.spec["problems"][1]["data"] = ""
        studio.save_draft(self.draft, self.spec, self.folder)
        with self.assertRaisesRegex(ValueError, "B 的数据 ZIP"):
            studio.export_spec(studio.read_draft(self.draft))
        before = self.draft.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            studio.save_draft(self.draft, self.spec, self.folder)
        self.assertEqual(self.draft.read_bytes(), before)

    def test_no_output_can_replace_a_source(self):
        for source in (self.pdf, self.folder / "tests.zip", self.draft):
            before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "另选文件"):
                studio.write_exam(self.draft, source)
            self.assertEqual(source.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "另选文件"):
            studio.save_draft(self.pdf, self.spec, self.folder)
        alias = self.folder / "another.json"
        os.link(self.pdf, alias)
        with self.assertRaisesRegex(ValueError, "另选文件"):
            studio.save_draft(alias, self.spec, self.folder)
        self.assertEqual(alias.read_bytes(), self.pdf.read_bytes())

    @unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("SELFEVAL_TEST_DESKTOP"),
                         "requires a Tk display")
    def test_open_reorder_save_and_export(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        root = tk.Tk()
        self.addCleanup(root.destroy)
        studio.configure_gui(root)
        def widgets(widget):
            for child in widget.winfo_children():
                yield child
                yield from widgets(child)
        def button(caption):
            return next(w for w in widgets(root) if isinstance(w, ttk.Button) and w.cget("text") == caption)

        with patch.object(filedialog, "askopenfilename", return_value=str(self.draft)):
            button("打开草稿…").invoke()
        tree = next(w for w in widgets(root) if isinstance(w, ttk.Treeview))
        button("添加题目").invoke()
        with patch("studio_data.choose_data_package", return_value=str(self.folder / "tests.zip")) as importer:
            button("从数据目录制作…").invoke()
            self.assertEqual(importer.call_args.kwargs["problem_id"], "C")
            check_destination = importer.call_args.kwargs["check_destination"]
            with self.assertRaisesRegex(ValueError, "另选文件"):
                check_destination(self.folder / "tests.zip")
            check_destination(self.folder / "other.zip")
        button("确定").invoke()
        self.assertEqual(tree.set("2", "data"), "tests.zip")
        button("移除").invoke()
        tree.selection_set("1")
        button("上移").invoke()
        self.assertEqual(tree.set("0", "id"), "B")
        with patch.object(messagebox, "askyesnocancel", return_value=None):
            button("新建").invoke()
            self.assertEqual(len(tree.get_children()), 2)
            with patch.object(filedialog, "askopenfilename", return_value=str(self.draft)):
                button("打开草稿…").invoke()
                self.assertEqual(tree.set("0", "id"), "B")
        button("保存草稿").invoke()
        self.assertEqual(studio.read_draft(self.draft)["problems"][0]["id"], "B")

        output = self.folder / "practice.seexam"
        with patch.object(filedialog, "asksaveasfilename", return_value=str(output)), \
             patch.object(messagebox, "showerror") as error:
            button("导出比赛包…").invoke()
            deadline = time.monotonic() + 15
            while button("导出比赛包…").instate(["disabled"]) and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            self.assertFalse(error.called, error.call_args)
        contest = Exam(output)
        self.assertTrue(contest.local_grading)
        self.assertEqual((contest.title, contest.style, contest.duration_minutes, contest.ids),
                         ("训练", "ioi", 120, ["B", "A"]))
        self.assertEqual(contest.public("pdf/B.pdf"), self.pdf.read_bytes())
        with contest.test_archive() as archive:
            self.assertEqual(archive.read("A.zip"), (self.folder / "tests.zip").read_bytes())
        with ZipFile(output) as archive:
            self.assertFalse(any(name.endswith((".pdf", ".zip")) for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
