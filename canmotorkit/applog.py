"""运行日志与未捕获异常处理。

- 日志写入 %LOCALAPPDATA%/CanMotorKit/logs/canmotorkit.log（Linux 为 ~/.local/share），按大小轮转。
- 未捕获异常写日志并弹窗提示，不让程序静默崩溃；后台线程异常同样记录。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path

log = logging.getLogger("canmotorkit")


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "CanMotorKit"


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging(level: int = logging.INFO) -> Path:
    path = log_dir() / "canmotorkit.log"
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
        h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
        log.addHandler(h)
    log.setLevel(level)
    return path


def _show_error(text: str):
    try:
        from PySide6.QtCore import QThread, QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return
    app = QApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        return

    def show():
        box = QMessageBox(QMessageBox.Critical, "程序错误",
                          "发生未处理的错误，已写入日志。\n若电机正在输出，请先按急停（Esc / 空格）确认安全。",
                          QMessageBox.Ok, app.activeWindow())
        box.setDetailedText(text)
        box.exec()
    QTimer.singleShot(0, show)


def install_excepthook():
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("未捕获异常\n%s", text)
        sys.__stderr__ and sys.__stderr__.write(text)
        _show_error(text)

    def thread_hook(args):
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log.error("线程 %s 未捕获异常\n%s", getattr(args.thread, "name", "?"), text)

    sys.excepthook = hook
    threading.excepthook = thread_hook
