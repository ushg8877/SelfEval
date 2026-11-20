"""Check which parts of a judge result reach the contestant."""
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exam import student_result
from gui import SelfEval
from menu import submit_batch


def judge_result(status):
    cases = [
        {"number": "1", "status": "AC", "extra": False,
         "time_ms": 1.75, "memory_kb": 2048, "message": "private checker output"},
        {"number": "附加 1", "status": status, "extra": True,
         "time_ms": 3, "memory_kb": 4096, "stderr": "private stderr"},
    ]
    return {"status": status, "cases": cases, "summary": "private runner summary",
            "diagnostic": "compiler diagnostic" if status == "CE" else "private diagnostics",
            "failure_dir": "/private/tests", "score": 12 if status == "AC" else 7,
            "max_score": 12, "score_parts": {"1": 7}}


class StudentFeedback(unittest.TestCase):
    def test_icpc_ac_includes_only_public_case_fields(self):
        result = student_result(SimpleNamespace(style="standard"), judge_result("AC"))
        self.assertEqual(result["status"], "AC")
        self.assertEqual(result["cases"], [
            {"number": "1", "status": "AC", "extra": False, "time_ms": 1.75, "memory_kb": 2048},
            {"number": "附加 1", "status": "AC", "extra": True, "time_ms": 3, "memory_kb": 4096},
        ])
        self.assertIsNone(result["score"])
        self.assertNotIn("private", repr(result))

    def test_icpc_unsuccessful_attempt_hides_all_cases(self):
        for status in ("WA", "TLE", "MLE", "OLE", "RE", "CE", "ERROR", "CANCELLED"):
            with self.subTest(status=status):
                result = student_result(SimpleNamespace(style="standard"), judge_result(status))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["cases"], [])
                self.assertIsNone(result["score"])
                self.assertIsNone(result["max_score"])
                self.assertNotIn("score_parts", result)
                self.assertNotIn("private", repr(result))
                self.assertEqual(result["diagnostic"], "compiler diagnostic" if status == "CE" else "")

    def test_ioi_partial_score_and_oi_sample_feedback(self):
        raw = judge_result("WA")
        ioi = student_result(SimpleNamespace(style="ioi"), raw)
        self.assertEqual([case["status"] for case in ioi["cases"]], ["AC", "WA"])
        self.assertEqual((ioi["score"], ioi["max_score"], ioi["score_parts"]), (7, 12, {"1": 7}))
        self.assertNotIn("private", repr(ioi))
        self.assertEqual(student_result(SimpleNamespace(style="oi"), raw), raw)

    def test_icpc_cli_mixed_batch(self):
        exam = SimpleNamespace(path=Path("contest.seexam"), style="standard", ids=["A", "B"],
                               problem_label=lambda pid: pid, test_archive=lambda: nullcontext(None))
        with tempfile.NamedTemporaryFile(suffix=".cpp") as source:
            source.write(b"int main() {}")
            source.flush()
            src = Path(source.name)
            with patch("exam.Exam", return_value=exam), patch("candidate.require_contest_started"), \
                    patch("candidate.contest_ended", return_value=False), \
                    patch("exam._official_result", side_effect=[judge_result("AC"), judge_result("WA")]):
                output = StringIO()
                with redirect_stdout(output):
                    code = submit_batch(exam, [("A", src), ("B", src)])
        self.assertEqual(code, 1)
        accepted, failed = output.getvalue().split("B　", 1)
        self.assertIn("附加 1", accepted)
        self.assertIn("4096 KiB", accepted)
        self.assertIn("WA", failed)
        self.assertNotIn("KiB", failed)
        self.assertNotIn("附加", failed)
        self.assertNotIn("private", output.getvalue())

    def test_history_switches_between_ac_failed_and_older_records(self):
        # No desktop is needed to exercise the production history controller.
        app = SelfEval.__new__(SelfEval)
        app.exam = SimpleNamespace(style="standard", problem_label=lambda pid: pid)
        app.active = False
        app.displayed_row = None
        app.prob_box = Mock()
        app.prob_box.current.return_value = 0
        app.problem_id = lambda: "A"
        app.details = Mock()
        app.details.get_children.return_value = ()
        app.details.selection.return_value = ()
        app.history = Mock()
        app.summary = Mock()
        app.show_case_table = Mock()
        app.display_case = Mock()
        app.set_note = Mock()
        app.history_data = {row: dict(time="12:00", pid="A", mode="full", summary="AC",
                                      cases=[], note="", failure=None) for row in ("ok", "wa", "old")}
        for row, verdict in (("ok", "AC"), ("wa", "WA")):
            app.finish(row, student_result(app.exam, judge_result(verdict)))
        for row, running, expected in (("ok", False, 2), ("wa", False, 0), ("old", False, 0),
                                       ("ok", True, 0), ("ok", False, 2)):
            with self.subTest(row=row, running=running):
                app.active = running
                app.history.selection.return_value = (row,)
                app.display_case.reset_mock()
                app.select_history()
                app.show_case_table.assert_called_with(bool(expected))
                self.assertEqual(app.display_case.call_count, expected)
                if running:
                    app.summary.set.assert_called_with("正在评测")


if __name__ == "__main__":
    unittest.main()
