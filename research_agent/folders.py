"""Native folder chooser in its own UI process; only explicitly called by the local user."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .workbench_store import Conflict, text_field

_PICKER_LOCK = threading.Lock()


def choose_folder(initial=""):
    if os.name != "nt":
        raise ValueError("系统目录选择仅支持本机 Windows；远程或容器部署请填写服务器目录")
    initial = text_field(initial, "起始目录", 2000, empty=True)
    if initial:
        path = Path(initial)
        if not path.is_absolute() or ".." in path.parts or initial.startswith(("\\\\", "//")):
            raise ValueError("起始目录必须是本机绝对路径")
        initial = str(path) if path.is_dir() else ""
    if not _PICKER_LOCK.acquire(blocking=False):
        raise Conflict("目录选择窗口已经打开，请先完成选择")
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "research_agent.folders"],
            input=json.dumps({"initial": initial}), text=True, encoding="utf-8", capture_output=True,
            timeout=180, cwd=Path(__file__).resolve().parents[1], creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise ValueError("无法打开系统目录选择窗口，请确认服务运行于本机桌面，或手动输入路径")
        selected = json.loads(result.stdout)["path"]
        if not selected:
            return {"path": None, "cancelled": True}
        path = Path(selected)
        if not path.is_absolute() or selected.startswith(("\\\\", "//")) or not path.is_dir():
            raise ValueError("请选择本机已有的文件夹")
        return {"path": str(path.resolve()), "cancelled": False}
    except subprocess.TimeoutExpired as exc:
        raise ValueError("目录选择超时，请重新打开") from exc
    finally:
        _PICKER_LOCK.release()


if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog

    request = json.load(sys.stdin)
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(parent=root, title="选择研究资料保存文件夹", initialdir=request.get("initial") or str(Path.home()), mustexist=True)
        print(json.dumps({"path": selected or None}, ensure_ascii=False))
    finally:
        root.destroy()
