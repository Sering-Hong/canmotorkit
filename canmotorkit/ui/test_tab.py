"""响应测试页：只发电流/扭矩激励，得到电流→速度、电流→位置的幅频/相频曲线，并给出模型与 PID 建议。"""
from __future__ import annotations

import csv
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from canmotorkit.analysis import sysid
from canmotorkit.analysis.runs import EXCITE_KINDS, ExciteConfig, ExciteRun

from . import theme
from .widgets import (AdaptiveDoubleSpinBox, CheckBox, SelectedFirstComboBox,
                      equalize_graphics_rows, set_legend_text_color)


def dspin(lo, hi, val, dec=3, step=0.1, suffix=""):
    s = AdaptiveDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    return s


DEFAULT_PHASE_MARGIN = 55.0
DEFAULT_VELOCITY_BANDWIDTH = 20.0


class RawLogAxisItem(pg.AxisItem):
    """对数频率轴使用原始数值显示，避免 30 显示成 3×10¹。"""

    def logTickStrings(self, values, scale, spacing):
        return [format(10.0 ** float(value) * scale, ".6g") for value in values]


class TestTab(QWidget):
    suggestion_ready = Signal(object)        # sysid.Suggestion
    limits_requested = Signal(float, float)  # 速度上限、输出上限

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.driver = None
        self.run: ExciteRun | None = None
        self.model: sysid.Model | None = None
        self.sugg: sysid.Suggestion | None = None
        self._live_dirty = False
        self._analysis_latency = 0.002
        self._analysis_rate = 500.0

        self.cb_kind = SelectedFirstComboBox()
        for k, v in EXCITE_KINDS.items():
            self.cb_kind.addItem(v, k)
        self.cb_kind.setCurrentIndex(1)
        self.sp_amp = dspin(0, 100, 2.5, 3, 0.1)
        self.sp_on = dspin(0.01, 10, 0.3, 3, 0.05, " s")
        self.sp_f0 = dspin(0.01, 200, 1.0, 2, 0.5, " Hz")
        self.sp_f1 = dspin(0.1, 300, 30.0, 2, 1.0, " Hz")
        self.sp_n = QSpinBox()
        self.sp_n.setRange(2, 40)
        self.sp_n.setValue(30)
        self.sp_cyc = QSpinBox()
        self.sp_cyc.setRange(2, 50)
        self.sp_cyc.setValue(6)
        self.sp_dur = dspin(0.5, 120, 5.0, 1, 1.0, " s")
        self.lbl_est = QLabel()
        self.lbl_est.setWordWrap(True)
        self.lbl_est.setProperty("themeRole", "note")

        g = QGroupBox("激励（只发送电流/扭矩）")
        f = QFormLayout(g)
        self.lbl_amp = QLabel("幅值")
        self.lbl_on = QLabel("阶跃持续时间")
        self.lbl_f0 = QLabel("起始频率")
        self.lbl_f1 = QLabel("终止频率")
        self.lbl_n = QLabel("频点数")
        self.lbl_cyc = QLabel("每频点周期")
        self.lbl_dur = QLabel("时长（扫频/PRBS）")
        f.addRow("类型", self.cb_kind)
        f.addRow(self.lbl_amp, self.sp_amp)
        f.addRow(self.lbl_on, self.sp_on)
        f.addRow(self.lbl_f0, self.sp_f0)
        f.addRow(self.lbl_f1, self.sp_f1)
        f.addRow(self.lbl_n, self.sp_n)
        f.addRow(self.lbl_cyc, self.sp_cyc)
        f.addRow(self.lbl_dur, self.sp_dur)
        f.addRow(self.lbl_est)
        self.btn_start = QPushButton("开始测试")
        self.btn_csv = QPushButton("导出数据")
        for button in (self.btn_start, self.btn_csv):
            button.setFixedHeight(32)
        self.btn_start.setStyleSheet("QPushButton{font-weight:bold}")
        self.btn_use = QPushButton("进入闭环测试")
        self.btn_use.setEnabled(False)
        self._test_form = f
        b = QHBoxLayout()
        b.addWidget(self.btn_start, 1)
        b.addWidget(self.btn_csv, 1)
        left = QVBoxLayout()
        left.addWidget(g)
        left.addLayout(b)
        left.addWidget(self.btn_use)
        left.addStretch(1)
        lw = QWidget()
        lw.setLayout(left)
        # This pane has no scroll container, so keep the same minimum width as
        # the left and closed-loop parameter panes.
        lw.setMinimumWidth(350)

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground("w")
        raw_log_axis = lambda: RawLogAxisItem(orientation="bottom")
        self.p_mag = self.glw.addPlot(row=0, col=0, axisItems={"bottom": raw_log_axis()})
        self.p_ph = self.glw.addPlot(row=1, col=0, axisItems={"bottom": raw_log_axis()})
        self.p_out = self.glw.addPlot(row=2, col=0)
        self.p_cl = self.glw.addPlot(row=3, col=0, axisItems={"bottom": raw_log_axis()})
        for p in (self.p_mag, self.p_ph, self.p_cl):
            p.setLogMode(x=True, y=False)
            p.showGrid(x=True, y=True, alpha=0.3)
            p.addLegend(offset=(5, 5))
        self.p_out.showGrid(x=True, y=True, alpha=0.3)
        self.p_out.addLegend(offset=(5, 5))
        self.p_out.setLimits(xMin=0)
        self.p_mag.setLabel("left", "幅值", units="dB")
        self.p_ph.setLabel("left", "相位", units="°")
        self.p_ph.setLabel("bottom", "频率", units="Hz")
        self.p_cl.setLabel("left", "闭环幅值", units="dB")
        self.p_cl.setLabel("bottom", "频率", units="Hz")
        self.p_out.setLabel("left", "输出")
        self.p_out.setLabel("bottom", "时间", units="s")
        sym = dict(symbol="o", symbolSize=6)
        self.c = {
            "mv": self.p_mag.plot(name="电流→速度", pen=pg.mkPen("#1565c0", width=2), symbolBrush="#1565c0", **sym),
            "mp": self.p_mag.plot(name="电流→位置", pen=pg.mkPen("#e65100", width=2), symbolBrush="#e65100", **sym),
            "mm": self.p_mag.plot(name="拟合模型(速度)", pen=pg.mkPen("#9e9e9e", width=1.5, style=Qt.DashLine)),
            "mpp": self.p_mag.plot(name="拟合模型(位置)", pen=pg.mkPen("#616161", width=1.5, style=Qt.DotLine)),
            "pv": self.p_ph.plot(name="电流→速度", pen=pg.mkPen("#1565c0", width=2), symbolBrush="#1565c0", **sym),
            "pp": self.p_ph.plot(name="电流→位置", pen=pg.mkPen("#e65100", width=2), symbolBrush="#e65100", **sym),
            "pm": self.p_ph.plot(name="拟合模型(速度)", pen=pg.mkPen("#9e9e9e", width=1.5, style=Qt.DashLine)),
            "ppp": self.p_ph.plot(name="拟合模型(位置)", pen=pg.mkPen("#616161", width=1.5, style=Qt.DotLine)),
            "tu": self.p_out.plot(name="指令", pen=pg.mkPen("#c62828", width=2)),
            "tfb": self.p_out.plot(name="反馈", pen=pg.mkPen("#1565c0", width=2)),
            "cv": self.p_cl.plot(name="预测速度闭环", pen=pg.mkPen("#1565c0", width=2)),
            "cp": self.p_cl.plot(name="预测串级位置闭环", pen=pg.mkPen("#e65100", width=2)),
            "cs": self.p_cl.plot(name="预测单环位置闭环", pen=pg.mkPen("#00897b", width=2)),
            "c0": self.p_cl.plot(name="0 dB", pen=pg.mkPen("#9e9e9e", width=1.5, style=Qt.DashLine)),
        }
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(1000)
        self.out.setMinimumHeight(0)
        self.out.setMaximumHeight(40)
        self.out.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 中间栏：测试完成后集中显示模型、目标设置和控制器建议。
        self.lbl_vel_bw = QLabel()
        self.lbl_vel_kp = QLabel()
        self.lbl_vel_ki = QLabel()
        self.lbl_pos_bw = QLabel()
        self.lbl_pos_kp = QLabel()
        self.lbl_pos_ki = QLabel()
        self.lbl_pos_kd = QLabel()
        self.lbl_single_pos_bw = QLabel()
        self.lbl_single_pos_kp = QLabel()
        self.lbl_single_pos_ki = QLabel()
        self.lbl_single_pos_kd = QLabel()
        self.lbl_vel_phase_margin = QLabel()
        self.lbl_pos_phase_margin = QLabel()
        self.lbl_single_pos_phase_margin = QLabel()
        self.lbl_model = QLabel("完成一次响应测试后显示")
        self.lbl_model.setWordWrap(True)
        self.lbl_model.setProperty("themeRole", "muted")
        self.lbl_transfer_function = QLabel()
        self.lbl_transfer_function.setWordWrap(False)
        self.lbl_transfer_function.setTextFormat(Qt.RichText)
        self.lbl_transfer_function.setMinimumHeight(0)
        self.lbl_transfer_function.setContentsMargins(0, 0, 0, 0)
        self.lbl_transfer_function.setText(self._transfer_function_html())
        self.sp_target_bw = dspin(0.01, 200.0, DEFAULT_VELOCITY_BANDWIDTH, 2, 0.5, " Hz")
        self.sp_single_pos_target_bw = dspin(0.01, 200.0, 10.0, 2, 0.5, " Hz")
        self.sp_target_bw.setToolTip("修改后立即重算速度 PI、位置外环 PID、相位裕度和预测闭环曲线")
        self.sp_single_pos_target_bw.setToolTip("修改后立即重算单环位置 PID、相位裕度和预测闭环曲线")
        self._analysis_controls = (self.sp_target_bw, self.sp_single_pos_target_bw)
        for control in self._analysis_controls:
            control.setEnabled(False)

        controller_box = QGroupBox("控制器建议")
        controller_form = QFormLayout(controller_box)
        def add_controller_gap():
            gap = QWidget()
            gap.setFixedHeight(6)
            controller_form.addRow(gap)

        controller_form.addRow("速度环带宽", self.lbl_vel_bw)
        controller_form.addRow("速度环 Kp", self.lbl_vel_kp)
        controller_form.addRow("速度环 Ki", self.lbl_vel_ki)
        add_controller_gap()
        controller_form.addRow("单环位置带宽", self.lbl_single_pos_bw)
        controller_form.addRow("单环位置 Kp", self.lbl_single_pos_kp)
        controller_form.addRow("单环位置 Ki", self.lbl_single_pos_ki)
        controller_form.addRow("单环位置 Kd", self.lbl_single_pos_kd)
        add_controller_gap()
        controller_form.addRow("串级位置外环带宽", self.lbl_pos_bw)
        controller_form.addRow("串级位置外环 Kp", self.lbl_pos_kp)
        controller_form.addRow("串级位置外环 Ki", self.lbl_pos_ki)
        controller_form.addRow("串级位置外环 Kd", self.lbl_pos_kd)

        analysis_box = QGroupBox("辨识与计算设置")
        analysis_layout = QVBoxLayout(analysis_box)
        analysis_layout.setSpacing(2)
        analysis_layout.addWidget(self.lbl_model)
        analysis_layout.addWidget(self.lbl_transfer_function)
        target_form = QFormLayout()
        target_form.addRow("速度环期望带宽", self.sp_target_bw)
        target_form.addRow("单环位置期望带宽", self.sp_single_pos_target_bw)
        analysis_layout.addLayout(target_form)
        margin_form = QFormLayout()
        margin_form.addRow("速度内环相位裕度", self.lbl_vel_phase_margin)
        margin_form.addRow("单环位置相位裕度", self.lbl_single_pos_phase_margin)
        margin_form.addRow("串级位置外环相位裕度", self.lbl_pos_phase_margin)
        analysis_layout.addLayout(margin_form)

        middle = QWidget()
        middle.setMinimumWidth(350)
        middle_layout = QVBoxLayout(middle)
        middle_layout.addWidget(controller_box)
        middle_layout.addWidget(analysis_box)
        middle_layout.addWidget(self.out, 1)

        # 响应测试页的参数和建议共用一个中间栏：测试参数在上，
        # 辨识结果与控制器建议在下，右侧只保留曲线区域。
        center = QWidget()
        center.setMinimumWidth(350)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(lw)
        center_layout.addWidget(middle, 1)
        center.setMinimumHeight(0)

        center_scroll = QScrollArea()
        center_scroll.setObjectName("testCenterScroll")
        center_scroll.setWidget(center)
        center_scroll.setWidgetResizable(True)
        center_scroll.setMinimumWidth(350)
        center_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.columns = QSplitter(Qt.Horizontal)
        self.columns.setObjectName("testSplitter")
        self.columns.addWidget(center_scroll)
        self.columns.addWidget(self.glw)
        self.columns.setChildrenCollapsible(False)
        self.columns.setCollapsible(0, False)
        self.columns.setCollapsible(1, False)
        self.columns.setHandleWidth(6)
        self.columns.setStretchFactor(1, 1)
        self.columns.setSizes([740, 700])
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.columns)

        self.cb_kind.currentIndexChanged.connect(self._update)
        for w in (self.sp_amp, self.sp_f0, self.sp_f1, self.sp_n, self.sp_cyc,
                  self.sp_dur, self.sp_on):
            w.valueChanged.connect(self._update)
        self.btn_start.clicked.connect(self._toggle_test)
        self.btn_csv.clicked.connect(self.export)
        self.btn_use.clicked.connect(self._use_suggestion)
        self.sp_target_bw.valueChanged.connect(self._recalculate_suggestion)
        self.sp_single_pos_target_bw.valueChanged.connect(self._recalculate_suggestion)
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(250)
        self._live_timer.timeout.connect(self._refresh_live_result)
        self._live_timer.start()
        self._update()
        self.apply_theme(True)
        self._equalize_plot_rows()
        self.set_bound(None)

    def set_bound(self, worker):
        self.worker = worker
        self.set_driver(None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._equalize_plot_rows)

    def _equalize_plot_rows(self):
        equalize_graphics_rows(self.glw)

    def apply_theme(self, dark: bool):
        """同步响应测试曲线的背景、坐标轴和图例颜色。"""
        background = theme.current().plot_bg()
        foreground = theme.current().pal.text
        self.glw.setBackground(background)
        for plot in (self.p_mag, self.p_ph, self.p_out, self.p_cl):
            for axis_name in ("left", "bottom", "right", "top"):
                axis = plot.getAxis(axis_name)
                axis.setPen(pg.mkPen(foreground))
                axis.setTextPen(pg.mkPen(foreground))
            legend = getattr(plot, "legend", None)
            set_legend_text_color(legend, foreground)

    def set_driver(self, d):
        self.driver = d
        on = self.worker is not None and d is not None
        self.btn_start.setEnabled(on)
        self.btn_start.setText("开始测试")
        u = d.out_unit if d else ""
        self.sp_amp.setSuffix(f" {u}" if u else "")
        self.p_out.setLabel("left", "输出", units=u)
        if d is not None:
            self.sp_amp.setMaximum(d.out_max)
        else:
            self.reset_for_no_driver()

    def reset_for_no_driver(self):
        """清除本次电机调试产生的数据，并恢复响应测试初始参数。"""
        self.run = None
        self._clear_result_curves()
        self.out.clear()
        self._reset_analysis_panel()
        controls = (
            (self.cb_kind, 1), (self.sp_amp, 2.5), (self.sp_on, 0.3),
            (self.sp_f0, 1.0), (self.sp_f1, 30.0),
            (self.sp_n, 30), (self.sp_cyc, 6), (self.sp_dur, 5.0),
        )
        for widget, value in controls:
            widget.blockSignals(True)
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(value)
            else:
                widget.setValue(value)
            widget.blockSignals(False)
        self.sp_amp.setMaximum(100.0)
        self._update()

    def on_state(self, s: dict):
        if self.driver is None:
            return
        running = s["running"]
        self.btn_start.setText("停止" if running else "开始测试")
        self.btn_start.setEnabled(running or (s["active"] and not s["tripped"]))

    def _toggle_test(self):
        if self.run is not None and not self.run.done:
            if self.worker is not None:
                self.worker.stop_run()
            self.btn_start.setText("开始测试")
            return
        self.start()

    def on_samples(self, samples):
        """收到实时反馈后标记响应结果，定时器负责节流刷新频响图。"""
        if samples and self.run is not None and not self.run.done:
            self._live_dirty = True

    def _update(self):
        self._clear_result_curves()
        k = self.cb_kind.currentData()
        self._set_row_visible(self._test_form, self.lbl_on, self.sp_on, k == "step")
        self._set_row_visible(self._test_form, self.lbl_f0, self.sp_f0, k in ("stepsine", "chirp"))
        self._set_row_visible(self._test_form, self.lbl_f1, self.sp_f1, k in ("stepsine", "chirp"))
        self._set_row_visible(self._test_form, self.lbl_n, self.sp_n, k == "stepsine")
        self._set_row_visible(self._test_form, self.lbl_cyc, self.sp_cyc, k == "stepsine")
        self._set_row_visible(self._test_form, self.lbl_dur, self.sp_dur, k in ("chirp", "prbs"))
        try:
            run = ExciteRun(self._cfg())
            self.lbl_est.setText(f"预计时长 {run.total:.1f} s")
        except Exception:
            self.lbl_est.setText("")
        self._request_limit_sync()
        self._update_command_preview()

    @staticmethod
    def _set_row_visible(form, label, field, visible):
        if hasattr(form, "setRowVisible"):
            form.setRowVisible(label, visible)
        else:
            label.setVisible(visible)
            field.setVisible(visible)

    def _clear_result_curves(self):
        """激励参数变化后清空上一轮响应结果。"""
        if not hasattr(self, "c"):
            return
        for curve in self.c.values():
            curve.setData([], [])
        self.model = None
        self.sugg = None
        if hasattr(self, "c"):
            self.c["cv"].setData([], [])
            self.c["cp"].setData([], [])
            self.c["cs"].setData([], [])
            self.c["c0"].setData([], [])
        if hasattr(self, "btn_use"):
            self.btn_use.setEnabled(False)

    def _reset_analysis_panel(self):
        if not hasattr(self, "sp_target_bw"):
            return
        for control in self._analysis_controls:
            control.setEnabled(False)
        for label in (self.lbl_vel_bw, self.lbl_vel_kp, self.lbl_vel_ki,
                      self.lbl_pos_bw, self.lbl_pos_kp, self.lbl_pos_ki,
                      self.lbl_pos_kd, self.lbl_single_pos_bw,
                      self.lbl_single_pos_kp, self.lbl_single_pos_ki,
                      self.lbl_single_pos_kd, self.lbl_vel_phase_margin,
                      self.lbl_pos_phase_margin, self.lbl_single_pos_phase_margin):
            label.clear()
        self.lbl_model.setText("完成一次响应测试后显示")
        self.lbl_transfer_function.setText(self._transfer_function_html())

    @staticmethod
    def _transfer_function_html(model=None):
        """用富文本表格把速度开环模型排成分式，便于直接展示给用户。"""
        if model is None:
            denominator = "J·s + b"
            delay = "-T<sub>d</sub>·s"
        else:
            denominator = f"{model.J:.7g}·s + {model.b:.7g}"
            delay = f"-{model.Td * 1000:.4g} ms·s"
        return (
            "<table cellspacing='0' cellpadding='0'>"
            "<tr><td colspan='3'><b><span style='font-size:10pt'>速度开环传递函数（Transfer Function）</span></b></td></tr>"
            "<tr><td colspan='3' height='1'></td></tr>"
            "<tr>"
            "<td valign='middle'><span style='font-size:12pt'>G<sub>v</sub>(s) = </span></td>"
            "<td align='center' valign='middle'>"
            "<table cellspacing='0' cellpadding='0'><tr><td align='center'>"
            "<span style='font-size:12pt'>1</span></td></tr>"
            "<tr><td><hr></td></tr>"
            f"<tr><td align='center'><span style='font-size:10pt'>{denominator}</span></td></tr></table>"
            "</td>"
            f"<td valign='middle'><span style='font-size:12pt'> · e<sup>{delay}</sup></span></td>"
            "</tr></table>"
        )

    def _set_suggestion_labels(self, s):
        u = s.unit
        self.lbl_vel_bw.setText(f"{s.vel_bw:.4g} Hz")
        self.lbl_vel_kp.setText(f"{s.vel_kp:.7g} {u}/(rad/s)")
        self.lbl_vel_ki.setText(f"{s.vel_ki:.7g} {u}/rad")
        self.lbl_pos_bw.setText(f"{s.pos_bw:.4g} Hz")
        self.lbl_pos_kp.setText(f"{s.pos_kp:.7g} (rad/s)/rad")
        self.lbl_pos_ki.setText(f"{s.pos_ki:.7g} (rad/s)/(rad·s)")
        self.lbl_pos_kd.setText(f"{s.pos_kd:.7g} (rad/s)/(rad/s)")
        self.lbl_single_pos_bw.setText(f"{s.single_pos_bw:.4g} Hz")
        self.lbl_single_pos_kp.setText(f"{s.single_pos_kp:.7g} {u}/rad")
        self.lbl_single_pos_ki.setText(f"{s.single_pos_ki:.7g} {u}/(rad·s)")
        self.lbl_single_pos_kd.setText(f"{s.single_pos_kd:.7g} {u}/(rad/s)")
        self.lbl_vel_phase_margin.setText(f"{s.vel_phase_margin:.4g} °")
        self.lbl_pos_phase_margin.setText(f"{s.pos_phase_margin:.4g} °")
        self.lbl_single_pos_phase_margin.setText(f"{s.single_pos_phase_margin:.4g} °")

    def _recalculate_suggestion(self):
        if self.model is None:
            return
        self.sugg = sysid.suggest(
            self.model,
            latency=self._analysis_latency,
            rate_hz=self._analysis_rate,
            pm_deg=DEFAULT_PHASE_MARGIN,
            vel_bw_hz=self.sp_target_bw.value(),
            start_ratio=1.0,
            single_pos_bw_hz=self.sp_single_pos_target_bw.value(),
        )
        self._set_suggestion_labels(self.sugg)
        self._plot_predicted_closed_loop()
        self.btn_use.setEnabled(True)

    def _use_suggestion(self):
        if self.sugg is not None:
            self.suggestion_ready.emit(self.sugg)

    def _request_limit_sync(self):
        """通知主窗口把响应测试所需的范围同步到左侧。"""
        self.limits_requested.emit(0.0, abs(self.sp_amp.value()))

    def _cfg(self) -> ExciteConfig:
        return ExciteConfig(kind=self.cb_kind.currentData(), amp=self.sp_amp.value(),
                            step_on=self.sp_on.value(),
                            f0=self.sp_f0.value(), f1=max(self.sp_f1.value(), self.sp_f0.value() * 1.01),
                            n_freq=self.sp_n.value(), cycles=self.sp_cyc.value(),
                            duration=self.sp_dur.value())

    def start(self):
        w = self.worker
        if not w or not self.driver:
            return
        if not w.active:
            QMessageBox.information(self, "响应测试", "请先使能电机")
            return
        self._request_limit_sync()
        cfg = self._cfg()
        self.run = ExciteRun(cfg)
        for c in self.c.values():
            c.setData([], [])
        self.out.clear()
        self._update_command_preview()
        self._live_dirty = False
        w.start_run(self.run)
        self.btn_start.setText("停止")

    def _refresh_live_result(self):
        """用当前已完成的数据增量绘制频响预览，不生成最终模型。"""
        if not self._live_dirty or self.run is None or self.run.done:
            return
        self._live_dirty = False
        self._set_time_curves(self.run)
        if self.run.cfg.kind == "step":
            return
        bode = sysid.bode_from_run(self.run)
        if bode is None:
            return
        self._set_bode_curves(bode)
        self._fit_result_plots()

    def _update_command_preview(self):
        """按当前参数预览响应测试将要下发的激励指令。"""
        if not hasattr(self, "p_out"):
            return
        try:
            run = ExciteRun(self._cfg())
        except Exception:
            self.c["tu"].setData([], [])
            return
        total = max(run.total, 1e-3)
        count = max(200, min(4000, int(total * 200) + 1))
        t = np.linspace(0.0, total, count)
        command = np.asarray([run.excitation(float(x)) for x in t])
        self.c["tu"].setData(t, command, connect="finite")
        self.c["tfb"].setData([], [])
        self.p_out.setXRange(0.0, total, padding=0)
        self.p_out.enableAutoRange(y=True)
        self.p_out.autoRange(padding=0.08)

    def on_run_finished(self, run):
        if run is not self.run:
            return
        n = len(run.rec)
        self._set_time_curves(run)
        if n < 10:
            return
        unit = self.driver.out_unit if self.driver else ""
        lat = self.worker.latency_stats().get("p50", 0.002) if self.worker else 0.002
        rate = self.worker.rate_hz if self.worker else 500
        if run.cfg.kind == "step":
            m = sysid.step_model(run.rec, run.cfg.amp, run.cfg.step_delay, run.cfg.step_on, unit)
            if m is None:
                self.out.appendPlainText("  阶跃辨识失败：电机可能未转动（幅值低于静摩擦？）")
                return
            self._show_model(m, None, lat, rate)
            return
        bode = sysid.bode_from_run(run)
        if bode is None:
            self.out.appendPlainText("  频响计算失败：有效数据不足（电机是否转动或激励幅值是否足够？）")
            return
        self._set_bode_curves(bode)
        f = bode.f
        db = lambda x: 20 * np.log10(np.maximum(x, 1e-12))
        bad_v = int(np.sum(bode.coh < 0.6))
        bad_p = int(np.sum(bode.coh_p < 0.6))
        self.out.appendPlainText(
            f"  拟合质量低：电流→速度 {bad_v} 个频点，电流→位置 {bad_p} 个频点"
        )
        m = sysid.fit_model(bode, unit)
        if m is not None:
            w = 2 * np.pi * f
            g = np.exp(-1j * w * m.Td) / (m.J * 1j * w + m.b)
            g_position = g / (1j * w)
            self.c["mm"].setData(f, db(np.abs(g)))
            self.c["mpp"].setData(f, db(np.abs(g_position)))
            self.c["pm"].setData(f, np.degrees(np.unwrap(np.angle(g))))
            self.c["ppp"].setData(f, np.degrees(np.unwrap(np.angle(g_position))))
            self._show_model(m, bode, lat, rate)
        self._fit_result_plots()

    def _set_bode_curves(self, bode):
        f = bode.f
        db = lambda x: 20 * np.log10(np.maximum(x, 1e-12))
        self.c["mv"].setData(f, db(bode.mag_v))
        self.c["mp"].setData(f, db(bode.mag_p))
        self.c["pv"].setData(f, bode.ph_v)
        self.c["pp"].setData(f, bode.ph_p)

    def _set_time_curves(self, run):
        rec = run.snapshot_record() if hasattr(run, "snapshot_record") else run.rec
        if not rec.t:
            return
        t = np.asarray(rec.t, dtype=float)
        self.c["tu"].setData(t, np.asarray(rec.u, dtype=float), connect="finite")
        self.c["tfb"].setData(t, np.asarray(rec.fb_out, dtype=float), connect="finite")
        self.p_out.enableAutoRange(x=True, y=True)
        self.p_out.autoRange(padding=0.08)

    def _show_model(self, m, bode, lat, rate):
        self.model = m
        self._analysis_latency = lat
        self._analysis_rate = rate
        base = sysid.suggest(m, latency=lat, rate_hz=rate)
        for widget, value in ((self.sp_target_bw, DEFAULT_VELOCITY_BANDWIDTH),
                              (self.sp_single_pos_target_bw, base.single_pos_bw)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        for control in self._analysis_controls:
            control.setEnabled(True)
        self.lbl_model.setText(m.text())
        # 传递函数只展示通用模型结构；本次辨识的 J、b、Td 已在上方物理模型中显示。
        self.lbl_transfer_function.setText(self._transfer_function_html())
        self._recalculate_suggestion()

    def _plot_predicted_closed_loop(self):
        if self.model is None or self.sugg is None:
            return
        f, vel_db, pos_db, single_pos_db = sysid.predicted_closed_loop(
            self.model, self.sugg, latency=self._analysis_latency,
            rate_hz=self._analysis_rate, fmin=0.05)
        self.c["cv"].setData(f, vel_db)
        self.c["cp"].setData(f, pos_db)
        self.c["cs"].setData(f, single_pos_db)
        self.c["c0"].setData(f, np.zeros_like(f))
        self.p_cl.enableAutoRange(x=True, y=True)
        self.p_cl.autoRange(padding=0.08)

    def _fit_result_plots(self):
        """让幅值/相位图在结果曲线写入后覆盖全部数据。"""
        for plot in (self.p_mag, self.p_ph, self.p_cl):
            plot.enableAutoRange(x=True, y=True)
            plot.autoRange(padding=0.08)

    def export(self):
        if self.run is None or not len(self.run.rec):
            QMessageBox.information(self, "导出", "没有数据")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出响应测试", "response.csv", "CSV (*.csv)")
        if not path:
            return
        r = self.run.rec
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["t", "u", "excite", "pos", "vel", "out_fb", "seg"])
            for row in zip(r.t, r.u, r.ref, r.pos, r.vel, r.fb_out, r.seg):
                w.writerow(["" if isinstance(x, float) and math.isnan(x) else f"{x:.7g}" for x in row])
        self.out.appendPlainText(f"已导出 {path}")
