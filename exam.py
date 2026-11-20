"""Read the public part of a contest and seal/open its private tests."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import unicodedata
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from packages import Package


DEFAULT_SOCKET = "/run/selfeval/judge.sock"
LOCAL_ACCESS = "local-access.v1"


def wrapping_key(fingerprint, sealed_prefix):
    # The offline client can always recover this key. Wrapping makes the ZIP
    # self-contained without storing plaintext tests or a raw content key.
    context = fingerprint + sealed_prefix
    return hashlib.sha256(b"Kamome SelfEval offline key wrapping v1\x00" + context).digest()


def material_path(name):
    return "materials/" + hashlib.sha256(name.encode("utf-8")).hexdigest() + ".bin"


def material_cipher(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import hmac
    return AESGCM(hmac.digest(key, b"SelfEval player materials v2", "sha256"))


def material_aad(fingerprint, name):
    return b"SelfEval/material/v2\x00" + fingerprint + name.encode("utf-8")


def write_player_pack(stage, raw, sealed, data_key, public):
    from cryptography.hazmat.primitives.keywrap import aes_key_wrap
    fingerprint = hashlib.sha256(raw).digest()
    cipher = material_cipher(data_key)
    with ZipFile(stage, "x") as z:
        z.writestr("manifest.json", json.dumps({"format": "selfeval/2", "fingerprint": fingerprint.hex()}))
        z.writestr(LOCAL_ACCESS, aes_key_wrap(wrapping_key(fingerprint, sealed[:28]), data_key))
        z.writestr("tests.sealed", sealed)
        def material(name, path, data):
            nonce = os.urandom(12)
            z.writestr(path, nonce + cipher.encrypt(nonce, data, material_aad(fingerprint, name)))
        material("index", "materials/index.bin", raw)
        for name, data in public:
            material(name, material_path(name), data)

# The original example packs predate the duration field. Their authenticated
# manifests also identify their official judge services and saved workspaces.
LEGACY_DURATIONS = {
    "3c43a1f7759f3c3950f037e3aff9d78d69ba5ebb83d552cb773f51d65852be47": 210,  # CSP-J
    "a58a11a8d4854f80308aa1a422765dfea4f83f03e0431ed36400b4d1a084744f": 30,   # ICPC demo
    "7dfe4aa0d8066ff6a99130405f94d39aae6624e1bc0b512f3640fd79895349b3": 180,  # Yunagi
}


def personal_socket():
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache")
    return base / "selfeval-private" / "judge.sock"


def client_socket():
    if os.environ.get("SELFEVAL_SOCKET"):
        return os.environ["SELFEVAL_SOCKET"]
    return DEFAULT_SOCKET if Path(DEFAULT_SOCKET).exists() else str(personal_socket())


def digest(b):
    return hashlib.sha256(b).hexdigest()


def derive(secret, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.backends import default_backend
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1,
                  backend=default_backend()).derive(secret.encode("utf-8"))


def identifiers(rows):
    ids = [p["id"] for p in rows]
    if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids) or not all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,36}", v) for v in ids):
        raise ValueError("题号须唯一，请使用字母、数字、下划线或短横线。")
    if any(not isinstance(p["title"], str) or not p["title"].strip() for p in rows):
        raise ValueError("请输入每道题的标题。")
    if any("english_title" in p and not isinstance(p["english_title"], str) for p in rows):
        raise ValueError("英文显示题名应为文本。")
    return ids


def display_name(problem, basename):
    explicit = problem.get("english_title", "").strip()
    if explicit:
        return explicit
    title = problem["title"].strip()
    if all("LATIN" in unicodedata.name(ch, "") for ch in title if ch.isalpha()):
        return title
    if basename and basename.casefold() != problem["id"].casefold():
        return basename
    return "Problem " + problem["id"]


def contest_style(value):
    if value == "icpc":
        return "standard"
    if value not in ("oi", "ioi", "standard"):
        raise ValueError("赛制须为 oi、ioi 或 standard。")
    return value


def contest_duration(value):
    if type(value) is not int or value < 1:
        raise ValueError("比赛时长须为正整数，单位为分钟。")
    return value


def contest_start(value, *, allow_local=False):
    date_time = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    offset = r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?"
    if not isinstance(value, str) or not re.fullmatch(date_time + offset, value):
        raise ValueError("开赛时间须包含日期和时分，例如 2026-11-21T09:00:00+08:00。")
    try:
        start = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise ValueError("开赛时间无效，请检查日期、时间和时区。") from None
    if start.tzinfo is None:
        if not allow_local:
            raise ValueError("开赛时间须指定时区，例如 +08:00 或 Z。")
        options = [start.replace(fold=fold).astimezone() for fold in (0, 1)]
        if options[0].utcoffset() != options[1].utcoffset():
            raise ValueError("这个本地时间处于夏令时切换期间，请明确填写时区偏移。")
        start = options[0]
    return start


def check_basenames(names):
    for name in names.values():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("OI 题目文件名须由 1 至 64 个字母、数字、短横线或下划线组成。")
    if len({name.casefold() for name in names.values()}) != len(names):
        raise ValueError("OI 题目的目录名不能重复。")
    return names


def public_samples(pkg):
    count = pkg.n_samples
    if not count:
        return None
    buf = io.BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as z:
        time, memory, output = pkg.limits(1, extra=True)
        cfg = [f"n_tests {count}", f"n_ex_tests {count}", f"n_sample_tests {count}",
               "use_builtin_judger on", f"time_limit {time}", f"memory_limit {memory}",
               f"output_limit {output}"]
        if pkg.checker == "ncmp":
            cfg.append("use_builtin_checker ncmp")
        elif pkg.checker == "yunagi-c-int32":
            for name in ("chk.cpp", "testlib.h"):
                z.writestr(name, pkg.archive.read(pkg._prefix + name))
        for rule in ("selfeval_checker", "selfeval_compiler", "selfeval_file_io"):
            if rule in pkg.config:
                cfg.append(f"{rule} {pkg.config[rule]}")
        z.writestr("problem.conf", "\n".join(cfg) + "\n")
        for i in range(1, count + 1):
            for name, suffix in zip(pkg._names(i, True), ("in", "ans")):
                data = pkg.archive.read(pkg._prefix + name)
                for prefix in ("", "ex_"):
                    z.writestr(f"{prefix}{i}.{suffix}", data)
    return buf.getvalue()


def build_exam(spec_path, output, passphrase=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if passphrase is not None and len(passphrase) < 12:
        raise ValueError("请使用至少 12 个字符的出题人口令。")
    spec_path, output = Path(spec_path).resolve(), Path(output).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    identifiers(spec["problems"])
    style = contest_style(spec.get("style", "standard"))
    schedule = {}
    if "duration_minutes" in spec:
        schedule["duration_minutes"] = contest_duration(spec["duration_minutes"])
    if "starts_at" in spec:
        schedule["starts_at"] = contest_start(spec["starts_at"]).isoformat()
    if not isinstance(spec["title"], str) or not spec["title"].strip():
        raise ValueError("比赛名称不能为空。")
    def path(s):
        return (spec_path.parent / s).resolve(strict=True)
    public = {}
    def pdf(key, value):
        content = path(value).read_bytes()
        if not content.startswith(b"%PDF-"):
            raise ValueError(f"{value} 不是 PDF 文件。")
        public[key] = content
    pdf("booklet.pdf", spec["pdf"])
    tests = io.BytesIO()
    rows = []
    with ZipFile(tests, "w") as private:
        for row in spec["problems"]:
            key = row["id"]
            pdf(f"pdf/{key}.pdf", row["pdf"])
            try:
                data_path = path(row["data"])
                with Package(data_path) as pkg:
                    sample = public_samples(pkg)
                    if sample:
                        public[f"samples/{key}.zip"] = sample
                    problem = {"id": key, "title": row["title"], "samples": pkg.n_samples,
                               "points": pkg.full_score}
                    if row.get("english_title", "").strip():
                        problem["english_title"] = row["english_title"].strip()
                    if style in ("oi", "ioi"):
                        problem["basename"] = row.get("basename", pkg.file_io or key)
                    rows.append(problem)
                private.write(data_path, key + ".zip")
            except (OSError, ValueError, BadZipFile, KeyError) as exc:
                raise ValueError(f"题目 {key} 的数据包 {row.get('data', '')}：{exc}") from exc
    if style in ("oi", "ioi"):
        check_basenames({p["id"]: p["basename"] for p in rows})
    manifest = json.dumps({"format": "selfeval/1", "edition": uuid4().hex, "title": spec["title"],
                           "style": style, "problems": rows, **schedule,
                           "sha256": {k: digest(v) for k,v in public.items()}},
                          ensure_ascii=False, sort_keys=True).encode()
    salt, nonce = os.urandom(16), os.urandom(12)
    key = os.urandom(32) if passphrase is None else derive(passphrase, salt)
    sealed = salt + nonce + AESGCM(key).encrypt(nonce, tests.getvalue(), manifest)
    stage = output.with_name(output.name + "." + uuid4().hex + ".tmp")
    try:
        write_player_pack(stage, manifest, sealed, key, public.items())
        stage.replace(output)
    finally:
        if stage.exists():
            stage.unlink()


class Exam:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        with ZipFile(self.path) as z:
            if z.getinfo("manifest.json").file_size > 512000:
                raise ValueError("比赛索引过大。")
            if len(z.namelist()) != len(set(z.namelist())):
                raise ValueError("赛包含有重复条目。")
            self.raw = z.read("manifest.json")
            self.manifest = json.loads(self.raw)
            self.local_grading = self.manifest.get("format") == "selfeval/2"
            if self.local_grading:
                from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, InvalidUnwrap
                self.fingerprint = self.manifest.get("fingerprint", "")
                if not isinstance(self.fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", self.fingerprint):
                    raise ValueError("比赛指纹无效。")
                if z.getinfo(LOCAL_ACCESS).file_size != 40 or z.getinfo("materials/index.bin").file_size > 512028:
                    raise ValueError("本地评测凭据或索引大小无效。")
                with z.open("tests.sealed") as sealed:
                    prefix = sealed.read(28)
                try:
                    self._data_key = aes_key_unwrap(wrapping_key(bytes.fromhex(self.fingerprint), prefix),
                                                   z.read(LOCAL_ACCESS))
                except InvalidUnwrap:
                    raise ValueError("赛包凭据校验失败，请重新获取赛包。") from None
                self.raw = self._read_material(z, "index", "materials/index.bin")
                if digest(self.raw) != self.fingerprint:
                    raise ValueError("赛包索引校验失败。")
                self.manifest = json.loads(self.raw)
        if self.manifest.get("format") != "selfeval/1":
            raise ValueError("不支持的比赛包版本。")
        self.title = self.manifest["title"]
        self.problems = self.manifest["problems"]
        self.ids = identifiers(self.problems)
        if any(type(p.get("samples")) is not int or not 0 <= p["samples"] <= 10000 for p in self.problems):
            raise ValueError("公开样例数量须为 0 至 10000 的整数。")
        self.fingerprint = digest(self.raw)
        self.duration_minutes = (contest_duration(self.manifest["duration_minutes"])
                                 if "duration_minutes" in self.manifest
                                 else LEGACY_DURATIONS.get(self.fingerprint))
        self.starts_at = (contest_start(self.manifest["starts_at"]).timestamp()
                          if "starts_at" in self.manifest else None)
        self.basenames = {}
        if "style" in self.manifest:
            self.style = contest_style(self.manifest["style"])
            if self.style in ("oi", "ioi"):
                self.basenames = check_basenames({p["id"]: p.get("basename", p["id"]) for p in self.problems})
        else:
            # Early CSP-J exports carried the contest's filenames only inside
            # the public sample packages. Keep their original fingerprint.
            for pid in self.ids:
                key = f"samples/{pid}.zip"
                if key not in self.manifest["sha256"]:
                    break
                with Package(io.BytesIO(self.public(key))) as sample:
                    if sample.checker != "noi-text" or not sample.file_io:
                        break
                    self.basenames[pid] = sample.file_io
            if len(set(self.basenames.values())) == len(self.ids):
                self.style = "oi"
            else:
                self.style = "standard"
                self.basenames = {}
        self.names = {p["id"]: display_name(p, self.basenames.get(p["id"])) for p in self.problems}

    def problem_label(self, pid):
        name = self.names[pid]
        return name if name in (pid, f"Problem {pid}") else f"{pid}  {name}"

    def public(self, key):
        if key not in self.manifest["sha256"]:
            raise ValueError("比赛中没有这份资料。")
        with ZipFile(self.path) as z:
            content = self._read_material(z, key, material_path(key)) if self.local_grading else z.read(key)
        if digest(content) != self.manifest["sha256"][key]:
            raise ValueError("赛包内文件与索引不一致：" + key)
        return content

    def _read_material(self, archive, name, path):
        from cryptography.exceptions import InvalidTag
        content = archive.read(path)
        try:
            return material_cipher(self._data_key).decrypt(content[:12], content[12:],
                    material_aad(bytes.fromhex(self.fingerprint), name))
        except (ValueError, InvalidTag):
            raise ValueError("题面或样例校验失败，请重新获取赛包。") from None

    def require_started(self, now=None):
        if self.starts_at is not None and (time.time() if now is None else now) < self.starts_at:
            local_start = datetime.fromtimestamp(self.starts_at).astimezone()
            raise ValueError(f"比赛尚未开始，将于本地时间 {local_start:%Y-%m-%d %H:%M:%S %z} 开赛。")

    def export_pdf(self, target, problem_id=None):
        from candidate import require_contest_started
        require_contest_started(self)
        if problem_id is not None and problem_id not in self.ids:
            raise ValueError("没有这个题号。")
        Path(target).write_bytes(self.public(f"pdf/{problem_id}.pdf" if problem_id else "booklet.pdf"))

    @contextmanager
    def test_archive(self, teacher_secret=None):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
        with ZipFile(self.path) as z:
            blob = z.read("tests.sealed")
            if teacher_secret is not None:
                key = derive(teacher_secret, blob[:16])
            else:
                if not self.local_grading:
                    raise ValueError("这是旧版比赛包，请使用教师通过 --studio enable-local 转换后的赛包。")
                key = self._data_key
        try:
            plain = AESGCM(key).decrypt(blob[16:28], blob[28:], self.raw)
        except (InvalidTag, ValueError):
            raise ValueError("无法验证正式数据，请检查赛包及出题人口令。") from None
        del blob
        with ZipFile(io.BytesIO(plain)) as z:
            if sorted(z.namelist()) != sorted(i + ".zip" for i in self.ids):
                raise ValueError("正式数据的题号与比赛索引不一致。")
            yield z

    def unlock(self, secret, dest):
        with self.test_archive(secret) as z:
            for key in self.ids:
                with z.open(key + ".zip") as inp, (Path(dest) / (key + ".zip")).open("xb") as out:
                    shutil.copyfileobj(inp, out)


def enable_local(path, output, passphrase):
    """Add offline access after authenticating with the teacher's credential."""
    exam = Exam(path)
    output = Path(output).resolve()
    with exam.test_archive(passphrase):
        pass
    if output == exam.path and exam.local_grading:
        with exam.test_archive():
            return
    stage = output.with_name(output.name + "." + uuid4().hex + ".tmp")
    try:
        with ZipFile(exam.path) as old:
            sealed = old.read("tests.sealed")
        key = derive(passphrase, sealed[:16])
        write_player_pack(stage, exam.raw, sealed, key,
                          ((name, exam.public(name)) for name in exam.manifest["sha256"]))
        with Exam(stage).test_archive():
            pass
        stage.replace(output)
    finally:
        stage.unlink(missing_ok=True)


def error_result(message):
    return dict(status="ERROR", summary=str(message), diagnostic="", cases=[], score=None, max_score=None)


def student_result(exam, result):
    if exam.style == "oi":
        return result
    status = result["status"]
    if status == "ERROR":
        from judge import environment_summary
        error = environment_summary(result.get("environment_phase"), result.get("environment_reason"))
    if exam.style == "ioi":
        # Checker differences and the contestant's stderr can contain private
        # input/answer text. Retain only the quantities shown on the scoreboard.
        cases = [{key: item[key] for key in ("number", "status", "time_ms", "memory_kb", "extra") if key in item}
                 for item in result.get("cases", ())]
        score, maximum = result.get("score"), result.get("max_score")
        if status == "ERROR":
            summary = error
        elif status == "CANCELLED":
            summary = "评测已停止。"
        elif score is not None and maximum is not None:
            summary = f"{status} · 本次得分 {score:g}/{maximum:g}"
        else:
            summary = status
        return dict(status=status, summary=summary,
                    diagnostic=result.get("diagnostic", "") if status == "CE" else "",
                    cases=cases, score=score, max_score=maximum,
                    score_parts=dict(result.get("score_parts", {})))
    names = {"AC": "通过", "WA": "答案错误", "TLE": "超出时间限制", "MLE": "超出内存限制",
             "OLE": "超出输出限制", "RE": "运行错误", "CE": "编译错误", "CANCELLED": "已停止"}
    if status == "ERROR":
        summary = error
    else:
        summary = f"{status} · {names.get(status, '评测环境异常')}"
    return dict(status=status, summary=summary, diagnostic=result.get("diagnostic", "") if status == "CE" else "",
                cases=[], score=None, max_score=None)


def _official_result(archive, pid, source, cancel=None):
    from judge import evaluate
    with io.BytesIO(archive.read(pid + ".zip")) as data:
        return evaluate(data, source, "full", cancel=cancel, boxed=True, compiler="/usr/bin/g++")


def judge_local_sources(exam, sources, cancel=None):
    if any(pid not in exam.ids for pid in sources):
        raise ValueError("正式评测中出现未知题号。")
    results = {}
    with exam.test_archive() as archive:
        for pid, source in sources.items():
            results[pid] = _official_result(archive, pid, source, cancel)
    return results


def judge_final_oi(exam, sources):
    from candidate import contest_ended
    if exam.style != "oi" or not contest_ended(exam):
        raise ValueError("仅能在 OI 比赛结束后统一进行正式评测。")
    return judge_local_sources(exam, sources)


def judge_exam(exam_path, problem_id, source, mode, *, on_event=None, cancel=None, compiler=None,
               submitted_at=None):
    from candidate import contest_ended, require_contest_started
    from judge import evaluate
    try:
        e = Exam(exam_path)
        if problem_id not in e.ids or mode not in ("samples", "full"):
            raise ValueError("请选择有效的题号和评测模式。")
        if e.style == "oi" and mode == "full":
            raise ValueError("OI 比赛的选手自测仅开放公开样例。")
        if e.style != "oi" and mode == "samples":
            raise ValueError(f"{'IOI' if e.style == 'ioi' else 'ICPC'} 比赛仅开放正式提交。")
        require_contest_started(e)
        if e.style == "oi" and contest_ended(e):
            raise ValueError("OI 比赛已结束，请查看统一正式评测结果。")
        if e.style != "oi" and contest_ended(e, now=submitted_at):
            raise ValueError("比赛已结束，正式提交已关闭。")
        if on_event:
            on_event({"kind": "progress", "message": "正在评测…"})
        if mode == "samples":
            key = f"samples/{problem_id}.zip"
            if key not in e.manifest["sha256"]:
                raise ValueError("本题没有公开样例。")
            with tempfile.TemporaryDirectory(prefix="selfeval-samples-") as d:
                pkg = Path(d) / "samples.zip"
                pkg.write_bytes(e.public(key))
                return student_result(e, evaluate(pkg, source, mode, cancel=cancel, compiler=compiler))
        if not 0 < Path(source).stat().st_size <= 1024 * 1024:
            raise ValueError("请提交 1 MiB 以内的非空单文件 C++ 源码。")
        with e.test_archive() as archive:
            result = student_result(e, _official_result(archive, problem_id, source, cancel))
        if e.style == "ioi" and result["status"] not in ("ERROR", "CANCELLED"):
            from final_round import record_ioi
            record_ioi(e, problem_id, result)
        return result
    except (ValueError, OSError, BadZipFile, RuntimeError, KeyError, ImportError) as exc:
        return error_result(exc)
