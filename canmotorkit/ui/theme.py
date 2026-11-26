"""界面主题：深色 / 浅色（樱花）调色板、背景图与透明度，统一生成样式表并持久化。

设计要点：
- 所有颜色集中在 Palette 里，样式表由模板生成，不再在各面板里写死十六进制颜色。
- 背景图画在主窗口的中央控件上（等比裁切铺满），容器控件透明，分组框/输入框/曲线区
  使用带透明度的面板色，透明度可调。
- 背景图路径、开关、背景不透明度、面板不透明度、强调色按「深色 / 浅色」分别记忆在 QSettings。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, replace
from string import Template

from PySide6.QtCore import QObject, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

ORG = APP = "CanMotorKit"
DEFAULT_ACCENT = "#e0709a"          # 樱花粉
DEFAULT_BG = os.path.join(os.path.expanduser("~"), "Pictures",
                          "Wallpaper Alchemy - 樱花初音未来粉色动漫壁纸 4K.jpg")


def settings() -> QSettings:
    return QSettings(ORG, APP)


# ============================ 调色板 ============================
@dataclass(frozen=True)
class Palette:
    dark: bool
    window: str          # 无背景图时的窗口底色
    panel: str           # 分组框/卡片底色（叠加透明度）
    pane: str            # 标签页内容区、曲线区底色
    field: str           # 输入框底色
    button: str
    button_hover: str
    button_pressed: str
    header: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_note: str
    text_disabled: str
    disabled_bg: str
    overlay: str         # 背景图上方的整体遮罩（压暗/提亮，保证文字可读）
    grid: str            # 曲线网格
    ok: str
    ok_bg: str
    danger: str
    danger_text: str
    warn_bg: str         # 已修改未应用的单元格底色
    info_bg: str
    info_text: str
    plot_blue: str
    plot_red: str


DARK = Palette(
    dark=True, window="#17141a", panel="#221d24", pane="#1c181e", field="#151217",
    button="#3a3139", button_hover="#4a3d48", button_pressed="#2c252b", header="#302830",
    border="#4b3f49", border_strong="#7a6574", text="#f4eef2", text_muted="#b7a9b2",
    text_note="#e7bf7a", text_disabled="#7c7178", disabled_bg="#2a2429", overlay="#120d12",
    grid="#8a7a85", ok="#66bb6a", ok_bg="#2e7d32", danger="#ef5350", danger_text="#ffffff",
    warn_bg="#6b5517", info_bg="#3b5c80", info_text="#eaf3ff",
    plot_blue="#64b5f6", plot_red="#f06292",
)

LIGHT = Palette(
    dark=False, window="#fbf4f7", panel="#ffffff", pane="#ffffff", field="#ffffff",
    button="#f6e7ee", button_hover="#fbd9e6", button_pressed="#efc6d6", header="#f7e4ec",
    border="#e8c9d6", border_strong="#c995ab", text="#3a2a33", text_muted="#7d6672",
    text_note="#8a5a00", text_disabled="#a8979f", disabled_bg="#f3eef0", overlay="#ffffff",
    grid="#c7a9b6", ok="#2e7d32", ok_bg="#2e7d32", danger="#c62828", danger_text="#ffffff",
    warn_bg="#fff3c4", info_bg="#d6e8fb", info_text="#163b5f",
    plot_blue="#1565c0", plot_red="#d81b60",
)


def rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    a = max(0, min(255, round(alpha * 255)))
    return f"rgba({c.red()},{c.green()},{c.blue()},{a})"


def mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


# ============================ 外观设置 ============================
@dataclass
class ThemeVariant:
    """某个明暗模式下的外观参数（分别记忆）。"""
    bg_enabled: bool = True
    bg_opacity: int = 100        # 背景图不透明度 %（越低越接近底色/遮罩）
    panel_opacity: int = 78      # 面板不透明度 %（越低越透出背景）
    overlay: int = 15            # 遮罩强度 %（深色压暗 / 浅色提亮）
    accent: str = DEFAULT_ACCENT


@dataclass
class Appearance:
    mode: str = "light"                      # dark / light / system
    bg_path: str = DEFAULT_BG
    variants: dict = field(default_factory=lambda: {
        "dark": ThemeVariant(bg_opacity=100, panel_opacity=78, overlay=55),
        "light": ThemeVariant(bg_opacity=100, panel_opacity=62, overlay=0),
    })

    def variant(self, dark: bool) -> ThemeVariant:
        return self.variants["dark" if dark else "light"]

    # ---- 持久化 ----
    @classmethod
    def load(cls, s: QSettings | None = None) -> "Appearance":
        s = s or settings()
        a = cls()
        a.mode = str(s.value("appearance/mode", s.value("theme_mode", a.mode)))
        if a.mode not in ("dark", "light", "system"):
            a.mode = "light"
        a.bg_path = str(s.value("appearance/bg_path", a.bg_path) or "")
        for key, v in a.variants.items():
            kw = {}
            for f in fields(ThemeVariant):
                raw = s.value(f"appearance/{key}/{f.name}")
                if raw is None:
                    continue
                try:
                    if f.type in (bool, "bool"):
                        kw[f.name] = raw in (True, "true", "1", 1)
                    elif f.type in (int, "int"):
                        kw[f.name] = max(0, min(100, int(raw)))
                    else:
                        kw[f.name] = str(raw) if QColor(str(raw)).isValid() else getattr(v, f.name)
                except (TypeError, ValueError):
                    pass
            a.variants[key] = replace(v, **kw)
        return a

    def save(self, s: QSettings | None = None):
        s = s or settings()
        s.setValue("appearance/mode", self.mode)
        s.setValue("appearance/bg_path", self.bg_path)
        for key, v in self.variants.items():
            for name, value in asdict(v).items():
                s.setValue(f"appearance/{key}/{name}", value)
        s.sync()

    def copy(self) -> "Appearance":
        return Appearance(self.mode, self.bg_path,
                          {k: replace(v) for k, v in self.variants.items()})


# ============================ 样式表 ============================
_QSS = Template("""
QMainWindow, QDialog { background: $window; color: $text; }
QWidget { color: $text; }
QWidget#leftColumn, QWidget#rightColumn, QScrollArea, QScrollArea > QWidget > QWidget,
QSplitter, QSplitter > QWidget, QStackedWidget, QStackedWidget > QWidget,
QWidget#controlPage, QTabWidget#mainTabs QStackedWidget > QWidget {
    background: transparent;
}
QScrollArea { border: none; }
QGraphicsView { background: transparent; border: none; }
QLabel { background: transparent; border: none; }
QSplitter::handle { background: transparent; }
QSplitter::handle:hover { background: $accent_soft; }
QGroupBox {
    background: $panel;
    border: 1px solid $border;
    border-radius: 8px;
    margin-top: 10px;
    padding: 10px 6px 6px 6px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: $accent_text_strong;
    background: transparent;
}
QGroupBox QWidget { font-weight: normal; }
QFrame[frameShape="6"], QFrame#card {
    background: $panel;
    border: 1px solid $border;
    border-radius: 8px;
}
QTabWidget::pane {
    background: $pane;
    border: 1px solid $border;
    border-radius: 8px;
    top: -1px;
}
QTabBar { background: transparent; }
QTabBar::tab {
    background: $tab;
    color: $text_muted;
    border: 1px solid $border;
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 6px 14px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: $pane_solidish;
    color: $text;
    border-top: 2px solid $accent;
    font-weight: 600;
}
QTabBar::tab:hover:!selected { background: $button_hover; color: $text; }
QTabBar::tab:disabled { color: $text_disabled; }
QPushButton, QToolButton {
    background: $button;
    color: $text;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 5px 11px;
}
QPushButton:hover, QToolButton:hover { background: $button_hover; border-color: $accent; }
QPushButton:pressed, QToolButton:pressed { background: $button_pressed; }
QPushButton:checked { background: $accent; color: $accent_on; border-color: $accent; }
QPushButton:disabled, QToolButton:disabled {
    background: $disabled_bg; color: $text_disabled; border-color: $border;
}
QPushButton#primary { background: $accent; color: $accent_on; border-color: $accent; font-weight: 600; }
QPushButton#primary:hover { background: $accent_hover; }
QPushButton#primary:disabled { background: $disabled_bg; color: $text_disabled; border-color: $border; }
QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox, QTableWidget, QTableView,
QListWidget, QTreeView {
    background: $field;
    color: $text;
    border: 1px solid $border;
    border-radius: 5px;
    padding: 2px 4px;
    selection-background-color: $accent;
    selection-color: $accent_on;
}
QTableWidget, QTableView { padding: 0; gridline-color: $border; alternate-background-color: $field_alt; }
QLineEdit:focus, QPlainTextEdit:focus, QAbstractSpinBox:focus, QComboBox:focus { border-color: $accent; }
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {
    background: $disabled_bg; color: $text_disabled;
}
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background: $menu;
    color: $text;
    border: 1px solid $border;
    selection-background-color: $accent;
    selection-color: $accent_on;
}
QTableWidget::item:selected, QTableView::item:selected { background: $accent_soft; color: $text; }
QHeaderView { background: transparent; }
QHeaderView::section {
    background: $header;
    color: $text;
    border: none;
    border-right: 1px solid $border;
    border-bottom: 1px solid $border;
    padding: 4px 6px;
}
QTableCornerButton::section { background: $header; border: none; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    width: 0px; height: 0px; border: none; background: transparent;
}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow { width: 0px; height: 0px; }
QProgressBar {
    background: $field;
    color: $text;
    border: 1px solid $border;
    border-radius: 5px;
    text-align: center;
}
QProgressBar::chunk { background: $accent_soft; border-radius: 4px; }
QSlider::groove:horizontal { height: 4px; background: $border; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $accent; border-radius: 2px; }
QSlider::handle:horizontal {
    background: $accent; border: 2px solid $menu; width: 12px; height: 12px;
    margin: -6px 0; border-radius: 8px;
}
QMenu {
    background: $menu;
    color: $text;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item { padding: 5px 28px 5px 10px; border-radius: 4px; }
QMenu::item:selected { background: $accent; color: $accent_on; }
QMenu::item:disabled { color: $text_disabled; }
QMenu::separator { height: 1px; background: $border; margin: 4px 6px; }
QToolTip {
    background: $menu;
    color: $text;
    border: 1px solid $accent;
    padding: 4px;
}
QStatusBar { background: $statusbar; color: $text; }
QStatusBar QLabel { padding: 0 6px; }
QLabel[themeRole="muted"] { color: $text_muted; }
QLabel[themeRole="note"] { color: $text_note; }
QCheckBox, QRadioButton { color: $text; background: transparent; spacing: 6px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    background: $field;
    border: 1px solid $border_strong;
    border-radius: 3px;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background: $accent_soft; border-color: $accent; }
QCheckBox::indicator:disabled { background: $disabled_bg; border-color: $border; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: $scroll; border-radius: 4px; min-height: 24px; min-width: 24px;
}
QScrollBar::handle:hover { background: $accent; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
""")


@dataclass(frozen=True)
class Theme:
    """一次主题求值的结果：调色板 + 透明度 + 强调色。供各面板查询颜色。"""
    pal: Palette
    accent: str
    panel_alpha: float
    has_image: bool

    @property
    def dark(self) -> bool:
        return self.pal.dark

    @property
    def accent_on(self) -> str:
        return "#ffffff" if QColor(self.accent).lightness() < 170 else "#2a1a22"

    def plot_bg(self) -> QColor:
        c = QColor(self.pal.pane)
        # 曲线区比面板再透一些，让背景图透出；仍保证网格与曲线可读。
        c.setAlphaF(self._a(-0.15) if self.has_image else 1.0)
        return c

    def _a(self, extra: float) -> float:
        return max(0.0, min(1.0, self.panel_alpha + extra))

    def stylesheet(self) -> str:
        p = self.pal
        a = self._a
        img = self.has_image
        acc = self.accent
        soft = rgba(acc, 0.30 if p.dark else 0.22)
        values = dict(
            window=p.window if not img else "transparent",
            panel=rgba(p.panel, a(0.0)) if img else p.panel,
            pane=rgba(p.pane, a(-0.2)) if img else p.pane,
            pane_solidish=rgba(p.pane, a(0.1)) if img else p.pane,
            tab=rgba(p.header, a(-0.1)) if img else p.header,
            field=rgba(p.field, a(0.12)) if img else p.field,
            field_alt=rgba(mix(p.field, acc, 0.06), a(0.12)) if img else mix(p.field, acc, 0.06),
            menu=p.panel,
            button=rgba(p.button, a(0.1)) if img else p.button,
            button_hover=p.button_hover, button_pressed=p.button_pressed,
            header=rgba(p.header, a(0.1)) if img else p.header,
            statusbar=rgba(p.panel, a(0.0)) if img else p.panel,
            border=rgba(p.border, 0.9), border_strong=p.border_strong,
            text=p.text, text_muted=p.text_muted, text_note=p.text_note,
            text_disabled=p.text_disabled,
            disabled_bg=rgba(p.disabled_bg, a(0.05)) if img else p.disabled_bg,
            scroll=rgba(p.border_strong, 0.7),
            accent=acc, accent_soft=soft, accent_on=self.accent_on,
            accent_hover=mix(acc, "#ffffff" if p.dark else "#000000", 0.12),
            accent_text_strong=mix(acc, p.text, 0.15 if p.dark else 0.35),
        )
        return _QSS.substitute(values)


def build_theme(app: Appearance, dark: bool, has_image: bool) -> Theme:
    v = app.variant(dark)
    return Theme(DARK if dark else LIGHT, v.accent if QColor(v.accent).isValid() else DEFAULT_ACCENT,
                 v.panel_opacity / 100.0, has_image)


_current = Theme(DARK, DEFAULT_ACCENT, 1.0, False)


def current() -> Theme:
    """当前主题（主窗口应用主题时更新），供绘图等非样式表颜色使用。"""
    return _current


def set_current(t: Theme):
    global _current
    _current = t


# ============================ 背景控件 ============================
class BackgroundWidget(QWidget):
    """画背景图的容器：等比裁切铺满（cover），缓存缩放结果，再叠加底色与遮罩。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._src: QPixmap | None = None
        self._path = ""
        self._cache: QPixmap | None = None
        self._cache_size = None
        self._opacity = 1.0
        self._overlay = QColor(0, 0, 0, 0)
        self._base = QColor("#ffffff")
        self.error = ""

    def set_image(self, path: str) -> bool:
        """加载背景图；失败返回 False 并记录 error。路径相同不重复加载。"""
        path = path or ""
        if path == self._path and (self._src is not None or not path):
            return self._src is not None
        self._path = path
        self._src = None
        self._cache = None
        self.error = ""
        if path:
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            # 4K 原图只需按屏幕尺寸读取，降低内存与缩放开销。
            size = reader.size()
            screen = self.screen().availableGeometry().size() if self.screen() else None
            if size.isValid() and screen is not None:
                scale = max(screen.width() * self.devicePixelRatioF() / size.width(),
                            screen.height() * self.devicePixelRatioF() / size.height(), 0.0)
                if scale < 1.0:
                    reader.setScaledSize(size * scale)
            img = reader.read()
            if img.isNull():
                self.error = reader.errorString() or "无法读取图片"
            else:
                self._src = QPixmap.fromImage(img)
        self.update()
        return self._src is not None

    @property
    def has_image(self) -> bool:
        return self._src is not None

    def set_look(self, base: str, opacity: float, overlay: str, overlay_strength: float):
        self._base = QColor(base)
        self._opacity = max(0.0, min(1.0, opacity))
        c = QColor(overlay)
        c.setAlphaF(max(0.0, min(1.0, overlay_strength)))
        self._overlay = c
        self.update()

    def _scaled(self) -> QPixmap | None:
        if self._src is None:
            return None
        dpr = self.devicePixelRatioF()
        size = (round(self.width() * dpr), round(self.height() * dpr))
        if self._cache is None or self._cache_size != size:
            pm = self._src.scaled(size[0], size[1], Qt.KeepAspectRatioByExpanding,
                                  Qt.SmoothTransformation)
            # 居中裁切
            x = max(0, (pm.width() - size[0]) // 2)
            y = max(0, (pm.height() - size[1]) // 3)
            pm = pm.copy(x, y, size[0], size[1])
            pm.setDevicePixelRatio(dpr)
            self._cache, self._cache_size = pm, size
        return self._cache

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(event.rect(), self._base)
        pm = self._scaled()
        if pm is not None:
            p.setOpacity(self._opacity)
            r = event.rect()
            dpr = pm.devicePixelRatio()
            p.drawPixmap(QRectF(r), pm, QRectF(r.x() * dpr, r.y() * dpr,
                                               r.width() * dpr, r.height() * dpr))
            p.setOpacity(1.0)
            if self._overlay.alpha():
                p.fillRect(r, self._overlay)
        p.end()


class ThemeBus(QObject):
    """主题变化广播（单例）。"""
    changed = Signal(object)


theme_bus = ThemeBus()
