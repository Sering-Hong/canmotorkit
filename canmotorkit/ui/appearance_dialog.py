"""外观设置对话框：主题、背景图、透明度、强调色。改动实时预览并立即记忆。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from .theme import DEFAULT_ACCENT, Appearance, ThemeVariant
from .widgets import CheckBox

ACCENT_PRESETS = (("樱花粉", "#e0709a"), ("初音青", "#39c5bb"), ("天空蓝", "#4a90d9"),
                  ("薰衣紫", "#9575cd"), ("琥珀橙", "#f0a030"))
MODES = (("light", "浅色（樱花）"), ("dark", "深色"), ("system", "跟随系统"))


class _Slider(QWidget):
    valueChanged = Signal(int)

    def __init__(self, lo: int, hi: int, suffix: str = " %"):
        super().__init__()
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(lo, hi)
        self.lbl = QLabel()
        self.lbl.setMinimumWidth(44)
        self.lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.suffix = suffix
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.s, 1)
        h.addWidget(self.lbl)
        self.s.valueChanged.connect(self._changed)

    def _changed(self, v):
        self.lbl.setText(f"{v}{self.suffix}")
        self.valueChanged.emit(v)

    def value(self) -> int:
        return self.s.value()

    def setValue(self, v: int):
        self.s.blockSignals(True)
        self.s.setValue(int(v))
        self.s.blockSignals(False)
        self.lbl.setText(f"{self.s.value()}{self.suffix}")


class AppearanceDialog(QDialog):
    """appearance_changed(Appearance) 在每次修改后发出；关闭时由主窗口保存。"""
    appearance_changed = Signal(object)

    def __init__(self, appearance: Appearance, is_dark_fn, parent=None):
        super().__init__(parent)
        self.setWindowTitle("外观设置")
        self.setModal(False)
        self.setMinimumWidth(460)
        self._orig = appearance.copy()
        self.a = appearance.copy()
        self._is_dark = is_dark_fn

        self.cb_mode = QComboBox()
        for key, text in MODES:
            self.cb_mode.addItem(text, key)

        self.chk_bg = CheckBox("显示背景图")
        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("未选择图片")
        self.ed_path.setReadOnly(True)
        self.btn_browse = QPushButton("选择图片…")
        self.btn_clear_img = QPushButton("清除")
        self.lbl_img = QLabel()
        self.lbl_img.setProperty("themeRole", "muted")
        self.lbl_img.setWordWrap(True)

        self.sl_bg = _Slider(0, 100)
        self.sl_bg.setToolTip("背景图本身的不透明度；越低越接近底色")
        self.sl_overlay = _Slider(0, 90)
        self.sl_overlay.setToolTip("深色模式下为压暗、浅色模式下为提亮，保证文字可读")
        self.sl_panel = _Slider(15, 100)
        self.sl_panel.setToolTip("分组框、输入框、曲线区的不透明度；越低越透出背景")

        self.btn_accent = QPushButton()
        self.btn_accent.setMinimumWidth(110)
        presets = QHBoxLayout()
        presets.setContentsMargins(0, 0, 0, 0)
        presets.addWidget(self.btn_accent)
        for name, color in ACCENT_PRESETS:
            b = QPushButton()
            b.setToolTip(name)
            b.setFixedSize(22, 22)
            b.setStyleSheet(f"QPushButton{{background:{color};border:1px solid rgba(0,0,0,60);"
                            f"border-radius:11px;padding:0}}")
            b.clicked.connect(lambda _=False, c=color: self._set_accent(c))
            presets.addWidget(b)
        presets.addStretch(1)

        g_theme = QGroupBox("主题")
        f1 = QFormLayout(g_theme)
        f1.addRow("模式", self.cb_mode)

        self.g_look = QGroupBox()
        f2 = QFormLayout(self.g_look)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.ed_path, 1)
        path_row.addWidget(self.btn_browse)
        path_row.addWidget(self.btn_clear_img)
        f2.addRow(self.chk_bg)
        f2.addRow("图片", path_row)
        f2.addRow("", self.lbl_img)
        f2.addRow("背景不透明度", self.sl_bg)
        f2.addRow("遮罩强度", self.sl_overlay)
        f2.addRow("面板不透明度", self.sl_panel)
        f2.addRow("强调色", presets)
        hint = QLabel("背景图路径两种模式共用；不透明度、遮罩和强调色按「深色 / 浅色」分别记忆。")
        hint.setProperty("themeRole", "muted")
        hint.setWordWrap(True)

        self.btn_defaults = QPushButton("恢复默认")
        self.btn_revert = QPushButton("撤销本次修改")
        self.btn_ok = QPushButton("完成")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_defaults)
        bottom.addWidget(self.btn_revert)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_ok)

        lay = QVBoxLayout(self)
        lay.addWidget(g_theme)
        lay.addWidget(self.g_look)
        lay.addWidget(hint)
        lay.addLayout(bottom)

        self.cb_mode.currentIndexChanged.connect(self._mode_changed)
        self.chk_bg.toggled.connect(lambda on: self._set_variant(bg_enabled=on))
        self.sl_bg.valueChanged.connect(lambda v: self._set_variant(bg_opacity=v))
        self.sl_overlay.valueChanged.connect(lambda v: self._set_variant(overlay=v))
        self.sl_panel.valueChanged.connect(lambda v: self._set_variant(panel_opacity=v))
        self.btn_accent.clicked.connect(self._pick_accent)
        self.btn_browse.clicked.connect(self._browse)
        self.btn_clear_img.clicked.connect(lambda: self._set_path(""))
        self.btn_defaults.clicked.connect(self._defaults)
        self.btn_revert.clicked.connect(self._revert)
        self.btn_ok.clicked.connect(self.accept)
        self._load_widgets()

    # ---- 当前编辑的模式 ----
    def _key(self) -> str:
        mode = self.a.mode
        dark = mode == "dark" or (mode == "system" and self._is_dark())
        return "dark" if dark else "light"

    def _variant(self) -> ThemeVariant:
        return self.a.variants[self._key()]

    def _load_widgets(self):
        v = self._variant()
        self.cb_mode.blockSignals(True)
        self.cb_mode.setCurrentIndex(max(0, self.cb_mode.findData(self.a.mode)))
        self.cb_mode.blockSignals(False)
        self.g_look.setTitle(f"{'深色' if self._key() == 'dark' else '浅色'}模式外观")
        self.chk_bg.blockSignals(True)
        self.chk_bg.setChecked(v.bg_enabled)
        self.chk_bg.blockSignals(False)
        self.ed_path.setText(self.a.bg_path)
        self.ed_path.setToolTip(self.a.bg_path)
        self.sl_bg.setValue(v.bg_opacity)
        self.sl_overlay.setValue(v.overlay)
        self.sl_panel.setValue(v.panel_opacity)
        self._show_accent(v.accent)
        self._update_img_label()

    def _update_img_label(self, error: str = ""):
        p = self.a.bg_path
        if not p:
            text = "未设置背景图"
        elif not os.path.isfile(p):
            text = "⚠ 文件不存在：请重新选择"
        elif error:
            text = "⚠ " + error
        else:
            text = os.path.basename(p)
        self.lbl_img.setText(text)
        on = self.chk_bg.isChecked() and bool(p)
        for w in (self.sl_bg, self.sl_overlay):
            w.setEnabled(on)

    def sync_from(self, appearance: Appearance):
        """主窗口菜单切换主题后同步对话框显示。"""
        self.a = appearance.copy()
        self._load_widgets()

    def set_image_error(self, error: str):
        self._update_img_label(error)

    def _show_accent(self, color: str):
        c = QColor(color)
        fg = "#ffffff" if c.lightness() < 170 else "#2a1a22"
        self.btn_accent.setText(color.upper())
        self.btn_accent.setStyleSheet(f"QPushButton{{background:{color};color:{fg};"
                                      f"border:1px solid rgba(0,0,0,60);border-radius:6px}}")

    # ---- 修改 ----
    def _emit(self):
        self._update_img_label()
        self.appearance_changed.emit(self.a.copy())

    def _set_variant(self, **kw):
        v = self._variant()
        for k, val in kw.items():
            setattr(v, k, val)
        self._emit()

    def _set_accent(self, color: str):
        self._show_accent(color)
        self._set_variant(accent=color)

    def _pick_accent(self):
        c = QColorDialog.getColor(QColor(self._variant().accent), self, "选择强调色")
        if c.isValid():
            self._set_accent(c.name())

    def _set_path(self, path: str):
        self.a.bg_path = path
        self.ed_path.setText(path)
        self.ed_path.setToolTip(path)
        if path and not self._variant().bg_enabled:
            self.chk_bg.setChecked(True)      # 会触发 _set_variant
            return
        self._emit()

    def _browse(self):
        start = os.path.dirname(self.a.bg_path) if self.a.bg_path else os.path.expanduser("~/Pictures")
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图", start,
                                              "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*)")
        if path:
            self._set_path(os.path.normpath(path))

    def _mode_changed(self):
        self.a.mode = self.cb_mode.currentData()
        self._load_widgets()
        self._emit()

    def _defaults(self):
        d = Appearance()
        d.mode = self.a.mode
        d.bg_path = self.a.bg_path or d.bg_path
        self.a = d
        self._load_widgets()
        self._emit()

    def _revert(self):
        self.a = self._orig.copy()
        self._load_widgets()
        self._emit()

    @staticmethod
    def default_accent() -> str:
        return DEFAULT_ACCENT
