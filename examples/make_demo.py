"""Generate the editable two-problem contest used in the Chinese manual."""
import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def pdf(pages):
    """Make small, self-contained text PDFs using PDF's standard Helvetica font."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for title, lines in pages:
        page, content = len(objects) + 1, len(objects) + 2
        kids.append(f"{page} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>".encode())
        commands = ["BT", "/F1 22 Tf", "52 758 Td"]
        for i, line in enumerate([title] + lines):
            if i == 1:
                commands.extend(["/F1 12 Tf", "0 -41 Td"])
            elif i > 1:
                commands.append("0 -22 Td")
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj")
        stream = ("\n".join(commands + ["ET"]) + "\n").encode("ascii")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        data.extend(f"{off:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return data


def generate(root):
    if root.exists() and any(root.iterdir()):
        raise ValueError("请选择空目录或新目录，以保留现有工程文件。")
    root.mkdir(parents=True, exist_ok=True)
    problems = [
        ("A", "Hello World", ["Print Hello World on one line.", "Input: The input is empty.",
                            "Output: Hello World (with one space)."],
         [("", "Hello World\n")], [("", "Hello World\n")]),
        ("B", "A+B", ["Read two integers a and b, each between -2000000000 and 2000000000, inclusive.",
                     "Print their sum on one line.", "Sample input: -7 4", "Sample output: -3"],
         [("2 3\n", "5\n"), ("-8 3\n", "-5\n"), ("2000000000 2000000000\n", "4000000000\n")],
         [("-7 4\n", "-3\n")]),
    ]
    (root / "statements").mkdir()
    (root / "packages").mkdir()
    pages = []
    rows = []
    for pid, title, desc, tests, samples in problems:
        page = (f"{pid}. {title}", desc)
        pages.append(page)
        statement, data = f"statements/{pid}.pdf", f"packages/{pid}.zip"
        (root / statement).write_bytes(pdf([page]))
        with ZipFile(root / data, "w", ZIP_DEFLATED) as z:
            config = ["use_builtin_judger on", "selfeval_checker noi-text", "time_limit 1",
                      "memory_limit 1024", "full_score 100", f"n_tests {len(tests)}",
                      f"n_ex_tests {len(samples)}", f"n_sample_tests {len(samples)}"]
            z.writestr("problem.conf", "\n".join(config) + "\n")
            for folder, prefix, cases in (("tests", "", tests), ("samples", "ex_", samples)):
                directory = root / "data" / pid / folder
                directory.mkdir(parents=True)
                for i, pair in enumerate(cases, 1):
                    for suffix, text in zip(("in", "ans"), pair):
                        name = f"{i}.{suffix}"
                        (directory / name).write_bytes(text.encode())
                        z.writestr(prefix + name, text)
        rows.append(dict(id=pid, title=title, pdf=statement, data=data))
    (root / "statements/booklet.pdf").write_bytes(pdf(pages))
    spec = dict(title="Hello & Sum", style="standard", duration_minutes=30,
                pdf="statements/booklet.pdf", problems=rows)
    (root / "contest.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("hello-sum"))
    args = parser.parse_args()
    try:
        generate(args.directory)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(args.directory / "contest.json")
