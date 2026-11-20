"""Tests for importing paired local files and distributing public samples."""
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exam import Exam, build_exam
from packages import Package
from studio_data import build_data_package


class ImportData(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tests = self.root / "tests"
        self.samples = self.root / "samples"
        self.tests.mkdir()
        self.samples.mkdir()
        self.out = self.root / "A.zip"
        for stem in ("point10", "point2", "point1"):
            (self.tests / (stem + ".in")).write_bytes(stem.encode())
            (self.tests / (stem + ".ans")).write_bytes(b"private " + stem.encode())
        (self.samples / "example.in").write_bytes(b"example")
        (self.samples / "example.out").write_bytes(b"public answer")

    def test_assemble_and_open_player_package(self):
        build_data_package(self.out, self.tests, self.samples, seconds="0.125",
                           memory_mib=64, file_io="travel", full_score=12)
        with Package(self.out) as data:
            self.assertEqual(data.limits(2), (0.125, 64, 64))
            self.assertEqual((data.n_tests, data.n_samples, data.checker), (3, 1, "noi-text"))
            self.assertEqual([data.archive.read(f"{n}.in") for n in (1, 2, 3)],
                             [b"point1", b"point2", b"point10"])
            self.assertEqual(data.score({1: "WA", 2: "AC", 3: "AC"}), 8)
        # Exercise the same exporter and loader used by the organizer/player.
        (self.root / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        spec = {"style": "ioi", "title": "Test", "pdf": "paper.pdf", "problems": [
            {"id": "A", "title": "Travel", "pdf": "paper.pdf", "data": "A.zip"}]}
        project = self.root / "contest.json"
        project.write_text(json.dumps(spec), encoding="utf-8")
        target = self.root / "test.seexam"
        build_exam(project, target)
        player = Exam(target)
        self.assertEqual(player.basenames["A"], "travel")
        with player.test_archive() as archive:
            self.assertEqual(archive.read("A.zip"), self.out.read_bytes())
        with Package(BytesIO(player.public("samples/A.zip"))) as public:
            self.assertEqual(public.limits(1), (0.125, 64, 64))
            self.assertEqual(public.n_tests, 1)
            self.assertEqual(public.archive.read("1.ans"), b"public answer")
            self.assertNotIn(b"private", b"".join(public.archive.read(n) for n in public.archive.namelist()))

    def test_all_or_nothing_and_no_samples(self):
        build_data_package(self.out, self.tests, scoring="all")
        with Package(self.out) as data:
            self.assertEqual(data.n_samples, 0)
            self.assertIsNone(data.file_io)
            self.assertEqual(data.score({1: "WA", 2: "AC", 3: "AC"}), 0)
            self.assertEqual(data.score({1: "AC", 2: "AC", 3: "AC"}), 100)

    def test_reject_ambiguous_inputs_and_keep_old_output(self):
        self.out.write_bytes(b"previous archive")
        with self.assertRaises(ValueError):
            build_data_package(self.out, self.tests, self.tests)
        for name in ("point1.out", "other.ans", "other.in"):
            with self.subTest(name=name):
                path = self.tests / name
                path.touch()
                with self.assertRaises(ValueError):
                    build_data_package(self.out, self.tests)
                path.unlink()
                self.assertEqual(self.out.read_bytes(), b"previous archive")
        with patch.object(ZipFile, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                build_data_package(self.out, self.tests)
        self.assertEqual(self.out.read_bytes(), b"previous archive")
        self.assertFalse(list(self.root.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
