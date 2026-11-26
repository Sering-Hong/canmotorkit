"""主窗口：左栏（① PCAN 连接 → ② 总线电机 → ③ 安全限制/急停），右侧顶部电机卡片 + 标签页。"""
from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QLabel, QLineEdit, QMainWindow,
                               QHBoxLayout, QMenu, QMessageBox, QPlainTextEdit, QScrollArea,
                               QSizePolicy, QSplitter, QTabWidget, QTableWidget, QPushButton,
                               QVBoxLayout, QWidget)

from canmotorkit import APP_NAME, __version__
from canmotorkit.core.bus import PcanBus
from canmotorkit.core.dm_usb2can import DmUsb2CanBus
from canmotorkit.core.worker import Worker
from canmotorkit.core.slcan import SlcanBus
from canmotorkit.core.socketcan import SocketCanBus
from canmotorkit.sim.demo import DemoBus

from .connection_panel import ConnectionPanel
from .control_tab import ControlPanel
from .loop_tab import LoopTab
from .motor_card import MotorCard
from .motor_list import MotorListPanel
from .plot_widget import PlotWidget
from .register_tab import RegisterTab
from . import theme
from .appearance_dialog import AppearanceDialog
from .guide_bar import GuideBar
from .widgets import CheckBox
from .safety_panel import SafetyPanel
from .scan_log_tab import ScanLogTab
from .test_tab import TestTab

TEXT_INPUTS = (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTableWidget)

class _SpaceEstop(QObject):
    """空格急停（焦点在输入控件时不触发）。"""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Space and not ev.isAutoRepeat():
            if not isinstance(QApplication.focusWidget(), TEXT_INPUTS):
                self.fn()
                return True
        return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self._settings = theme.settings()
        self._appearance = theme.Appearance.load(self._settings)
        self._appearance_dialog = None
        self._start_tab = 0
        self._last_state = None
        self.resize(1500, 920)
        self.bus = None
        self.worker: Worker | None = None
        self.driver = None

        self.conn = ConnectionPanel()
        self.motors = MotorListPanel()
        self.safety = SafetyPanel()
        self.card = MotorCard()
        self.safety.set_output_controls(self.card.output_panel, self.card.service_panel, self.card.rate_panel)
        self.safety.setTitle("③ 安全限制 / 急停")
        left = QWidget()
        left.setObjectName("leftColumn")
        # The scroll area owns the minimum pane width. Let its content follow
        # the viewport instead of retaining the widest child size hint.
        left.setMinimumWidth(0)
        left.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.addWidget(self.conn)
        ll.addWidget(self.motors)
        ll.addWidget(self.safety)
        ll.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("leftScroll")
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        # 左栏保留至少 350 px，避免控件被压缩得难以阅读。
        scroll.setMinimumWidth(350)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.control = ControlPanel()
        self.safety.limits_changed.connect(self.control.set_output_limit)
        self.control.setObjectName("controlPage")
        self.mini_plot = PlotWidget(capacity=60_000, compact=True)
        # The curve pane may be compressed when the data pane is widened.
        self.mini_plot.setMinimumWidth(0)
        self.mini_plot.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        ctl = QSplitter(Qt.Horizontal)
        ctl.setObjectName("controlSplitter")
        cs = QScrollArea()
        cs.setObjectName("controlScroll")
        cs.setWidget(self.control)
        cs.setWidgetResizable(True)
        cs.setMinimumWidth(350)
        cs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ctl.addWidget(cs)
        ctl.addWidget(self.mini_plot)
        ctl.setChildrenCollapsible(False)
        ctl.setCollapsible(0, False)
        ctl.setCollapsible(1, True)
        ctl.setHandleWidth(6)
        ctl.setStretchFactor(1, 1)
        ctl.setSizes([350, 700])
        self.test = TestTab()
        self.loop = LoopTab()
        self.regs = RegisterTab()
        self.scan = ScanLogTab()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(ctl, "控制")
        self.tabs.addTab(self.test, "响应测试")
        self.tabs.addTab(self.loop, "闭环测试")
        self.tabs.addTab(self.regs, "电机参数")
        self.tabs.addTab(self.scan, "报文")

        right = QWidget()
        right.setObjectName("rightColumn")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        topbar = QHBoxLayout()
        topbar.setContentsMargins(0, 0, 4, 0)
        topbar.addWidget(self.card.lbl_title)
        topbar.addStretch(1)
        self.chk_simple = CheckBox("简洁模式")
        self.chk_simple.setToolTip("只保留连接、选电机、使能和输出控制；\n"
                                   "取消勾选显示响应测试、闭环测试、参数寄存器、报文等高级功能")
        topbar.addWidget(self.chk_simple)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setToolTip("外观、主题与软件信息")
        self._theme_menu = QMenu(self.btn_settings)
        act = self._theme_menu.addAction("使用说明")
        act.triggered.connect(self.open_help)
        act = self._theme_menu.addAction("外观设置…（背景图 / 透明度 / 强调色）")
        act.triggered.connect(self.open_appearance)
        self._theme_menu.addSeparator()
        self._theme_actions = {}
        theme_group = QActionGroup(self._theme_menu)
        theme_group.setExclusive(True)
        for mode, label in (("light", "浅色（樱花）"), ("dark", "深色模式"),
                            ("system", "跟随系统")):
            action = QAction(label, theme_group)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=mode: self._set_theme(value))
            self._theme_menu.addAction(action)
            self._theme_actions[mode] = action
        self._theme_menu.addSeparator()
        act = self._theme_menu.addAction("打开日志目录")
        act.triggered.connect(self._open_log_dir)
        act = self._theme_menu.addAction("重置窗口布局")
        act.triggered.connect(self._reset_layout)
        act = self._theme_menu.addAction(f"关于 {APP_NAME}")
        act.triggered.connect(self._about)
        self.btn_settings.clicked.connect(self._show_theme_menu)
        topbar.addWidget(self.btn_settings)
        rl.addLayout(topbar)
        self.guide = GuideBar(self)
        rl.addWidget(self.guide)
        rl.addWidget(self.tabs, 1)
        # Keep the complete right section usable; its internal curve panes
        # are intentionally allowed to shrink with their splitters.
        right.setMinimumWidth(700)
        right.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sp = QSplitter(Qt.Horizontal)
        sp.setObjectName("mainSplitter")
        sp.addWidget(scroll)
        sp.addWidget(right)
        # Keep the splitter's hit area at the pane border without showing a
        # second visible bar over the existing pane border.
        sp.setHandleWidth(6)
        sp.setOpaqueResize(True)
        sp.setChildrenCollapsible(False)
        sp.setCollapsible(0, False)
        sp.setCollapsible(1, False)
        sp.setStretchFactor(1, 1)
        sp.setSizes([450, 1050])
        self.main_splitter = sp
        self.background = theme.BackgroundWidget()
        bl = QVBoxLayout(self.background)
        bl.setContentsMargins(6, 6, 6, 0)
        bl.addWidget(sp)
        self.setCentralWidget(self.background)

        sb = self.statusBar()
        self.st_conn = QLabel("未连接")
        self.st_conn.setStyleSheet("padding:0 8px")
        sb.addPermanentWidget(self.st_conn)

        self.panels = (self.motors, self.safety, self.card, self.control, self.test, self.loop,
                       self.regs, self.scan)
        self.conn.connect_requested.connect(self.connect_bus)
        self.conn.disconnect_requested.connect(self.disconnect_bus)
        self.motors.motor_selected.connect(self.select_motor)
        self.test.suggestion_ready.connect(self._use_suggestion)
        self.test.limits_requested.connect(self._raise_test_limits)
        self.loop.limits_requested.connect(self._raise_test_limits)
        self.regs.ids_changed.connect(lambda *_: self.select_motor(None))
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.safety.estop,
                  context=Qt.ApplicationShortcut)
        self._space = _SpaceEstop(self.safety.estop)
        QApplication.instance().installEventFilter(self._space)
        QApplication.instance().installEventFilter(self)
        self._apply_appearance()
        # 各页面的参数栏与曲线栏使用同一默认比例，并保持拖动比例同步。
        self._pane_splitters = (ctl, self.loop.columns, self.test.columns)
        # Initial ratio after the right pane is laid out at the default window
        # size. Hidden tabs are laid out lazily, so their sizes are applied
        # again when they become visible.
        self._pane_ratio = 350 / 1050
        self._syncing_panes = False
        for splitter in self._pane_splitters:
            splitter.splitterMoved.connect(
                lambda _pos, _index, source=splitter: self._pane_splitter_moved(source)
            )
        self.tabs.currentChanged.connect(
            lambda _index: QTimer.singleShot(0, self._apply_pane_ratio)
        )
        self._restore_layout()
        simple = self._settings.value("ui/simple", True) in (True, "true", "1", 1)
        self.chk_simple.setChecked(simple)
        self.chk_simple.toggled.connect(self._set_simple)
        self._set_simple(simple)
        QTimer.singleShot(0, self._apply_pane_ratio)
        if self.conn.pcan_error:
            self.statusBar().showMessage(self.conn.pcan_error + "（仍可使用演示台）", 15000)

    # ---------- 连接 ----------
    @staticmethod
    def _system_is_dark() -> bool:
        return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _show_theme_menu(self, _checked=False):
        width = self._theme_menu.sizeHint().width()
        pos = self.btn_settings.mapToGlobal(
            QPoint(max(0, self.btn_settings.width() - width), self.btn_settings.height())
        )
        self._theme_menu.popup(pos)

    def _pane_splitter_moved(self, source):
        if self._syncing_panes:
            return
        sizes = source.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        self._pane_ratio = max(0.0, min(1.0, sizes[0] / total))
        self._apply_pane_ratio()

    def _apply_pane_ratio(self):
        if getattr(self, "_syncing_panes", False):
            return
        self._syncing_panes = True
        try:
            for splitter in getattr(self, "_pane_splitters", ()):
                total = sum(splitter.sizes())
                if total <= 0:
                    continue
                left = round(total * self._pane_ratio)
                splitter.setSizes([left, total - left])
        finally:
            self._syncing_panes = False

    def _is_dark(self) -> bool:
        mode = self._appearance.mode
        return mode == "dark" or (mode == "system" and self._system_is_dark())

    def _set_theme(self, mode: str, persist: bool = True):
        if mode not in ("dark", "light", "system"):
            mode = "light"
        self._appearance.mode = mode
        if persist:
            self._appearance.save(self._settings)
        self._apply_appearance()
        if self._appearance_dialog is not None:
            self._appearance_dialog.sync_from(self._appearance)

    def _apply_appearance(self, appearance=None):
        """按当前外观设置刷新背景图、样式表和各面板绘图颜色。"""
        if appearance is not None:
            self._appearance = appearance
        a = self._appearance
        dark = self._is_dark()
        v = a.variant(dark)
        bg = self.background
        want_img = bool(v.bg_enabled and a.bg_path)
        has_img = bg.set_image(a.bg_path if want_img else "")
        if want_img and not has_img:
            self.statusBar().showMessage(f"背景图加载失败：{bg.error or a.bg_path}", 8000)
        if self._appearance_dialog is not None:
            self._appearance_dialog.set_image_error(bg.error if want_img else "")
        t = theme.build_theme(a, dark, has_img)
        theme.set_current(t)
        pal = t.pal
        bg.set_look(pal.window, v.bg_opacity / 100.0, pal.overlay, v.overlay / 100.0)
        self.setStyleSheet(t.stylesheet())
        self._set_native_titlebar(dark)
        for key, action in self._theme_actions.items():
            action.setChecked(key == a.mode)
        self.motors.apply_theme(dark)
        self.control.apply_theme(dark)
        self.regs.apply_theme(dark)
        for panel in (self.mini_plot, self.loop, self.test, self.scan):
            panel.apply_theme(dark)
        theme.theme_bus.changed.emit(t)

    def open_appearance(self):
        if self._appearance_dialog is None:
            dlg = AppearanceDialog(self._appearance, self._is_dark, self)
            dlg.appearance_changed.connect(self._apply_appearance)
            dlg.finished.connect(self._appearance_closed)
            self._appearance_dialog = dlg
        self._appearance_dialog.show()
        self._appearance_dialog.raise_()
        self._appearance_dialog.activateWindow()

    def _appearance_closed(self, _result=0):
        self._appearance.save(self._settings)
        self._appearance_dialog.deleteLater()
        self._appearance_dialog = None

    # ---------- 布局记忆 ----------
    def _restore_layout(self):
        s = self._settings
        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = s.value("window/main_splitter")
        if state is not None:
            self.main_splitter.restoreState(state)
        try:
            ratio = float(s.value("window/pane_ratio", self._pane_ratio))
            if 0.05 < ratio < 0.95:
                self._pane_ratio = ratio
        except (TypeError, ValueError):
            pass

    def _save_layout(self):
        s = self._settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/main_splitter", self.main_splitter.saveState())
        s.setValue("window/pane_ratio", self._pane_ratio)
        self.conn.save_settings(s)
        self.card.save_settings(s)
        self.safety.save_settings(s)
        self._appearance.save(s)

    def _reset_layout(self):
        for key in ("window/geometry", "window/main_splitter", "window/pane_ratio"):
            self._settings.remove(key)
        self.showNormal()
        self.resize(1500, 920)
        self.main_splitter.setSizes([450, 1050])
        self._pane_ratio = 350 / 1050
        self._apply_pane_ratio()

    # ---------- 简洁模式 ----------
    def _set_simple(self, on: bool):
        """简洁模式只保留日常调试需要的内容，高级功能隐藏但不改变其状态。"""
        self._settings.setValue("ui/simple", bool(on))
        for i in range(1, self.tabs.count()):
            self.tabs.setTabVisible(i, not on)
        if on:
            self.tabs.setCurrentIndex(0)
        self.tabs.tabBar().setVisible(not on)
        self.safety.set_simple(on)
        self.card.rate_panel.setVisible(not on)
        self.motors.sp_to.setVisible(not on)

    def open_help(self):
        from .help_dialog import HelpDialog
        dlg = getattr(self, "_help_dialog", None)
        if dlg is None:
            dlg = self._help_dialog = HelpDialog(self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _open_log_dir(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from canmotorkit.applog import log_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    def _about(self):
        from canmotorkit.applog import log_dir
        QMessageBox.about(
            self, f"关于 {APP_NAME}",
            f"<b>{APP_NAME}</b> {__version__}<br>"
            "CAN 电机调试与测试上位机：达妙/HM、大疆 RoboMaster、高擎 GD；"
            "PCAN / SLCAN / SocketCAN / 演示台。<br><br>"
            "急停（Esc / 空格）= 零扭矩并锁存，不失能。<br>"
            f"设置与日志目录：{log_dir()}")

    def _set_native_titlebar(self, dark: bool):
        """同步 Windows 原生标题栏，避免浅色内容配深色标题栏。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute

            def set_attr(attr: int, value: int):
                data = ctypes.c_int(value)
                dwm(hwnd, attr, ctypes.byref(data), ctypes.sizeof(data))

            def colorref(rgb):
                r, g, b = rgb
                return r | (g << 8) | (b << 16)

            set_attr(20, 1 if dark else 0)
            set_attr(35, colorref((23, 23, 23) if dark else (244, 244, 244)))
            set_attr(36, colorref((241, 241, 241) if dark else (32, 32, 32)))
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            target = QApplication.widgetAt(event.globalPosition().toPoint())
            table = getattr(self.motors, "tbl", None)
            if table is not None and (target is None or not table.isAncestorOf(target)):
                self.motors.clear_temporary_selection()
        return super().eventFilter(obj, event)

    def connect_bus(self, p: dict):
        try:
            if p.get("kind") == "socketcan":
                bus = SocketCanBus(p["channel"])
            elif p.get("kind") == "slcan":
                bus = SlcanBus(p["channel"], p["bitrate"])
            elif p.get("kind") == "dmusb":
                bus = DmUsb2CanBus(p["channel"], p["bitrate"])
            elif p["sim"]:
                bus = DemoBus()
            else:
                bus = PcanBus(p["channel"], p["baud"])
            bus.open()
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
            return
        self.bus = bus
        w = Worker(bus)
        w.samples.connect(self._on_samples)
        w.samples.connect(self.test.on_samples)
        w.frames.connect(self.scan.on_frames)
        w.reg_done.connect(self.regs.on_reg_done)
        w.gd_done.connect(self.regs.on_gd_done)
        w.gd_done.connect(self._on_gd_done)
        w.scan_progress.connect(self.motors.on_scan_progress)
        w.scan_done.connect(self.motors.on_scan_done)
        w.scan_done.connect(self._auto_pick)
        w.state.connect(self._on_state)
        w.message.connect(self._on_message)
        w.limits_changed.connect(self._on_limits)
        w.run_finished.connect(self.test.on_run_finished)
        w.run_finished.connect(self.loop.on_run_finished)
        self.worker = w
        for p_ in self.panels:
            p_.set_bound(w)
        self.conn.set_connected(True)
        w.start()
        self.st_conn.setText(f"{bus.name} 已连接")
        self._on_message(f"已连接 {p['channel_name']}，开始自动扫描")
        if p.get("kind") == "dmusb" and self.card.sp_rate.value() > 500:
            # 达妙 USB2CAN 每毫秒最多转发 1 帧，1 kHz 下查询/控制帧会互相覆盖。
            self.card.sp_rate.setValue(500)
            self.statusBar().showMessage("达妙 USB2CAN：通信频率已调为 500 Hz（适配器每毫秒最多转发 1 帧）", 10000)
        self.motors.scan_bus()

    def _auto_pick(self, found):
        """总线上只有一台电机时直接选中它（只选中，不使能、不输出）。"""
        if len(found) == 1 and self.driver is None and self.worker is not None:
            QTimer.singleShot(0, lambda: self.motors._use_row(0))

    def disconnect_bus(self):
        w = self.worker
        if w is not None:
            w.stop()
            if not w.wait(1500):
                w.terminate()
                w.wait(500)
            self.worker = None
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
            self.bus = None
        self.driver = None
        self.safety.set_driver(None)
        for p_ in self.panels:
            p_.set_bound(None)
        self.conn.set_connected(False)
        self.st_conn.setText("未连接")
        self._last_state = None

    def closeEvent(self, ev):
        self._save_layout()
        self.disconnect_bus()
        super().closeEvent(ev)

    # ---------- 电机选择 ----------
    def select_motor(self, drv):
        w = self.worker
        if w is None:
            return
        if w.configuration_busy:
            self._on_message("GD 设置正在执行，请等待操作结束再切换电机")
            return
        if w.active:
            QMessageBox.information(self, "切换电机", "请先失能/停止当前电机")
            return
        self.driver = drv
        w.set_driver(drv)
        for p_ in (self.card, self.control, self.test, self.loop, self.regs, self.scan):
            p_.set_driver(drv)
        self.mini_plot.clear()
        if drv is not None:
            self.mini_plot.set_unit(drv.out_unit, drv.out_name)
            self.motors.set_debug_driver(drv)
        else:
            self.mini_plot.set_unit("Nm", "输出")
            self.motors.set_debug_driver(None)
        self.safety.set_driver(drv)
        lim = self.safety.limits()
        self.control.set_output_limit(lim.out_max if drv is not None and lim.out_enabled else None)
        if drv is None:
            self.tabs.setCurrentIndex(0)
        if drv is not None:
            self.tabs.setCurrentIndex(0)

    # ---------- 回调 ----------
    def _on_gd_done(self, kind, value, error):
        d = self.driver
        if error or d is None or d.protocol != "gd":
            return
        if kind in ("change_id", "model"):
            row = self.motors._debug_row
            if row is not None and 0 <= row < len(self.motors.entries):
                entry = self.motors.entries[row]
                entry.update(model=d.model, motor_id=d.motor_id, fb_id=d.fb_id,
                             candidates=[(d.model, d.motor_id)], gd_identity=dict(d.identity))
                self.motors._refresh()
                self.motors.set_debug_driver(d)
            self.card.lbl_title.setText(d.model)
            self.scan.set_driver(d)

    def _on_samples(self, samples):
        self.mini_plot.add_samples(samples)
        self.control.on_sample(samples[-1])
        self.loop.on_samples(samples)

    def _on_state(self, s: dict):
        self._last_state = s
        for p_ in (self.card, self.control, self.safety, self.motors, self.test, self.loop, self.regs):
            p_.on_state(s)
        run = self.worker._run_obj if self.worker else None
        target = ""
        if run is not None:
            cfg = getattr(run, "cfg", None)
            target = getattr(cfg, "loop", "") if cfg is not None and hasattr(cfg, "loop") else ""
        self.mini_plot.set_ref_target(target)

    def _on_limits(self, lim, ok: bool):
        self.control.set_limits(lim, ok)
        safety_lim = self.safety.limits()
        self.control.set_output_limit(safety_lim.out_max
                                      if self.driver is not None and safety_lim.out_enabled else None)
        self.regs.set_limits(lim, ok)

    def _on_message(self, msg: str):
        self.statusBar().showMessage(msg, 8000)

    def _raise_test_limits(self, vel_max: float, out_max: float):
        if self.safety.raise_for_test(vel_max, out_max):
            self.statusBar().showMessage("已按测试参数提高左侧速度/输出限幅", 3000)

    def _use_suggestion(self, s):
        self.loop.set_suggestion(s)
        self.tabs.setCurrentWidget(self.loop)
