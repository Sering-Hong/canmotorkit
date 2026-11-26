"""使用说明窗口：渲染项目根目录的「使用说明.md」（打包后随 exe 一起分发）。"""
from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout

GUIDE_NAME = "使用说明.md"


def guide_path() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, GUIDE_NAME)


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使用说明")
        self.resize(860, 760)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        try:
            with open(guide_path(), encoding="utf-8") as f:
                view.setMarkdown(f.read())
        except OSError as e:
            view.setPlainText(f"找不到使用说明文件：{guide_path()}\n{e}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(view)
