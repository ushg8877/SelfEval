"""Build an all-architecture desktop package from a SelfEval player release."""
import argparse
from hashlib import md5
from io import BytesIO
from pathlib import Path, PurePosixPath
import tarfile


APP = "kamome-selfeval"
BASE = "usr/share/" + APP
VERSION = "1.2.1"


def tar_gz(files):
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        directories = {parent for name in files for parent in PurePosixPath(name).parents}
        for directory in sorted(directories, key=lambda p: (len(p.parts), p.as_posix())):
            info = tarfile.TarInfo("." if not directory.parts else "./" + str(directory))
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            info.uname = info.gname = "root"
            archive.addfile(info)
        for name, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo("./" + name)
            info.size, info.mode = len(data), mode
            info.uname = info.gname = "root"
            archive.addfile(info, BytesIO(data))
    return buf.getvalue()


def write_member(stream, name, data):
    # Debian binary packages contain three ordinary System V ar members.
    header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}" + "`\n"
    stream.write(header.encode("ascii"))
    stream.write(data)
    if len(data) % 2:
        stream.write(b"\n")


def build(player, destination):
    files = {}
    with tarfile.open(player, "r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.parts[:1] != ("selfeval-linux",) or ".." in path.parts:
                raise ValueError(f"unexpected distribution entry: {member.name}")
            name = str(path.relative_to("selfeval-linux"))
            if name == "install.sh":
                continue
            data = archive.extractfile(member).read()
            if name in ("selfeval.desktop", "selfeval-studio.desktop"):
                target = "usr/share/applications/" + name
            elif name == "docs/manual-zh.pdf":
                target = "usr/share/doc/" + APP + "/manual-zh.pdf"
            elif name == "selfeval.svg":
                target = "usr/share/icons/hicolor/scalable/apps/selfeval.svg"
            elif name == "selfeval-mime.xml":
                target = "usr/share/mime/packages/selfeval.xml"
            else:
                target = BASE + "/" + name
            files[target] = (data, 0o755 if name in ("selfeval", "selfeval-studio") else 0o644)
    desktop = files["usr/share/applications/selfeval.desktop"][0].decode()
    if "Exec=kamome-selfeval --gui %f" not in desktop.splitlines() or "Terminal=false" not in desktop.splitlines():
        raise ValueError("desktop entry must launch the installed application")
    if "Icon=selfeval" not in desktop.splitlines() or "usr/share/icons/hicolor/scalable/apps/selfeval.svg" not in files:
        raise ValueError("desktop icon missing")
    for required in ("selfeval", "selfeval-studio", "studio.py", "studio_data.py", "gui.py", "meter.c"):
        if BASE + "/" + required not in files:
            raise ValueError("missing application file: " + required)
    wrapper = '#!/bin/sh\nexec /bin/sh /usr/share/kamome-selfeval/selfeval "$@"\n'
    files["usr/bin/" + APP] = (wrapper.encode(), 0o755)
    files["usr/bin/selfeval-studio"] = (wrapper.replace('/selfeval "$@"', '/selfeval-studio "$@"').encode(), 0o755)
    kib = sum((len(data) + 1023) // 1024 for data, _ in files.values())
    control = f"""Package: {APP}
Version: {VERSION}
Section: education
Priority: optional
Architecture: all
Maintainer: SelfEval maintainers <selfeval@localhost>
Depends: python3 (>= 3.8), python3-tk, g++, util-linux, xdg-utils, python3-cryptography, bubblewrap
Installed-Size: {kib}
Description: Offline programming contest player and contest authoring studio
 Create encrypted contests, import test data, and judge C++ submissions on Linux.
"""
    sums = "".join(f"{md5(data).hexdigest()}  {name}\n" for name, (data, _) in sorted(files.items()))
    temp = destination.with_suffix(".tmp")
    with temp.open("wb") as out:
        out.write(b"!<arch>\n")
        write_member(out, "debian-binary", b"2.0\n")
        write_member(out, "control.tar.gz", tar_gz({"control": (control.encode(), 0o644),
                                                   "md5sums": (sums.encode(), 0o644)}))
        write_member(out, "data.tar.gz", tar_gz(files))
    temp.replace(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.player, args.destination)
