import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import release


def deb_payload(path):
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ValueError("invalid Debian package")
        while True:
            header = stream.read(60)
            if len(header) != 60:
                raise ValueError("Debian payload missing")
            size = int(header[48:58])
            if header[:16].strip().rstrip(b"/") == b"data.tar.gz":
                return tarfile.open(fileobj=io.BytesIO(stream.read(size)), mode="r:gz")
            stream.seek(size + size % 2, 1)


class ReleaseTest(unittest.TestCase):
    def test_isolated_source_build(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            source = tmp / "source.zip"
            release.source_archive(source)
            with ZipFile(source) as src:
                self.assertEqual(set(src.namelist()), {"SelfEval/" + p for p in release.SOURCE})
                self.assertTrue(all(not n.endswith((".seexam", ".zip", ".pyc")) for n in src.namelist()))
                src.extractall(tmp)
            project = tmp / "SelfEval"
            # Build from the clean source tree, with no dependency on the original checkout.
            (project / "unpublished.seexam").write_bytes(b"test-only content")
            (project / "private-answers.zip").write_bytes(b"test-only content")
            subprocess.run([sys.executable, str(project / "release.py")], cwd=tmp,
                           stdout=subprocess.PIPE, check=True)
            out = project / "dist"
            self.assertEqual(source.read_bytes(), (out / "SelfEval-source.zip").read_bytes())
            with tarfile.open(out / "selfeval-linux.tar.gz") as portable:
                self.assertEqual(set(portable.getnames()), {"selfeval-linux/" + p for p in release.PLAYER})
                for p in release.EXECUTABLES:
                    self.assertEqual(portable.getmember("selfeval-linux/" + p).mode & 0o777, 0o755)
                with deb_payload(out / "SelfEval-Linux.deb") as installed:
                    names = set(installed.getnames())
                    self.assertIn("./usr/bin/selfeval-studio", names)
                    self.assertIn("./usr/share/applications/selfeval-studio.desktop", names)
                    self.assertFalse(any(n.endswith((".seexam", ".zip", ".pyc")) for n in names))
                    self.assertEqual(installed.extractfile("./usr/share/doc/kamome-selfeval/manual-zh.pdf").read(),
                                     portable.extractfile("selfeval-linux/docs/manual-zh.pdf").read())
                    for name in ("studio.py", "studio_data.py"):
                        self.assertEqual(installed.extractfile("./usr/share/kamome-selfeval/" + name).read(),
                                         portable.extractfile("selfeval-linux/" + name).read())


if __name__ == "__main__":
    unittest.main()
