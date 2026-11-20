"""Build the public source ZIP, portable Linux archive, and Debian package."""
import argparse
from io import BytesIO
from pathlib import Path
import tarfile
from uuid import uuid4
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

import debian_package


ROOT = Path(__file__).resolve().parent
# Explicit lists keep locally stored exams, solutions, and credentials out of releases.
PLAYER = (
    "README.md", "selfeval", "selfeval-studio", "install.sh", "selfeval.desktop",
    "selfeval-studio.desktop", "selfeval.svg", "selfeval-mime.xml", "meter.c",
    "boxed.py", "candidate.py", "exam.py", "final_round.py", "gui.py", "judge.py",
    "linuxrunner.py", "menu.py", "packages.py", "service.py", "studio.py", "studio_data.py",
    "docs/manual-zh.pdf",
)
SOURCE = PLAYER + (
    ".gitignore", ".gitattributes", "debian_package.py", "release.py",
    "docs/manual_zh.py", "examples/make_demo.py",
    "tests/test_studio.py", "tests/test_studio_data.py", "tests/test_release.py",
    "tests/test_student_feedback.py",
)
EXECUTABLES = {"selfeval", "selfeval-studio", "install.sh"}


def read_entry(name):
    raw = (ROOT / name).read_bytes()
    return raw if name.endswith(".pdf") else raw.replace(b"\r\n", b"\n")


def portable_archive(destination):
    with tarfile.open(destination, "w:gz") as archive:
        for name in PLAYER:
            data = read_entry(name)
            member = tarfile.TarInfo("selfeval-linux/" + name)
            member.mode = 0o755 if name in EXECUTABLES else 0o644
            member.size = len(data)
            archive.addfile(member, BytesIO(data))


def source_archive(destination):
    with ZipFile(destination, "w") as archive:
        for name in SOURCE:
            member = ZipInfo("SelfEval/" + name)
            member.create_system = 3  # Unix permission bits are valid even when building on Windows.
            member.external_attr = (0o100755 if name in EXECUTABLES else 0o100644) << 16
            archive.writestr(member, read_entry(name), compress_type=ZIP_DEFLATED)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output directory (default: dist)")
    out = parser.parse_args(argv).out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Stage with the output directory's ordinary ACL. On Windows, moving files
    # out of a private TemporaryDirectory can leave finished releases unreadable.
    stage = out / (".build-" + uuid4().hex)
    stage.mkdir()
    source = stage / "SelfEval-source.zip"
    player = stage / "selfeval-linux.tar.gz"
    deb = stage / "SelfEval-Linux.deb"
    try:
        source_archive(source)
        portable_archive(player)
        debian_package.build(player, deb)
        for artifact in (source, player, deb):
            artifact.replace(out / artifact.name)
            print(out / artifact.name)
    finally:
        for artifact in (source, player, deb, deb.with_suffix(".tmp")):
            artifact.unlink(missing_ok=True)
        stage.rmdir()


if __name__ == "__main__":
    main()
