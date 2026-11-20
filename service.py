"""Local Unix-socket judge, launched by the organizer under a separate account."""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import time
from zipfile import BadZipFile

from exam import DEFAULT_SOCKET, Exam, error_result, personal_socket, student_result
from judge import evaluate


def formal_exam(exam_path):
    exam = Exam(exam_path)
    if exam.style == "oi":
        raise ValueError("OI 比赛的选手自测仅开放公开样例。")
    return exam


def serve(exam_path, endpoint, password, ready=None):
    exam = formal_exam(exam_path)
    if os.geteuid() == 0:
        raise ValueError("请用专用的普通评测账户运行服务。")
    if not shutil.which("bwrap") or not shutil.which("prlimit") or not shutil.which("g++"):
        raise ValueError("正式评测服务需要 bubblewrap、util-linux 和 g++。")
    endpoint = Path(endpoint).absolute()
    folder = endpoint.parent
    folder.mkdir(mode=0o700 if endpoint == personal_socket() else 0o755, parents=True, exist_ok=True)
    st = folder.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o022 or folder.is_symlink():
        raise ValueError("套接字所在目录应由评测账户拥有，其他账户不可写。")
    with tempfile.TemporaryDirectory(prefix="selfeval-private-") as root:
        root = Path(root)
        exam.unlock(password, root)
        del password
        class Handler(socketserver.StreamRequestHandler):
            def emit(self, obj):
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")

            def handle(self):
                try:
                    self.connection.settimeout(15)
                    line = self.rfile.readline(1500000)
                    if not line.endswith(b"\n"):
                        raise ValueError("提交过大或不完整。")
                    req = json.loads(line)
                    if req == {"control": "stop"}:
                        peer = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                          struct.calcsize("3i"))
                        if struct.unpack("3i", peer)[1] != os.geteuid():
                            self.emit({"stopping": False, "message": "请用启动评测服务的账户停止服务。"})
                            return
                        self.server.stopping = True
                        self.emit({"stopping": True})
                        return
                    if not isinstance(req, dict) or not isinstance(req.get("code"), str):
                        raise ValueError("提交格式不正确。")
                    if len(req["code"]) > 1398104:
                        raise ValueError("源码超过 1 MiB。")
                    if req.get("exam") != exam.fingerprint or req.get("problem") not in exam.ids:
                        raise ValueError("服务运行的是另一场比赛或题号不存在。")
                    exam.require_started()
                    code = base64.b64decode(req["code"], validate=True)
                    if not 0 < len(code) <= 1024 * 1024:
                        raise ValueError("请提交 1 MiB 以内的单文件 C++ 源码。")
                    self.connection.settimeout(30)
                    self.emit({"kind": "progress", "message": "正在评测…"})
                    with tempfile.TemporaryDirectory(dir=root) as d:
                        source = Path(d) / "main.cpp"
                        source.write_bytes(code)
                        result = evaluate(root / (req["problem"] + ".zip"), source, "full",
                                          boxed=True, compiler="/usr/bin/g++")
                    if result["status"] == "ERROR":
                        print(result["summary"], flush=True)
                        result = error_result("服务端评测环境异常，请联系组题人。")
                except (BrokenPipeError, ConnectionResetError):
                    return
                except ValueError as exc:
                    result = error_result(str(exc))
                except (KeyError, OSError) as exc:
                    print(f"评测服务错误：{exc}", flush=True)
                    result = error_result("服务端处理提交失败，请联系组题人。")
                self.emit({"kind": "done", "result": student_result(exam, result)})

        # Never replace another process's socket. Remove only the socket we bound.
        if endpoint.exists() or endpoint.is_symlink():
            raise ValueError("套接字已存在，请检查是否已有评测服务。")
        with socketserver.UnixStreamServer(str(endpoint), Handler) as server:
            inode = endpoint.stat().st_ino
            try:
                endpoint.chmod(0o666)
                message = f"{exam.title} · 正式评测就绪\n{endpoint}"
                if ready is None:
                    print(message, flush=True)
                else:
                    ready(message)
                server.stopping = False
                while not server.stopping:
                    server.handle_request()
            finally:
                if endpoint.exists() and stat.S_ISSOCK(endpoint.lstat().st_mode) and endpoint.stat().st_ino == inode:
                    endpoint.unlink()


def start_background(exam_path, endpoint, password):
    """Wait until the detached worker has decrypted and bound the socket."""
    parent, child = socket.socketpair()
    with parent, child:
        args = [sys.executable, str(Path(__file__).resolve()), str(Path(exam_path).resolve()),
                "--socket", str(Path(endpoint).absolute()), "--ready-fd", str(child.fileno())]
        worker_process = subprocess.Popen(args, start_new_session=True, cwd="/",
                                          pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.close()
        try:
            parent.sendall(password.encode("utf-8"))
            parent.shutdown(socket.SHUT_WR)
            del password
            with parent.makefile("rb") as response:
                reply = response.readline(65536)
            if not reply:
                raise ValueError("后台服务在启动时退出，请以前台方式查看错误。")
            status = json.loads(reply)
            if not status["ok"]:
                raise ValueError(status["message"])
            return status["message"]
        except BaseException:
            if worker_process.poll() is None:
                worker_process.terminate()
            worker_process.wait()
            raise


def worker(exam_path, endpoint, channel_fd):
    def terminate(_signum, _frame):
        raise SystemExit(1)  # unwind the socket and the private temporary directory

    signal.signal(signal.SIGTERM, terminate)
    with socket.socket(fileno=channel_fd) as channel:
        def notify(ok, message):
            channel.sendall(json.dumps({"ok": ok, "message": message}).encode() + b"\n")
            channel.close()

        def receive_password():
            with channel.makefile("rb") as stream:
                return stream.read().decode("utf-8")

        try:
            serve(exam_path, endpoint, receive_password(), lambda message: notify(True, message))
        except Exception as exc:
            if channel.fileno() != -1:
                notify(False, str(exc))
            raise


def stop_service(endpoint):
    path = Path(endpoint)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            inode = path.lstat().st_ino
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            raise ValueError("没有正在此套接字上运行的评测服务。") from None
        client.sendall(b'{"control":"stop"}\n')
        with client.makefile("rb") as answer:
            line = answer.readline(65536)
        if not line:
            raise ValueError("评测服务断开了连接。")
        result = json.loads(line)
        if not result.get("stopping"):
            raise ValueError(result.get("message", "停止评测服务失败。"))
    # Wait for the old socket to be removed so an immediate restart succeeds.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if path.lstat().st_ino != inode:
                break
        except FileNotFoundError:
            break
        time.sleep(0.02)
    else:
        raise ValueError("服务已收到停止请求，套接字仍在关闭。")
    return "正式评测服务已停止。"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("exam", nargs="?")
    p.add_argument("--socket", default=os.environ.get("SELFEVAL_SOCKET", DEFAULT_SOCKET))
    p.add_argument("--personal", action="store_true", help="在个人目录创建服务，无需 /run/selfeval")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true", help="启动成功后返回，服务继续在后台运行")
    mode.add_argument("--stop", action="store_true", help="用启动服务的账户停止服务")
    mode.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    a = p.parse_args()
    try:
        endpoint = personal_socket() if a.personal else a.socket
        if a.stop:
            print(stop_service(endpoint))
        elif not a.exam:
            p.error("请指定 .seexam 比赛文件")
        elif a.ready_fd is not None:
            worker(a.exam, endpoint, a.ready_fd)
        else:
            formal_exam(a.exam)
            if a.background:
                print(start_background(a.exam, endpoint, getpass.getpass("出题人口令：")))
            else:
                serve(a.exam, endpoint, getpass.getpass("出题人口令："))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, ImportError, BadZipFile, KeyError) as exc:
        p.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
