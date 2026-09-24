# -*- coding: utf-8 -*-
"""稳定启动合约交易助手：复用已运行服务，等待就绪后自动打开浏览器。"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
START_PORT = 8501
MAX_PORT = 8510
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
NO_BROWSER = os.environ.get('CRYPTO_AGENT_NO_BROWSER') == '1'


def health_ok(port: int) -> bool:
    """只信任 Streamlit 自己的健康检查，不把纯 200 当成服务已就绪。"""
    try:
        req = urllib.request.Request(f"http://{HOST}:{port}/_stcore/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=1.2) as r:
            return r.status == 200 and r.read(20).decode("utf-8", "ignore").strip().lower() == "ok"
    except Exception:
        return False


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def choose_or_find() -> tuple[int | None, bool]:
    """返回 (端口, 是否已有可复用服务)。"""
    for port in range(START_PORT, MAX_PORT + 1):
        if health_ok(port):
            return port, True
        if port_available(port):
            return port, False
    return None, False


def tail(path: Path, n=18) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "（暂时没有日志）"


def main() -> int:
    port, running = choose_or_find()
    if port is None:
        print("启动失败：8501-8510 端口都被占用了。")
        return 1

    url = f"http://localhost:{port}"
    if running:
        print(f"服务已经在运行，正在打开：{url}")
        if not NO_BROWSER:
            webbrowser.open_new_tab(url)
        return 0

    out_log = DATA_DIR / f"streamlit-{port}.out.log"
    err_log = DATA_DIR / f"streamlit-{port}.err.log"
    out_f = out_log.open("w", encoding="utf-8", errors="replace")
    err_f = err_log.open("w", encoding="utf-8", errors="replace")

    cmd = [
        sys.executable, "-m", "streamlit", "run", "app.py",
        f"--server.port={port}",
        f"--server.address={HOST}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--server.fileWatcherType=poll",
    ]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=out_f,
        stderr=err_f,
        creationflags=flags,
        close_fds=True,
    )
    out_f.close()
    err_f.close()

    print(f"正在启动服务（端口 {port}）…")
    deadline = time.time() + 60
    while time.time() < deadline:
        if health_ok(port):
            print(f"启动成功，正在打开：{url}")
            if not NO_BROWSER:
                webbrowser.open_new_tab(url)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.8)

    print("启动失败。最近错误日志如下：")
    print(tail(err_log))
    print(f"完整日志：{err_log}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
