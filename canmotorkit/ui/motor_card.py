"""电机操作卡片：使能/失能、清错/设零和发送频率。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSpinBox, QWidget


class MotorCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.worker = None
        self.driver = None
        self._active = False
        self.lbl_title = QLabel("未选择电机")
        self.lbl_title.setObjectName("motorTitle")
        self.lbl_title.setStyleSheet(
            "QLabel#motorTitle{border:none;background:transparent;"
            "font-weight:bold;font-size:14px;padding:0;}"
        )
        self.lbl_state = QLabel("—")
        self.lbl_state.setAlignment(Qt.AlignCenter)
        self.lbl_state.setMinimumWidth(150)
        self.lbl_info = QLabel()
        self.lbl_info.setStyleSheet("font-family:Consolas,monospace")

        def btn(t, tip=""):
            b = QPushButton(t)
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(tip)
            return b
        self.btn_en = btn("使能", "达妙/HM 发 FC；大疆为软件开关。会自动打开发送。")
        self.btn_dis = btn("失能", "达妙/HM 发 FD（会刹车）；大疆发零电流并停止输出")
        self.btn_clr = btn("清错")
        self.btn_zero = btn("设零点", "当前位置设为零点（需失能）")
        self.sp_rate = QSpinBox()
        self.sp_rate.setRange(10, 1000)
        from .theme import settings
        try:
            rate = int(settings().value("card/rate_hz", 1000))
        except (TypeError, ValueError):
            rate = 1000
        self.sp_rate.setValue(max(10, min(1000, rate)))
        self.sp_rate.setToolTip("控制帧/查询帧的发送频率；退出时记忆")
        self.sp_rate.setSuffix(" Hz")
        self.lbl_rate = QLabel("—")
        self.lbl_rate.setMinimumWidth(0)

        self.output_panel = QWidget()
        output_row = QHBoxLayout(self.output_panel)
        output_row.setContentsMargins(8, 4, 8, 4)
        output_row.setSpacing(6)
        output_row.addWidget(self.btn_en, 1)
        output_row.addWidget(self.btn_dis, 1)

        self.service_panel = QFrame()
        service_row = QHBoxLayout(self.service_panel)
        service_row.setContentsMargins(0, 0, 0, 0)
        service_row.setSpacing(6)
        service_row.addWidget(self.btn_clr, 1)
        service_row.addWidget(self.btn_zero, 1)

        self.rate_panel = QWidget()
        rate_row = QHBoxLayout(self.rate_panel)
        rate_row.setContentsMargins(0, 0, 0, 0)
        rate_row.addWidget(QLabel("目标"))
        rate_row.addWidget(self.sp_rate)
        rate_row.addWidget(self.lbl_rate, 1)

        self.btn_en.clicked.connect(self._toggle_output)
        self.btn_dis.clicked.connect(lambda: self.worker and self.worker.disable())
        self.btn_clr.clicked.connect(lambda: self.worker and self.worker.clear_error())
        self.btn_zero.clicked.connect(lambda: self.worker and self.worker.set_zero())
        self.sp_rate.valueChanged.connect(self._rate)
        self.set_bound(None)

    def set_bound(self, worker):
        self.worker = worker
        self._rate()
        self.set_driver(None)

    def set_driver(self, d):
        self.driver = d
        on = self.worker is not None and d is not None
        self._active = False
        for w in (self.btn_en, self.btn_dis, self.sp_rate):
            w.setEnabled(on)
        self.btn_clr.setEnabled(on and d.caps.clear)
        self.btn_zero.setEnabled(on and d.caps.zero)
        has_clear = d is not None and d.caps.clear
        has_zero = d is not None and d.caps.zero
        self.btn_clr.setVisible(has_clear)
        self.btn_zero.setVisible(has_zero)
        self.service_panel.setVisible(has_clear or has_zero)
        if self.worker:
            self.worker.set_sending(on)
        if d is None:
            self.lbl_title.setText("未选择电机" if self.worker else "未连接")
            self.lbl_state.setText("—")
            self.lbl_state.setStyleSheet("")
            self.lbl_info.setText("")
            self.lbl_rate.setText("—")
            self.btn_en.setText("使能")
            self.btn_dis.setText("失能")
            self.btn_en.setStyleSheet("")
            return
        self.lbl_title.setText(d.model)
        self.btn_en.setText("使能" if d.caps.enable_cmd else "开始输出")
        self.btn_dis.setText("失能" if d.caps.enable_cmd else "停止输出")
        self.btn_en.setStyleSheet("")

    def save_settings(self, s):
        s.setValue("card/rate_hz", self.sp_rate.value())

    def _rate(self):
        if self.worker:
            self.worker.rate_hz = float(self.sp_rate.value())

    def _toggle_output(self):
        if not self.worker:
            return
        if self.driver is not None and not self.driver.caps.enable_cmd and self._active:
            self.worker.disable()
        else:
            self.worker.enable()

    def on_state(self, s: dict):
        d = s["driver"]
        if d is None:
            return
        self._active = bool(s["active"])
        self.btn_en.setEnabled(not (d.protocol == "gd" and s.get("gd_busy")))
        code = s["code"]
        if s["scanning"]:
            txt, st = "扫描中", "background:#90caf9;color:#102a43"
        elif not s["link_ok"]:
            txt, st = "无反馈", "background:#9e9e9e;color:#202020"
        elif d.caps.status_code and d.is_fault(code):
            txt, st = d.code_text(code), "background:#c62828;color:white"
        elif s["active"]:
            txt, st = ("已使能", "background:#2e7d32;color:white") if d.caps.enable_cmd else ("", "")
        else:
            txt, st = ("失能", "background:#d7d7d7;color:#222222") if d.caps.enable_cmd else ("", "")
        if s["tripped"] and s["active"]:
            txt = (txt + " · " if txt else "") + "零电流锁存"
        if s["running"]:
            txt = (txt + " · " if txt else "") + "测试中"
        self.lbl_state.setText(txt)
        self.lbl_state.setStyleSheet(st + ";font-weight:bold;padding:3px;border-radius:3px" if st else "")
        if not d.caps.enable_cmd:
            active = bool(s["active"])
            self.btn_en.setText("输出中" if active else "开始输出")
            self.btn_en.setStyleSheet(
                "QPushButton{background:#2e7d32;color:white;font-weight:bold;}"
                if active else ""
            )
        if s["active"]:
            what = "MIT 帧" if s["mode"] == "mit" else f"{d.out_name}指令"
        else:
            what = "状态查询" if d.idle_frames() else "未输出"
        self.lbl_rate.setText(f"实际 {what} {s['tx_rate']:.0f} Hz")
