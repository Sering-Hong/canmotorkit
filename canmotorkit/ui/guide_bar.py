"""上手引导条：用 4 个步骤告诉用户「现在该做什么」，并提供一键完成当前步骤的按钮。

步骤：① 连接 CAN → ② 选择电机 → ③ 使能 / 开始输出 → ④ 设定输出。
只读取主窗口状态并调用已有的入口（connect_bus / 选电机 / 使能），不绕过任何安全逻辑。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import theme

STEPS = ("连接 CAN", "选择电机", "使能电机", "设定输出")


class GuideBar(QFrame):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.win = win
        self._step = 0
        self._action = None

        self.step_labels = []
        steps = QHBoxLayout()
        steps.setSpacing(4)
        for i, name in enumerate(STEPS):
            lb = QLabel(f"{'①②③④'[i]} {name}")
            lb.setAlignment(Qt.AlignCenter)
            lb.setMinimumWidth(86)
            self.step_labels.append(lb)
            steps.addWidget(lb)
            if i < len(STEPS) - 1:
                arrow = QLabel("›")
                arrow.setProperty("themeRole", "muted")
                steps.addWidget(arrow)
        steps.addStretch(1)

        self.lbl_hint = QLabel()
        self.lbl_hint.setWordWrap(True)
        self.btn = QPushButton()
        self.btn.setObjectName("primary")
        self.btn.setMinimumHeight(34)
        self.btn.setMinimumWidth(170)
        self.btn.setFocusPolicy(Qt.NoFocus)
        self.btn.clicked.connect(self._do_action)
        self.btn_help = QPushButton("?")
        self.btn_help.setToolTip("使用说明：接线、步骤、安全、常见问题")
        self.btn_help.setFixedSize(34, 34)
        self.btn_help.setFocusPolicy(Qt.NoFocus)
        self.btn_help.clicked.connect(win.open_help)

        left = QVBoxLayout()
        left.setSpacing(4)
        left.addLayout(steps)
        left.addWidget(self.lbl_hint)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addLayout(left, 1)
        lay.addWidget(self.btn_help, 0, Qt.AlignVCenter)
        lay.addWidget(self.btn, 0, Qt.AlignVCenter)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(200)
        self.refresh()

    # ---------- 状态判断 ----------
    def _evaluate(self):
        """返回 (步骤序号 0~4, 提示文字, 按钮文字或 None, 动作)。"""
        w = self.win
        worker = w.worker
        conn = w.conn
        if worker is None:
            data = conn.cb_channel.currentData()
            name = conn.cb_channel.currentText() or "（无可用通道）"
            if data and data[0] == "sim":
                return (0, "没有检测到 USB 转 CAN 适配器（PCAN / CANable）。接好适配器后点左侧「刷新」；"
                        "只想熟悉操作可以先连接「演示台」虚拟电机。点「?」看接线说明。",
                        "连接演示台", lambda: conn.btn_connect.click())
            return (0, f"已检测到适配器：{name}。确认电机已上电、波特率正确（一般 1M），然后连接。",
                    "连接", lambda: conn.btn_connect.click())
        s = w._last_state or {}
        if w.driver is None:
            m = w.motors
            if s.get("scanning") or not m.btn_scan.isEnabled():
                return 1, "正在扫描总线上的电机，请稍候…", None, None
            if not m.entries:
                return (1, "总线上没有发现电机：检查电机供电、CAN 线和波特率（常见 1M），然后重新扫描。",
                        "重新扫描", m.scan_bus)
            row = m.tbl.currentRow()
            if not (0 <= row < len(m.entries)):
                row = 0
            e = m.entries[row]
            n = len(m.entries)
            tip = (f"发现 {n} 台电机。在左侧列表选中要调试的一台（双击也可），"
                   f"型号不对可点型号下拉改。" if n > 1 else "发现 1 台电机。")
            return (1, tip, f"调试 {e['model']} · ID {e['motor_id']}", lambda r=row: m._use_row(r))
        d = w.driver
        if s.get("tripped"):
            return (2, f"安全保护已触发零输出：{s.get('trip_reason', '')}。确认机构安全后再解除。",
                    "解除锁存", lambda: worker.reset_safety())
        if not s.get("active"):
            if not s.get("limits_ok", True):
                return 2, "正在从电机读取量程，读到后才能使能…", None, None
            if not s.get("link_ok"):
                return (2, "已选择电机但收不到反馈：检查电机供电与 ID，或换一台电机。", None, None)
            verb = "使能" if d.caps.enable_cmd else "开始输出"
            return (2, f"电机在线，未输出。点「{verb}」后电机受控（左侧限幅已默认开启）。"
                    "任何时候按 Esc 或空格急停。", verb, lambda: w.card._toggle_output())
        unit = d.out_unit
        return (3, f"电机已受控。在「控制」页拖动滑条或输入数值，点「应用」才生效（单位 {unit}）。"
                "Esc / 空格 = 急停（零输出，不失能）。", "输出归零", lambda: worker.zero_output())

    def refresh(self):
        try:
            step, hint, btn, action = self._evaluate()
        except Exception as e:  # noqa: BLE001 — 引导条不能影响主功能
            step, hint, btn, action = self._step, f"引导状态未知：{e}", None, None
        self._step, self._action = step, action
        self.lbl_hint.setText(hint)
        self.btn.setVisible(btn is not None)
        if btn is not None:
            self.btn.setText(btn)
        t = theme.current()
        for i, lb in enumerate(self.step_labels):
            if i < step:
                lb.setStyleSheet(f"color:{t.pal.ok};font-weight:600;padding:3px 8px;")
                lb.setText(f"✓ {STEPS[i]}")
            elif i == step:
                lb.setStyleSheet(f"background:{t.accent};color:{t.accent_on};font-weight:700;"
                                 "padding:3px 8px;border-radius:10px;")
                lb.setText(f"{'①②③④'[i]} {STEPS[i]}")
            else:
                lb.setStyleSheet(f"color:{t.pal.text_muted};padding:3px 8px;")
                lb.setText(f"{'①②③④'[i]} {STEPS[i]}")

    def _do_action(self):
        if self._action is not None:
            self._action()
            QTimer.singleShot(50, self.refresh)
