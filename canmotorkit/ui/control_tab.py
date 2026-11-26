"""控制页：电流/扭矩（主页）+ MIT（子页，仅 MIT 电机）+ 实时反馈。

约定：参数默认全 0；修改后必须点“应用”或回车才下发；单位按厂家协议。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QSlider, QTabWidget, QToolButton, QVBoxLayout,
                               QWidget, QSizePolicy)

from canmotorkit.drivers import dm_protocol as P
from .gd_panel import GdControl



def _spin_style(dark: bool) -> str:
    """让目标值框使用与置零按钮一致的底色（跟随主题），并保留禁用态对比度。"""
    from .theme import current
    p = current().pal
    return (f"QDoubleSpinBox{{background:{p.button};color:{p.text};}}"
            f"QDoubleSpinBox:disabled{{background:{p.disabled_bg};color:{p.text_disabled};}}")


class _Row:
    """一个参数：滑条 + 数值框 + 置零按钮。只修改界面值，不下发。"""
    STEPS = 2000

    def __init__(self, grid: QGridLayout, row: int, label: str, unit: str, decimals: int,
                 step: float, on_edit, on_enter, place_spin=True):
        self.lo, self.hi = -1.0, 1.0
        self.applied = 0.0
        self.on_edit = on_edit
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self.STEPS)
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(step)
        self.spin.setKeyboardTracking(True)
        # 单位直接显示在数值框内部，避免一行被拆成多个灰色小框。
        # 数值框保持紧凑，窄栏时仍优先压缩滑条。
        self.spin.setFixedWidth(96)
        self.spin.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.spin.setStyleSheet(_spin_style(True))
        self.slider.setMinimumWidth(0)
        self.spin.setSuffix(f" {unit}" if unit else "")
        self.btn0 = QToolButton()
        self.btn0.setText("0")
        self.btn0.setFocusPolicy(Qt.NoFocus)
        self.btn0.setFixedWidth(32)
        title = QLabel(label)
        # 所有 MIT 参数共用这一套紧凑行布局，避免某个型号单独撑宽。
        title.setFixedWidth(60)
        grid.addWidget(title, row, 0)
        grid.addWidget(self.slider, row, 1)
        if place_spin:
            grid.addWidget(self.spin, row, 2)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        self.spin.lineEdit().returnPressed.connect(on_enter)

    def set_range(self, lo: float, hi: float):
        v = self.value()
        self.lo, self.hi = lo, hi
        self.spin.blockSignals(True)
        self.spin.setRange(lo, hi)
        self.spin.blockSignals(False)
        self.set_value(v)

    def value(self) -> float:
        return self.spin.value()

    def set_value(self, v: float):
        v = P.clamp(v, self.lo, self.hi)
        self.spin.blockSignals(True)
        self.slider.blockSignals(True)
        self.spin.setValue(v)
        self.slider.setValue(self._to_slider(v))
        self.spin.blockSignals(False)
        self.slider.blockSignals(False)
        self._mark()

    def mark_applied(self):
        self.applied = self.value()
        self._mark()

    def pending(self) -> bool:
        return abs(self.value() - self.applied) > 1e-12

    def _mark(self):
        self.on_edit()

    def apply_theme(self, dark: bool):
        self.spin.setStyleSheet(_spin_style(dark))

    def _to_slider(self, v: float) -> int:
        span = self.hi - self.lo
        return int(round((v - self.lo) / span * self.STEPS)) if span > 0 else 0

    def _on_slider(self, i: int):
        v = self.lo + (self.hi - self.lo) * i / self.STEPS
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self._mark()

    def _on_spin(self, v: float):
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(v))
        self.slider.blockSignals(False)
        self._mark()


class _Params(QWidget):
    """一组参数行 + 应用/置零。"""

    def __init__(self, specs, on_apply):
        super().__init__()
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.on_apply = on_apply
        self._bound = False
        self._output_enabled = False
        self.rows = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setColumnMinimumWidth(0, 60)
        grid.setColumnMinimumWidth(2, 96)
        grid.setColumnMinimumWidth(3, 32)
        grid.setColumnStretch(1, 1)
        single_output = len(specs) == 1
        for i, (k, label, unit, dec, step) in enumerate(specs):
            self.rows[k] = _Row(grid, i, label, unit, dec, step, self._edited, self.apply,
                                place_spin=not single_output)
        for row in self.rows.values():
            row.btn0.clicked.connect(lambda _checked=False, target=row: self._zero_row(target))
        n = len(specs)
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setStyleSheet("QPushButton:enabled{font-weight:bold}")
        self.btn_apply.setFocusPolicy(Qt.NoFocus)
        self.btn_apply.setFixedHeight(28)
        r = QHBoxLayout()
        r.addWidget(self.btn_apply)
        if single_output:
            # 单输出页的置零操作与应用放在同一行，避免挤压输出数值框。
            output_row = next(iter(self.rows.values()))
            output_row.spin.setFixedWidth(96)
            output_row.spin.setFixedHeight(28)
            output_row.spin.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            output_row.btn0.setFixedHeight(28)
            r.addWidget(output_row.spin)
            r.addWidget(output_row.btn0)
        else:
            for row_index, row in enumerate(self.rows.values()):
                grid.addWidget(row.btn0, row_index, 3)
        r.addStretch(1)
        grid.addLayout(r, n, 0, 1, 4 if not single_output else 3)
        self.btn_apply.clicked.connect(self.apply)
        self._edited()

    def applied(self) -> dict:
        return {k: r.applied for k, r in self.rows.items()}

    def apply(self):
        if not self._bound or not self._output_enabled:
            return
        for r in self.rows.values():
            r.mark_applied()
        self.on_apply(self.applied())

    def _zero_row(self, row: _Row):
        """把指定参数置零，并在输出中时立即下发零指令。"""
        row.set_value(0.0)
        if self._bound and self._output_enabled:
            self.apply()

    def reset_zero(self):
        for r in self.rows.values():
            r.set_value(0.0)
            r.mark_applied()

    def set_enabled(self, enabled: bool):
        """未绑定电机时锁定整组输出控件，避免修改无效参数。"""
        self._bound = bool(enabled)
        self._output_enabled = False
        for row in self.rows.values():
            row.slider.setEnabled(enabled)
            row.spin.setEnabled(enabled)
            row.btn0.setEnabled(enabled and self._output_enabled)
        self._edited()

    def set_output_enabled(self, enabled: bool):
        """只有电机已使能/开始输出时才允许下发参数。"""
        self._output_enabled = bool(enabled)
        for row in self.rows.values():
            row.btn0.setEnabled(self._bound and self._output_enabled)
        self._edited()

    def _edited(self):
        if not hasattr(self, "btn_apply"):
            return
        self.btn_apply.setEnabled(self._bound and self._output_enabled)

    def apply_theme(self, dark: bool):
        for row in self.rows.values():
            row.apply_theme(dark)


class ControlPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.worker = None
        self.driver = None
        self._safety_out_max = None
        self._dark = True
        self._lim_warning = False
        self._code_fault = False
        self._active = False

        # 电流/扭矩主页
        self.cur = _Params([("out", "输出", "", 3, 0.05)], self._apply_cur)
        pc = QWidget()
        pc.setMinimumWidth(0)
        pc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lc = QVBoxLayout(pc)
        lc.addWidget(self.cur)
        lc.addStretch(1)

        # MIT 子页
        self.mit = _Params([("p", "位置 p", "rad", 4, 0.01), ("v", "速度 v", "rad/s", 3, 0.1),
                            ("kp", "Kp", "", 2, 1.0), ("kd", "Kd", "", 3, 0.05),
                            ("t", "前馈", "Nm", 3, 0.1)], self._apply_mit)
        self.lbl_lim = QLabel("—")
        self.lbl_lim.setWordWrap(True)
        pm = QWidget()
        pm.setMinimumWidth(0)
        pm.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lm = QVBoxLayout(pm)
        lm.addWidget(self.mit)
        f = QFormLayout()
        f.addRow("当前量程", self.lbl_lim)
        lm.addLayout(f)
        lm.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.addTab(pc, "电流 / 扭矩")
        self.tabs.addTab(pm, "MIT")
        self.gd = GdControl()
        self.tabs.addTab(self.gd, "GD 控制")
        self.tabs.setTabVisible(2, False)
        self.tabs.currentChanged.connect(self._tab_changed)

        rg = QGroupBox("实时反馈")
        rg.setMinimumWidth(0)
        rg.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        rf = QFormLayout(rg)
        rf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        mono = "font-family:Consolas,monospace;font-size:13px"
        self.lbl = {}
        self._feedback_rows = {}
        for k, name in (("pos_raw", "原始位置"), ("pos_mt", "累计位置"),
                        ("vel", "速度"), ("out", "输出（指令 / 反馈）"),
                        ("tmp", "温度"), ("code", "状态码")):
            title = QLabel(name)
            lb = QLabel("—")
            lb.setMinimumWidth(0)
            lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lb.setStyleSheet(mono)
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.lbl[k] = lb
            self._feedback_rows[k] = (title, lb)
            rf.addRow(title, lb)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addWidget(rg)
        lay.addStretch(1)
        self.set_bound(None)

    # ---------- 外部 ----------
    def set_bound(self, worker):
        self.worker = worker
        self.set_driver(None)

    def apply_theme(self, dark: bool):
        self._dark = bool(dark)
        self.cur.apply_theme(self._dark)
        self.mit.apply_theme(self._dark)
        self._refresh_limit_style()
        self._refresh_code_style()

    def set_driver(self, d):
        self.driver = d
        self._safety_out_max = None
        self._active = False
        self.gd.bind(self.worker, d)
        self.tabs.setTabVisible(2, d is not None and d.protocol == "gd")
        self.tabs.setCurrentIndex(0)
        self.cur.reset_zero()
        self.mit.reset_zero()
        has_mit = d is not None and d.caps.mit
        bound = d is not None and self.worker is not None
        self.cur.set_enabled(bound)
        self.mit.set_enabled(bound and has_mit)
        self.tabs.setTabEnabled(1, has_mit)
        self.tabs.setTabToolTip(1, "" if has_mit else "当前电机不支持 MIT 协议")
        if not has_mit:
            self.tabs.setCurrentIndex(0)
        if d is None:
            self.cur.rows["out"].spin.setSuffix("")
            self.lbl_lim.setText("—")
            self._lim_warning = False
            self._refresh_limit_style()
            self.tabs.setCurrentIndex(0)
            for lb in self.lbl.values():
                lb.setText("")
            self._update_feedback_rows()
            return
        r = self.cur.rows["out"]
        r.spin.setSuffix(f" {d.out_unit}")
        self._update_output_range()
        if d.protocol == "gd":
            self.set_limits(d.lim, True)
        self.mit.rows["kp"].spin.setToolTip("GD 原生 Kp，位置误差按圈计算" if d.protocol == "gd" else "")
        self.mit.rows["kd"].spin.setToolTip("GD 原生 Kd，速度误差按圈/s计算" if d.protocol == "gd" else "")
        self._tab_changed(self.tabs.currentIndex())
        self._update_feedback_rows()

    def on_state(self, s: dict):
        """同步 Worker 的实际输出状态，控制参数下发权限。"""
        active = bool(s.get("active", False))
        self.gd.on_state(s)
        if self._active and not active:
            # 停止输出后清掉界面和后台目标，重新使能必须从零开始。
            self.cur.reset_zero()
            self.mit.reset_zero()
            if self.worker is not None:
                self.worker.set_target_out(0.0)
        self._active = active
        self.cur.set_output_enabled(self._active)
        self.mit.set_output_enabled(self._active)

    def _update_feedback_rows(self):
        has_status = self.driver is not None and self.driver.caps.status_code
        title, value = self._feedback_rows["code"]
        title.setVisible(has_status)
        value.setVisible(has_status)

    def set_limits(self, lim: P.Limits, from_motor: bool):
        r = self.mit.rows
        r["p"].set_range(lim.p_min, lim.p_max)
        r["v"].set_range(lim.v_min, lim.v_max)
        r["kp"].set_range(lim.kp_min, lim.kp_max)
        r["kd"].set_range(lim.kd_min, lim.kd_max)
        r["t"].set_range(lim.t_min, lim.t_max)
        if self.driver is not None and self.driver.caps.mit:
            self._update_output_range()
        no_can_limits = self.driver is not None and getattr(self.driver, "limits_from_motor", True) is False
        src = ("默认量程（该型号不支持 CAN 读取）" if no_can_limits else
               ("来自电机" if from_motor else "未读取（默认值，禁止使能）"))
        src = getattr(self.driver, "limits_source", src)
        self.lbl_lim.setText(f"p {lim.p_min:.7g}~{lim.p_max:.7g}  v ±{lim.v_max:.7g}  t ±{lim.t_max:.7g}\n"
                             f"kp {lim.kp_min:g}~{lim.kp_max:g}  kd {lim.kd_min:g}~{lim.kd_max:g}（{src}）")
        self._lim_warning = not from_motor and not no_can_limits
        self._refresh_limit_style()

    def set_output_limit(self, limit: float | None):
        """同步左侧安全限幅，控制滑条只能在当前允许范围内调整。"""
        value = None if limit is None or limit <= 0 else float(limit)
        if self._safety_out_max == value:
            return
        self._safety_out_max = value
        self._update_output_range()

    def _update_output_range(self):
        if self.driver is None:
            return
        protocol_limit = max(0.0, float(self.driver.out_max))
        limit = protocol_limit if self._safety_out_max is None else min(protocol_limit, self._safety_out_max)
        self.cur.rows["out"].set_range(-limit, limit)

    def on_sample(self, s):
        d = self.driver
        if d is None:
            return
        fb = s.fb
        self.lbl["pos_raw"].setText(f"{fb.pos:+.4f} rad")
        self.lbl["pos_mt"].setText(f"{s.pos_mt:+.4f} rad")
        self.lbl["vel"].setText("—" if fb.vel != fb.vel else f"{fb.vel:+.3f} rad/s")
        cmd = "—" if s.out != s.out else f"{s.out:+.3f}"
        self.lbl["out"].setText(f"{cmd} / {fb.out:+.3f} {d.out_unit}")
        t2 = "" if fb.temp2 != fb.temp2 else f" / {fb.temp2:.0f}"
        self.lbl["tmp"].setText("—" if fb.temp != fb.temp else f"{fb.temp:.0f}{t2} ℃")
        if d.caps.status_code:
            fault = d.is_fault(fb.code)
            self.lbl["code"].setText(f"{fb.code}  {d.code_text(fb.code)}")
            self._code_fault = fault
            self._refresh_code_style()
        else:
            self.lbl["code"].setText("")
            self._code_fault = False
            self._refresh_code_style()

    def to_current(self):
        self.tabs.setCurrentIndex(0)

    def _refresh_limit_style(self):
        self.lbl_lim.setStyleSheet(
            f"color:{'#ef5350' if self._dark else '#c62828'}" if self._lim_warning else ""
        )

    def _refresh_code_style(self):
        self.lbl["code"].setStyleSheet(
            f"color:{'#ef5350' if self._dark else '#c62828'};font-weight:bold"
            if self._code_fault else ""
        )

    # ---------- 内部 ----------
    def _apply_cur(self, a: dict):
        if self.worker:
            if a["out"] == 0.0:
                self.worker.zero_output()
            else:
                self.worker.set_target_out(a["out"])

    def _apply_mit(self, a: dict):
        if self.worker:
            self.worker.set_target_mit(P.MitCommand(a["p"], a["v"], a["kp"], a["kd"], a["t"]))

    def _tab_changed(self, i: int):
        if self.worker is not None:
            self.worker.set_mode("gd" if i == 2 else "mit" if i == 1 else "current")
