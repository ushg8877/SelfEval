#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 - "$here" <<'PY'
"""Install the portable player and its contest for this Linux user."""
import errno
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def publish(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stage = Path(stream.name)
    try:
        stage.write_bytes(content)
        stage.chmod(0o644)
        stage.replace(path)
    finally:
        if stage.exists():
            stage.unlink()


def desktop_arg(value):
    # Exec quoting and desktop-entry string escaping are two distinct layers.
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in str(value))
    escaped = escaped.replace("\\", "\\\\").replace("%", "%%")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + escaped + '"'


def install(source):
    home = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not home.is_absolute():
        home = Path.home() / ".local" / "share"
    versions = home / "kamome-selfeval" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    files = [source / name for name in (
        "selfeval", "gui.py", "menu.py", "candidate.py", "final_round.py", "exam.py", "judge.py",
        "packages.py", "linuxrunner.py", "meter.c", "boxed.py", "studio.py", "studio_data.py", "service.py",
        "selfeval-studio", "selfeval-studio.desktop", "docs/manual-zh.pdf",
        "README.md", "install.sh", "selfeval.svg", "selfeval.desktop", "selfeval-mime.xml")]
    if (source / "contest.seexam").is_file():
        files.append(source / "contest.seexam")

    # Activate the desktop entry only after the whole release has been copied.
    # An already-running GUI can continue to use its original code and exam.
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix=".install-", dir=versions) as temp:
        stage = Path(temp)
        for original in files:
            relative = original.relative_to(source)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = original.read_bytes()
            target.write_bytes(content)
            target.chmod(0o755 if relative.name in {"selfeval", "selfeval-studio", "install.sh"} else 0o644)
            digest.update(os.fsencode(relative) + b"\x00" + hashlib.sha256(content).digest())
        installed = versions / digest.hexdigest()
        try:
            stage.rename(installed)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not installed.is_dir():
                raise

    template = (installed / "selfeval.desktop").read_text(encoding="utf-8")
    entry = "Exec=/bin/sh " + desktop_arg(installed / "selfeval") + " --gui"
    template = template.replace("Exec=kamome-selfeval --gui", entry)
    icon = home / "icons/hicolor/scalable/apps/selfeval.svg"
    publish(icon, (installed / "selfeval.svg").read_bytes())
    publish(home / "applications/selfeval.desktop", template.encode("utf-8"))
    studio = (installed / "selfeval-studio.desktop").read_text(encoding="utf-8")
    studio = studio.replace("Exec=selfeval-studio", "Exec=/bin/sh " + desktop_arg(installed / "selfeval-studio"))
    publish(home / "applications/selfeval-studio.desktop", studio.encode("utf-8"))
    publish(home / "mime/packages/selfeval.xml", (installed / "selfeval-mime.xml").read_bytes())
    for tool, directory in (("update-mime-database", home / "mime"),
                            ("update-desktop-database", home / "applications")):
        if shutil.which(tool):
            subprocess.run([tool, str(directory)], check=False)
    print("安装完成。应用菜单中的 SelfEval 用于参赛，SelfEval Studio 用于组赛。")


try:
    install(Path(sys.argv[1]).resolve())
except OSError as error:
    sys.exit("安装 SelfEval 失败：" + str(error))
PY
