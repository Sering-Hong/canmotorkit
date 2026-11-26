"""闭环测试页：速度 PI 或串级位置环，输出为电流/扭矩。仅用于测试与辨识。"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget, QSizePolicy)

from canmotorkit.analysis import sysid
from canmotorkit.analysis.runs import LOOP_KINDS, REF_KINDS, LoopConfig, LoopRun

from . import theme
from .test_tab import dspin
from .widgets import (CheckBox, SelectedFirstComboBox, equalize_graphics_rows,
                      set_legend_text_color)


class LoopTab(QWidget):
    limits_requested = Signal(float, float)  # 速度上限、输出上限

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.driver = None
        self.run: LoopRun | None = None
        self.sugg = None
        self._live_t = []
        self._live_ref = []
        self._live_y = []
        self._live_u = []
        self._live_fb = []
        self._live_p = []
        self._live_i = []
        self._live_d = []

        self.cb_loop = SelectedFirstComboBox()
        for k, v in LOOP_KINDS.items():
            self.cb_loop.addItem(v, k)
        self.sp_vel_kp = dspin(0, 1e4, 2.5, 5, 0.01)
        self.sp_vel_ki = dspin(0, 1e5, 0.0, 5, 0.01)
        self.sp_vel_imax = dspin(0, 1000, 20.0, 3, 0.1)
        self.sp_vel_omax = dspin(0, 1000, 20.0, 3, 0.1)
        self.sp_single_pos_kp = dspin(0, 1e4, 1.0, 5, 0.01)
        self.sp_single_pos_ki = dspin(0, 1e5, 0.0, 5, 0.01)
        self.sp_single_pos_kd = dspin(0, 1e4, 0.0, 5, 0.001)
        self.sp_single_pos_imax = dspin(0, 1000, 0.0, 3, 0.1)
        self.sp_single_pos_omax = dspin(0, 1000, 20.0, 3, 0.1)
        self.sp_single_pos_d_fc = dspin(0, 1000, 0.0, 2, 1.0, " Hz")
        self.sp_kp = dspin(0, 1e4, 2.5, 5, 0.01)
        self.sp_ki = dspin(0, 1e5, 0.0, 5, 0.01)
        self.sp_kd = dspin(0, 1e4, 0.0, 5, 0.001)
        self.sp_imax = dspin(0, 1000, 20.0, 3, 0.1)
        self.sp_ff = dspin(-1000, 1000, 0.0, 5, 0.01)
        self.sp_err_fc = dspin(0, 1000, 0.0, 2, 1.0, " Hz")
        self.sp_d_fc = dspin(0, 1000, 0.0, 2, 1.0, " Hz")
        self.sp_pos_kp = dspin(0, 1e4, 1.0, 5, 0.01)
        self.sp_pos_ki = dspin(0, 1e5, 0.0, 5, 0.01)
        self.sp_pos_kd = dspin(0, 1e4, 0.0, 5, 0.001)
        self.sp_pos_ff = dspin(-1000, 1000, 0.0, 5, 0.01)
        self.sp_pos_imax = dspin(0, 1000, 0.0, 3, 0.1)
        self.sp_pos_omax = dspin(0, 1000, 50.0, 3, 0.1)
        self.sp_pos_err_fc = dspin(0, 1000, 0.0, 2, 1.0, " Hz")
        self.sp_pos_d_fc = dspin(0, 1000, 0.0, 2, 1.0, " Hz")
        self.sp_step_count = QSpinBox()
        self.sp_step_count.setRange(1, 16)
        self.sp_step_count.setValue(1)
        self.step_fields = []
        self.cb_ref = SelectedFirstComboBox()
        for k, v in REF_KINDS.items():
            self.cb_ref.addItem(v, k)
        self.sp_amp = dspin(-1000, 1000, 2.0, 3, 0.5)
        self.sp_freq = dspin(0.01, 50, 1.0, 2, 0.5, " Hz")
        self.sp_ramp = dspin(0.01, 30, 1.0, 2, 0.1, " s")
        self.sp_dur = dspin(0.2, 60, 2.0, 2, 0.5, " s")
        self.sp_omax = dspin(0, 1000, 20.0, 3, 0.1)
        self.sp_omax.setToolTip("控制器输出限幅；修改后会同步提高左侧对应安全限幅")
        self.chk_rel = CheckBox("位置参考相对起始位置")
        self.chk_rel.setChecked(True)

        for w in (self.sp_vel_omax, self.sp_omax):
            w.setPrefix("±")
        for w in (self.sp_vel_imax, self.sp_imax):
            w.setPrefix("±")

        g = QGroupBox("控制器")
        controller_layout = QVBoxLayout(g)
        loop_form = QFormLayout()
        loop_form.addRow("回路", self.cb_loop)
        controller_layout.addLayout(loop_form)

        self.single_controller = QWidget()
        single_form = QFormLayout(self.single_controller)
        single_form.setContentsMargins(0, 0, 0, 0)
        single_form.addRow("Kp", self.sp_vel_kp)
        single_form.addRow("Ki", self.sp_vel_ki)
        single_form.addRow("积分项限幅", self.sp_vel_imax)
        single_form.addRow("输出限幅", self.sp_vel_omax)
        controller_layout.addWidget(self.single_controller)

        self.single_position_controller = QWidget()
        single_position_form = QFormLayout(self.single_position_controller)
        single_position_form.setContentsMargins(0, 0, 0, 0)
        single_position_form.addRow("Kp", self.sp_single_pos_kp)
        single_position_form.addRow("Ki", self.sp_single_pos_ki)
        single_position_form.addRow("Kd", self.sp_single_pos_kd)
        single_position_form.addRow("积分项限幅", self.sp_single_pos_imax)
        single_position_form.addRow("输出限幅", self.sp_single_pos_omax)
        single_position_form.addRow("微分低通", self.sp_single_pos_d_fc)
        controller_layout.addWidget(self.single_position_controller)

        self.cascade_controller = QWidget()
        cascade_layout = QVBoxLayout(self.cascade_controller)
        cascade_layout.setContentsMargins(0, 0, 0, 0)
        inner = QGroupBox("内环：速度环")
        inner_form = QFormLayout(inner)
        inner_form.addRow("Kp", self.sp_kp)
        inner_form.addRow("Ki", self.sp_ki)
        inner_form.addRow("Kd", self.sp_kd)
        inner_form.addRow("前馈", self.sp_ff)
        inner_form.addRow("积分项限幅", self.sp_imax)
        inner_form.addRow("输出限幅", self.sp_omax)
        inner_form.addRow("误差低通", self.sp_err_fc)
        inner_form.addRow("微分低通", self.sp_d_fc)
        outer = QGroupBox("外环：位置环")
        outer_form = QFormLayout(outer)
        outer_form.addRow("Kp", self.sp_pos_kp)
        outer_form.addRow("Ki", self.sp_pos_ki)
        outer_form.addRow("Kd", self.sp_pos_kd)
        outer_form.addRow("前馈", self.sp_pos_ff)
        outer_form.addRow("积分项限幅", self.sp_pos_imax)
        outer_form.addRow("输出限幅", self.sp_pos_omax)
        outer_form.addRow("误差低通", self.sp_pos_err_fc)
        outer_form.addRow("微分低通", self.sp_pos_d_fc)
        cascade_layout.addWidget(outer)
        cascade_layout.addWidget(inner)
        controller_layout.addWidget(self.cascade_controller)
        self.inner_group = inner
        self.outer_group = outer
        g2 = QGroupBox("参考信号")
        f2 = QFormLayout(g2)
        f2.addRow("类型", self.cb_ref)
        self.lbl_amp = QLabel("幅值")
        f2.addRow(self.lbl_amp, self.sp_amp)
        self.lbl_step_count = QLabel("阶跃次数")
        f2.addRow(self.lbl_step_count, self.sp_step_count)
        self.lbl_step = QLabel("阶跃序列")
        self.step_panel = QWidget()
        self.step_grid = QGridLayout(self.step_panel)
        self.step_grid.setContentsMargins(0, 0, 0, 0)
        self.step_grid.setHorizontalSpacing(8)
        self.step_grid.setColumnStretch(1, 3)
        self.step_grid.setColumnStretch(2, 2)
        self.step_grid.addWidget(QLabel("目标值"), 0, 1)
        self.step_grid.addWidget(QLabel("持续时间"), 0, 2)
        # The sequence needs both value columns at narrow widths. Keep its
        # heading and grid in a full-width row instead of the form's field
        # column, which would leave too little room for the units.
        step_section = QWidget()
        step_section_layout = QVBoxLayout(step_section)
        step_section_layout.setContentsMargins(0, 0, 0, 0)
        step_section_layout.setSpacing(2)
        step_section_layout.addWidget(self.lbl_step)
        step_section_layout.addWidget(self.step_panel)
        f2.addRow(step_section)
        self.step_section = step_section
        self.lbl_freq = QLabel("频率")
        self.lbl_ramp = QLabel("斜坡时间")
        self.lbl_dur = QLabel("时长")
        f2.addRow(self.lbl_freq, self.sp_freq)
        f2.addRow(self.lbl_ramp, self.sp_ramp)
        f2.addRow(self.lbl_dur, self.sp_dur)
        self._reference_form = f2
        f2.addRow(self.chk_rel)
        self.btn_start = QPushButton("开始闭环")
        self.btn_start.setStyleSheet("QPushButton{font-weight:bold;min-height:28px}")
        left = QVBoxLayout()
        for w in (g, g2):
            left.addWidget(w)
        left.addStretch(1)
        lw = QWidget()
        lw.setLayout(left)
        # The scroll area owns the 350 px pane minimum. The page itself must
        # follow the viewport so its controls can reflow at that width.
        lw.setMinimumWidth(0)
        lw.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground("w")
        self.p_y = self.glw.addPlot(row=0, col=0)
        self.p_u = self.glw.addPlot(row=1, col=0)
        self.glw.ci.layout.setRowStretchFactor(0, 1)
        self.glw.ci.layout.setRowStretchFactor(1, 1)
        self.p_y.addLegend(offset=(5, 5))
        for p in (self.p_y, self.p_u):
            p.showGrid(x=True, y=True, alpha=0.3)
            p.setLimits(xMin=0)
        self.p_u.setLabel("bottom", "时间", units="s")
        self.c_ref = self.p_y.plot(name="参考", pen=pg.mkPen("#c62828", width=2))
        self.c_y = self.p_y.plot(name="反馈", pen=pg.mkPen("#1565c0", width=2))
        output_colors = ("#c62828", "#00acc1", "#1565c0", "#6a1b9a", "#00897b")
        # The output curves are controlled by the checkboxes below the plot;
        # they must not also be registered in a pyqtgraph legend.
        self.c_u = self.p_u.plot(pen=pg.mkPen(output_colors[0], width=2))
        self.c_fb = self.p_u.plot(pen=pg.mkPen(output_colors[1], width=2))
        self.c_p = self.p_u.plot(pen=pg.mkPen(output_colors[2], width=2))
        self.c_i = self.p_u.plot(pen=pg.mkPen(output_colors[3], width=2))
        self.c_d = self.p_u.plot(pen=pg.mkPen(output_colors[4], width=2))
        self.chk_u = CheckBox("总输出")
        self.chk_fb = CheckBox("电机反馈")
        self.chk_p = CheckBox("P 输出")
        self.chk_i = CheckBox("I 输出")
        self.chk_d = CheckBox("D 输出")
        self.chk_u.setChecked(True)
        self.chk_fb.setChecked(True)
        self._output_checks = (self.chk_u, self.chk_fb, self.chk_p, self.chk_i, self.chk_d)
        self._output_items = []
        curve_bar = QHBoxLayout()
        curve_bar.addWidget(self.btn_start)
        curve_bar.addStretch(1)
        output_bar = QHBoxLayout()
        output_bar.setContentsMargins(8, 0, 8, 0)
        output_bar.addWidget(QLabel("输出曲线"))
        for color, checkbox in zip(output_colors, self._output_checks):
            swatch = QLabel()
            swatch.setFixedSize(14, 3)
            swatch.setStyleSheet(f"background:{color};")
            item = QWidget()
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(8, 0, 0, 0)
            item_layout.setSpacing(5)
            item_layout.addWidget(swatch)
            item_layout.addWidget(checkbox)
            output_bar.addWidget(item)
            self._output_items.append(item)
        output_bar.addStretch(1)
        output_controls = QWidget()
        output_controls.setMinimumHeight(30)
        output_controls.setLayout(output_bar)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(curve_bar)
        right_layout.addWidget(self.glw, 1)
        right_layout.addWidget(output_controls, 0)
        right_layout.addWidget(self.out, 0)
        self.out.setMaximumHeight(160)
        right = QWidget()
        right.setLayout(right_layout)
        left_scroll = QScrollArea()
        left_scroll.setObjectName("loopScroll")
        left_scroll.setWidget(lw)
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(350)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.columns = QSplitter(Qt.Horizontal)
        self.columns.setObjectName("loopSplitter")
        self.columns.addWidget(left_scroll)
        self.columns.addWidget(right)
        self.columns.setChildrenCollapsible(False)
        self.columns.setCollapsible(0, False)
        self.columns.setCollapsible(1, True)
        # The pane border itself is the visual divider and hit area.
        self.columns.setHandleWidth(6)
        self.columns.setStretchFactor(1, 1)
        self.columns.setSizes([350, 700])
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.columns)

        self.cb_loop.currentIndexChanged.connect(self._loop_changed)
        self.cb_ref.currentIndexChanged.connect(self._update)
        self.sp_step_count.valueChanged.connect(self._rebuild_step_fields)
        for w in (self.sp_vel_kp, self.sp_vel_ki, self.sp_vel_imax, self.sp_vel_omax,
                  self.sp_kp, self.sp_ki, self.sp_kd, self.sp_ff, self.sp_imax,
                  self.sp_err_fc, self.sp_d_fc, self.sp_pos_kp, self.sp_pos_ki,
                  self.sp_pos_kd, self.sp_pos_ff, self.sp_pos_imax, self.sp_pos_omax,
                  self.sp_pos_err_fc, self.sp_pos_d_fc, self.sp_amp,
                  self.sp_freq, self.sp_ramp, self.sp_dur):
            w.valueChanged.connect(self._update)
        self.chk_rel.toggled.connect(self._update)
        for checkbox in self._output_checks:
            checkbox.toggled.connect(self._update_output_visibility)
        self.btn_start.clicked.connect(self._toggle_run)
        self._rebuild_step_fields()
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
        """同步闭环测试曲线的背景、坐标轴和图例颜色。"""
        background = theme.current().plot_bg()
        foreground = theme.current().pal.text
        self.glw.setBackground(background)
        for plot in (self.p_y, self.p_u):
            for axis_name in ("left", "bottom", "right", "top"):
                axis = plot.getAxis(axis_name)
                axis.setPen(pg.mkPen(foreground))
                axis.setTextPen(pg.mkPen(foreground))
            legend = getattr(plot, "legend", None)
            set_legend_text_color(legend, foreground)

    def set_driver(self, d):
        self.driver = d
        self.btn_start.setEnabled(self.worker is not None and d is not None)
        self.btn_start.setText("开始闭环")
        if d is None:
            self.reset_for_no_driver()
        else:
            self._update()

    def reset_for_no_driver(self):
        """清除本次调试数据，并恢复闭环页首次打开时的参数。"""
        self.run = None
        self.sugg = None
        self.out.clear()
        controls = (
            (self.cb_loop, 0), (self.cb_ref, 0),
            (self.sp_vel_kp, 2.5), (self.sp_vel_ki, 0.0),
            (self.sp_vel_imax, 20.0), (self.sp_vel_omax, 20.0),
            (self.sp_single_pos_kp, 1.0), (self.sp_single_pos_ki, 0.0),
            (self.sp_single_pos_kd, 0.0), (self.sp_single_pos_imax, 0.0),
            (self.sp_single_pos_omax, 20.0), (self.sp_single_pos_d_fc, 0.0),
            (self.sp_kp, 2.5), (self.sp_ki, 0.0), (self.sp_kd, 0.0),
            (self.sp_ff, 0.0), (self.sp_imax, 20.0), (self.sp_omax, 20.0),
            (self.sp_err_fc, 0.0), (self.sp_d_fc, 0.0),
            (self.sp_pos_kp, 1.0), (self.sp_pos_ki, 0.0),
            (self.sp_pos_kd, 0.0), (self.sp_pos_ff, 0.0),
            (self.sp_pos_imax, 0.0), (self.sp_pos_omax, 50.0),
            (self.sp_pos_err_fc, 0.0), (self.sp_pos_d_fc, 0.0),
            (self.sp_amp, 2.0), (self.sp_freq, 1.0),
            (self.sp_ramp, 1.0), (self.sp_dur, 2.0),
        )
        for widget, value in controls:
            widget.blockSignals(True)
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(value)
            else:
                widget.setValue(value)
            widget.blockSignals(False)
        self.chk_rel.blockSignals(True)
        self.chk_rel.setChecked(True)
        self.chk_rel.blockSignals(False)
        for checkbox in self._output_checks:
            checkbox.blockSignals(True)
            checkbox.setChecked(checkbox is self.chk_u)
            checkbox.blockSignals(False)
        self.sp_step_count.blockSignals(True)
        self.sp_step_count.setValue(1)
        self.sp_step_count.blockSignals(False)
        self._rebuild_step_fields()
        if self.step_fields:
            value, duration = self.step_fields[0]
            value.setValue(2.0)
            duration.setValue(1.0)
        self._update()

    def _loop_changed(self):
        if self.cb_loop.currentData() == "pos":
            self._copy_velocity_to_inner()
        else:
            self._copy_inner_to_velocity()
        self._update()

    def _copy_velocity_to_inner(self):
        for source, target in ((self.sp_vel_kp, self.sp_kp), (self.sp_vel_ki, self.sp_ki),
                               (self.sp_vel_imax, self.sp_imax), (self.sp_vel_omax, self.sp_omax)):
            target.blockSignals(True)
            target.setValue(source.value())
            target.blockSignals(False)

    def _copy_inner_to_velocity(self):
        for source, target in ((self.sp_kp, self.sp_vel_kp), (self.sp_ki, self.sp_vel_ki),
                               (self.sp_imax, self.sp_vel_imax), (self.sp_omax, self.sp_vel_omax)):
            target.blockSignals(True)
            target.setValue(source.value())
            target.blockSignals(False)

    def on_state(self, s: dict):
        if self.driver is None:
            return
        running = s["running"]
        self.btn_start.setText("停止" if running else "开始闭环")
        self.btn_start.setEnabled(running or (s["active"] and not s["tripped"]))

    def _toggle_run(self):
        if self.run is not None and not self.run.done:
            if self.worker is not None:
                self.worker.stop_run()
            self.btn_start.setText("开始闭环")
            return
        self.start()

    def _units(self):
        u = self.driver.out_unit if self.driver else ""
        yu = "rad/s" if self.cb_loop.currentData() == "vel" else "rad"
        return u, yu, f"{u}/(rad/s)", f"{u}/rad", f"{u}/(rad/s)"

    def _update(self):
        self._clear_measured_curves()
        u, yu, kpu, kiu, kdu = self._units()
        loop = self.cb_loop.currentData()
        is_vel = loop == "vel"
        is_cascade = loop == "pos"
        is_single_position = loop == "pos_single"
        self.single_controller.setVisible(is_vel)
        self.cascade_controller.setVisible(is_cascade)
        self.single_position_controller.setVisible(is_single_position)
        self.chk_fb.setText(f"电机反馈{self.driver.out_name}" if self.driver else "电机反馈")
        self._output_items[4].setVisible(not is_vel)
        if is_vel:
            self.chk_d.setChecked(False)
        for w in (self.sp_vel_kp, self.sp_kp):
            w.setSuffix(f" {kpu}")
        for w in (self.sp_vel_ki, self.sp_ki):
            w.setSuffix(f" {kiu}")
        self.sp_kd.setSuffix(f" {kdu}")
        self.sp_ff.setSuffix(f" {u}")
        self.sp_imax.setSuffix(f" {u}")
        self.sp_vel_imax.setSuffix(f" {u}")
        self.sp_vel_omax.setSuffix(f" {u}")
        self.sp_pos_kp.setSuffix(" (rad/s)/rad")
        self.sp_pos_ki.setSuffix(" (rad/s)/(rad·s)")
        self.sp_pos_kd.setSuffix(" (rad/s)/(rad/s)")
        self.sp_pos_ff.setSuffix(" rad/s")
        self.sp_pos_imax.setSuffix(" rad/s")
        self.sp_pos_omax.setSuffix(" rad/s")
        self.sp_amp.setSuffix(f" {yu}")
        self.sp_omax.setSuffix(f" {u}")
        self.sp_single_pos_kp.setSuffix(f" {u}/rad")
        self.sp_single_pos_ki.setSuffix(f" {u}/(rad·s)")
        self.sp_single_pos_kd.setSuffix(f" {u}/(rad/s)")
        self.sp_single_pos_imax.setSuffix(f" {u}")
        self.sp_single_pos_omax.setSuffix(f" {u}")
        self._set_row_visible(self._reference_form, self.chk_rel, self.chk_rel,
                              is_cascade or is_single_position)
        k = self.cb_ref.currentData()
        self._set_row_visible(self._reference_form, self.lbl_amp, self.sp_amp, k != "step")
        self._set_row_visible(self._reference_form, self.lbl_step_count, self.sp_step_count, k == "step")
        self.step_section.setVisible(k == "step")
        self._set_row_visible(self._reference_form, self.lbl_freq, self.sp_freq, k == "sine")
        self._set_row_visible(self._reference_form, self.lbl_ramp, self.sp_ramp, k == "ramp")
        self._set_row_visible(self._reference_form, self.lbl_dur, self.sp_dur,
                              k not in ("ramp", "step"))
        for value, duration in self.step_fields:
            value.setSuffix(f" {yu}")
            duration.setSuffix(" s")
        self.p_y.setLabel("left", "速度" if is_vel else "位置", units=yu)
        self.p_u.setLabel("left", "输出", units=u)
        self._update_reference_preview()
        self._request_limit_sync()
        self._update_output_visibility()

    def _update_output_visibility(self):
        if not hasattr(self, "c_u"):
            return
        for curve, checkbox in ((self.c_u, self.chk_u), (self.c_fb, self.chk_fb),
                                (self.c_p, self.chk_p),
                                (self.c_i, self.chk_i), (self.c_d, self.chk_d)):
            curve.setVisible(checkbox.isChecked())

    def _clear_measured_curves(self):
        """参数变化后只保留新的参考信号预览，移除上一轮反馈/输出数据。"""
        if hasattr(self, "c_y"):
            self.c_y.setData([], [])
            for curve in (self.c_u, self.c_fb, self.c_p, self.c_i, self.c_d):
                curve.setData([], [])

    def _request_limit_sync(self):
        """通知主窗口把测试所需的速度/输出范围同步到左侧。"""
        ref_kind = self.cb_ref.currentData()
        if ref_kind == "step":
            speed_max = max((abs(value.value()) for value, _ in self.step_fields), default=0.0)
        else:
            speed_max = abs(self.sp_amp.value())
        loop = self.cb_loop.currentData()
        if loop == "vel":
            required_vel = speed_max
            required_out = max(abs(self.sp_vel_imax.value()), abs(self.sp_vel_omax.value()))
        elif loop == "pos":
            required_vel = max(abs(self.sp_pos_imax.value()), abs(self.sp_pos_omax.value()))
            required_out = max(abs(self.sp_imax.value()), abs(self.sp_omax.value()))
        else:
            required_vel = 0.0
            required_out = max(abs(self.sp_single_pos_imax.value()),
                               abs(self.sp_single_pos_omax.value()))
        self.limits_requested.emit(required_vel, required_out)

    def _preview_config(self) -> LoopConfig:
        ref_kind = self.cb_ref.currentData()
        step_values = [value.value() for value, _ in self.step_fields]
        step_durations = [duration.value() for _, duration in self.step_fields]
        duration = (sum(step_durations) if ref_kind == "step"
                    else self.sp_ramp.value() if ref_kind == "ramp" else self.sp_dur.value())
        loop = self.cb_loop.currentData()
        if loop == "vel":
            kp, ki, kd = self.sp_vel_kp.value(), self.sp_vel_ki.value(), 0.0
            ff = 0.0
            integral_max, out_max = self.sp_vel_imax.value(), self.sp_vel_omax.value()
            err_fc, d_fc = 0.0, 0.0
            pos_params = {}
        elif loop == "pos_single":
            kp, ki, kd = (self.sp_single_pos_kp.value(), self.sp_single_pos_ki.value(),
                          self.sp_single_pos_kd.value())
            ff = 0.0
            integral_max, out_max = (self.sp_single_pos_imax.value(),
                                     self.sp_single_pos_omax.value())
            err_fc, d_fc = 0.0, self.sp_single_pos_d_fc.value()
            pos_params = dict(pos_kp=None, pos_ki=None, pos_kd=None, pos_ff=0.0,
                              pos_integral_max=0.0, pos_out_max=50.0,
                              pos_err_fc=0.0, pos_d_fc=0.0)
        else:
            kp, ki, kd = self.sp_kp.value(), self.sp_ki.value(), self.sp_kd.value()
            ff = self.sp_ff.value()
            integral_max, out_max = self.sp_imax.value(), self.sp_omax.value()
            err_fc, d_fc = self.sp_err_fc.value(), self.sp_d_fc.value()
            pos_params = dict(pos_kp=self.sp_pos_kp.value(), pos_ki=self.sp_pos_ki.value(),
                              pos_kd=self.sp_pos_kd.value(), pos_ff=self.sp_pos_ff.value(),
                              pos_integral_max=self.sp_pos_imax.value(),
                              pos_out_max=self.sp_pos_omax.value(),
                              pos_err_fc=self.sp_pos_err_fc.value(),
                              pos_d_fc=self.sp_pos_d_fc.value())
        return LoopConfig(
            loop=loop,
            kp=kp, ki=ki, kd=kd, ff=ff, integral_max=integral_max, out_max=out_max,
            err_fc=err_fc, d_fc=d_fc,
            ref_kind=ref_kind,
            amp=self.sp_amp.value(),
            freq=self.sp_freq.value(),
            ramp_time=self.sp_ramp.value(),
            duration=duration,
            relative=self.chk_rel.isChecked(),
            **pos_params,
            step_values=step_values,
            step_durations=step_durations,
        )

    def _update_reference_preview(self, pos0: float = 0.0):
        if not hasattr(self, "p_y") or not self.step_fields:
            return
        cfg = self._preview_config()
        run = LoopRun(cfg)
        run.start(0.0, pos0, 0.0)
        total = max(run.total, 1e-3)
        count = max(200, min(2000, int(total * 200) + 1))
        t = np.linspace(0.0, total, count)
        ref = np.asarray([run.reference(float(x)) for x in t])
        self.c_ref.setData(t, ref, connect="finite")
        for plot in (self.p_y, self.p_u):
            plot.setXRange(0.0, total, padding=0)
        self._set_plot_y_range(self.p_y, ref)
        loop = self.cb_loop.currentData()
        output_limit = (
            self.sp_vel_omax.value() if loop == "vel" else
            self.sp_omax.value() if loop == "pos" else
            self.sp_single_pos_omax.value()
        )
        self._set_plot_y_range(self.p_u, np.array([]), fallback=output_limit)

    @staticmethod
    def _set_plot_y_range(plot, values, fallback=1.0):
        values = np.asarray(values)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            limit = max(abs(float(fallback)), 1.0)
            plot.setYRange(-limit, limit, padding=0)
            return
        low, high = float(np.min(finite)), float(np.max(finite))
        span = max(high - low, abs(low) * 0.1, abs(high) * 0.1, 1e-3)
        pad = max(span * 0.12, 1e-3)
        if high == low:
            pad = max(abs(high) * 0.2, 1.0)
        plot.setYRange(low - pad, high + pad, padding=0)

    def _update_live_plot_ranges(self, t, ref, y, u, fb, p, i, d, total):
        """同步时间窗口，并按窗口内的数据重新适配两张图的 Y 轴。"""
        if len(t) == 0:
            return
        window = min(10.0, max(float(total), 1e-3))
        right = max(float(t[-1]), window)
        left = max(0.0, right - window)
        for plot in (self.p_y, self.p_u):
            plot.setXRange(left, right, padding=0)
        visible = (t >= left) & (t <= right)
        self._set_plot_y_range(self.p_y, np.concatenate((ref[visible], y[visible])))
        self._set_plot_y_range(
            self.p_u,
            np.concatenate((u[visible], fb[visible], p[visible], i[visible], d[visible])),
            fallback=(self.sp_vel_omax.value() if self.cb_loop.currentData() == "vel" else
                      self.sp_omax.value() if self.cb_loop.currentData() == "pos" else
                      self.sp_single_pos_omax.value()),
        )

    def _rebuild_step_fields(self):
        old_values = [value.value() for value, _ in self.step_fields]
        old_durations = [duration.value() for _, duration in self.step_fields]
        while self.step_grid.count() > 2:
            item = self.step_grid.takeAt(2)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.step_fields = []
        for i in range(self.sp_step_count.value()):
            value = dspin(-1000, 1000, old_values[i] if i < len(old_values) else 2.0, 3, 0.5)
            duration = dspin(0.01, 60, old_durations[i] if i < len(old_durations) else 1.0, 2, 0.1, " s")
            for field in (value, duration):
                field.setMinimumWidth(0)
                field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            value.valueChanged.connect(self._update)
            duration.valueChanged.connect(self._update)
            self.step_fields.append((value, duration))
            # A compact row number keeps both value columns readable at the
            # pane minimum while the section heading already identifies the
            # rows as the step sequence.
            self.step_grid.addWidget(QLabel(f"{i + 1}."), i + 1, 0)
            self.step_grid.addWidget(value, i + 1, 1)
            self.step_grid.addWidget(duration, i + 1, 2)
        self._update()

    @staticmethod
    def _set_row_visible(form, label, field, visible):
        if hasattr(form, "setRowVisible"):
            form.setRowVisible(label, visible)
        else:
            label.setVisible(visible)
            field.setVisible(visible)

    def set_suggestion(self, s: sysid.Suggestion):
        self.sugg = s
        scale = max(0.0, min(1.0, getattr(s, "start_ratio", 1.0)))
        # 同时填入内环速度 PI 和串级位置外环 PID，按响应测试页设定的比例起步。
        self.sp_vel_kp.setValue(s.vel_kp * scale)
        self.sp_vel_ki.setValue(s.vel_ki * scale)
        self.sp_kp.setValue(s.vel_kp * scale)
        self.sp_ki.setValue(s.vel_ki * scale)
        self.sp_kd.setValue(0.0)
        self.sp_ff.setValue(0.0)
        self.sp_pos_kp.setValue(s.pos_kp * scale)
        self.sp_pos_ki.setValue(s.pos_ki * scale)
        self.sp_pos_kd.setValue(s.pos_kd * scale)
        self.sp_pos_ff.setValue(0.0)
        self.sp_single_pos_kp.setValue(s.single_pos_kp * scale)
        self.sp_single_pos_ki.setValue(s.single_pos_ki * scale)
        self.sp_single_pos_kd.setValue(s.single_pos_kd * scale)
        if self.cb_loop.currentData() == "vel":
            self._copy_velocity_to_inner()

    def start(self):
        w = self.worker
        if not w or not self.driver:
            return
        if not w.active:
            QMessageBox.information(self, "闭环测试", "请先使能电机")
            return
        self._request_limit_sync()
        loop = self.cb_loop.currentData()
        ref_kind = self.cb_ref.currentData()
        step_values = [value.value() for value, _ in self.step_fields]
        step_durations = [duration.value() for _, duration in self.step_fields]
        amp = self.sp_amp.value() if ref_kind != "step" else 0.0
        duration = (sum(step_durations) if ref_kind == "step"
                    else self.sp_ramp.value() if ref_kind == "ramp" else self.sp_dur.value())
        if loop == "vel":
            kp, ki = self.sp_vel_kp.value(), self.sp_vel_ki.value()
            kd, ff = 0.0, 0.0
            integral_max, out_max = self.sp_vel_imax.value(), self.sp_vel_omax.value()
            err_fc, d_fc = 0.0, 0.0
        elif loop == "pos":
            kp, ki, kd = self.sp_kp.value(), self.sp_ki.value(), self.sp_kd.value()
            ff = self.sp_ff.value()
            integral_max, out_max = self.sp_imax.value(), self.sp_omax.value()
            err_fc, d_fc = self.sp_err_fc.value(), self.sp_d_fc.value()
        else:
            kp, ki, kd = (self.sp_single_pos_kp.value(), self.sp_single_pos_ki.value(),
                          self.sp_single_pos_kd.value())
            ff = 0.0
            integral_max, out_max = (self.sp_single_pos_imax.value(),
                                     self.sp_single_pos_omax.value())
            err_fc, d_fc = 0.0, self.sp_single_pos_d_fc.value()
        cfg = LoopConfig(loop=loop, kp=kp, ki=ki, kd=kd, ff=ff,
                         ref_kind=ref_kind, amp=amp, freq=self.sp_freq.value(),
                         ramp_time=self.sp_ramp.value(), duration=duration,
                         out_max=out_max, integral_max=integral_max,
                         err_fc=err_fc, d_fc=d_fc,
                         pos_kp=(self.sp_pos_kp.value() if loop == "pos" else None),
                         pos_ki=(self.sp_pos_ki.value() if loop == "pos" else None),
                         pos_kd=(self.sp_pos_kd.value() if loop == "pos" else None),
                         pos_ff=(self.sp_pos_ff.value() if loop == "pos" else 0.0),
                         pos_integral_max=(self.sp_pos_imax.value() if loop == "pos" else 0.0),
                         pos_out_max=(self.sp_pos_omax.value() if loop == "pos" else 50.0),
                         pos_err_fc=(self.sp_pos_err_fc.value() if loop == "pos" else 0.0),
                         pos_d_fc=(self.sp_pos_d_fc.value() if loop == "pos" else 0.0),
                         relative=self.chk_rel.isChecked(), step_values=step_values,
                         step_durations=step_durations)
        self.run = LoopRun(cfg)
        self._live_t.clear()
        self._live_ref.clear()
        self._live_y.clear()
        self._live_u.clear()
        self._live_fb.clear()
        self._live_p.clear()
        self._live_i.clear()
        self._live_d.clear()
        self.out.clear()
        for c in (self.c_y, self.c_u, self.c_fb, self.c_p, self.c_i, self.c_d):
            c.setData([], [])
        self._update_reference_preview(float(getattr(w.safety, "pos", 0.0)))
        w.start_run(self.run)
        self.btn_start.setText("停止")

    def on_samples(self, samples):
        """实时绘制闭环运行中的参考、反馈和输出。"""
        run = self.run
        if run is None or run.done or not samples:
            return
        for sample in samples:
            self._live_t.append(max(0.0, sample.t - run.t0))
            self._live_ref.append(sample.ref)
            self._live_y.append(sample.fb.vel if run.cfg.loop == "vel" else sample.pos_mt)
            self._live_u.append(sample.out)
            self._live_fb.append(sample.fb.out)
            p, i, d = sample.pid_components or (np.nan, np.nan, np.nan)
            self._live_p.append(p)
            self._live_i.append(i)
            self._live_d.append(d)
        t = np.asarray(self._live_t)
        ref = np.asarray(self._live_ref)
        y = np.asarray(self._live_y)
        u = np.asarray(self._live_u)
        fb = np.asarray(self._live_fb)
        p = np.asarray(self._live_p)
        i = np.asarray(self._live_i)
        d = np.asarray(self._live_d)
        self.c_y.setData(t, y, connect="finite")
        self.c_u.setData(t, u, connect="finite")
        self.c_fb.setData(t, fb, connect="finite")
        self.c_p.setData(t, p, connect="finite")
        self.c_i.setData(t, i, connect="finite")
        self.c_d.setData(t, d, connect="finite")
        if len(t) > 1:
            self._update_live_plot_ranges(t, ref, y, u, fb, p, i, d, run.total)

    def on_run_finished(self, run):
        if run is not self.run:
            return
        r = run.rec
        if len(r) < 5:
            return
        t = np.maximum(0.0, np.asarray(r.t))
        y = np.asarray(r.vel if run.cfg.loop == "vel" else r.pos)
        self.c_ref.setData(t, np.asarray(r.ref), connect="finite")
        self.c_y.setData(t, y)
        self.c_u.setData(t, np.asarray(r.u), connect="finite")
        self.c_fb.setData(t, np.asarray(r.fb_out), connect="finite")
        self.c_p.setData(t, np.asarray(r.p_out), connect="finite")
        self.c_i.setData(t, np.asarray(r.i_out), connect="finite")
        self.c_d.setData(t, np.asarray(r.d_out), connect="finite")
        self._update_live_plot_ranges(
            t, np.asarray(r.ref), y, np.asarray(r.u), np.asarray(r.fb_out),
            np.asarray(r.p_out), np.asarray(r.i_out), np.asarray(r.d_out), run.total
        )
        st = sysid.loop_stats(r, run.cfg.loop)
        if st:
            unit = "rad/s" if run.cfg.loop == "vel" else "rad"
            self.out.appendPlainText(st.text(unit))
