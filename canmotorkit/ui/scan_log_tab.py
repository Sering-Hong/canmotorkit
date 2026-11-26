"""报文页：原始帧日志（解析注释/过滤/保存）、手动发送、往返延迟直方图。"""
from __future__ import annotations

from collections import deque

import numpy as np
import pyqtgraph as pg
import time
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from canmotorkit.drivers import dm_protocol as P

from . import theme
from .widgets import CheckBox


class ScanLogTab(QWidget):
    # 非聚合模式是原始报文监视器，不适合无限制地把 1 kHz 报文全部保存在
    # QTableWidget 中。保留最近一段可视日志，并按批次裁剪，避免每帧删除首行。
    _RAW_ROW_LIMIT = 2000
    _RAW_TRIM_BATCH = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.driver = None
        self.t0: float | None = None
        self._row_by_key: dict[tuple, int] = {}
        self._bus_bits: deque[tuple[float, int]] = deque()
        self._feedback_times: deque[float] = deque()
        self._feedback_id: int | None = None
        self._last_log_scroll_t = 0.0

        lg = QGroupBox("报文日志")
        self.chk_log = CheckBox("记录")
        self.chk_log.setChecked(True)
        self.chk_pause = CheckBox("暂停显示")
        self.chk_hide = CheckBox("隐藏周期帧（控制/反馈/查询）")
        self.chk_mine = CheckBox("只显示当前电机")
        self.chk_aggregate = CheckBox("按 ID 聚合")
        self.chk_aggregate.setChecked(True)
        self.lbl_load = QLabel("总线负载率：—")
        self.lbl_load.setToolTip("最近 1 秒经典 CAN 帧负载估算，包含位填充和帧间隔")
        self.lbl_fps = QLabel("反馈帧率：—")
        self.lbl_fps.setToolTip("自动统计当前选中电机最近 1 秒收到的反馈帧数量")
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("过滤 ID（十六进制，逗号分隔，如 7FF,0,201）")
        self.btn_clear = QPushButton("清空")
        self.btn_save = QPushButton("保存…")
        self.log = QTableWidget(0, 9)
        self.log.setObjectName("scanLogTable")
        self.log.setHorizontalHeaderLabels(
            ("Timestamp", "Channel", "Rx/Tx", "CAN ID", "Sender", "Name", "DLC", "Data", "Comment")
        )
        self.log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.log.setAlternatingRowColors(True)
        self.log.setSortingEnabled(False)
        self.log.verticalHeader().setVisible(False)
        header = self.log.horizontalHeader()
        # 短字段使用固定紧凑宽度，避免报文内容把前面的列撑宽；剩余空间留给
        # Data 和 Comment，窄窗口下也能优先看到协议说明。
        compact_widths = {
            0: 84,    # Timestamp
            1: 94,    # Channel
            2: 58,    # Rx/Tx
            3: 72,    # CAN ID
            4: 64,    # Sender
            5: 104,   # Name
            6: 44,    # DLC
        }
        for column, width in compact_widths.items():
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            header.resizeSection(column, width)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        self.log.setFont(QFont("Consolas", 9))
        lr = QHBoxLayout()
        for w in (self.chk_log, self.chk_pause, self.chk_hide, self.chk_mine, self.chk_aggregate):
            lr.addWidget(w)
        lr.addSpacing(8)
        lr.addWidget(self.lbl_load)
        lr.addWidget(self.lbl_fps)
        lr.addWidget(self.ed_filter, 1)
        lr.addWidget(self.btn_clear)
        lr.addWidget(self.btn_save)
        self.ed_id = QLineEdit("7FF")
        self.ed_id.setMaximumWidth(60)
        self.ed_data = QLineEdit("01 00 33 3C 00 00 00 00")
        self.btn_send = QPushButton("发送")
        self.cb_frame_mode = QComboBox()
        self.cb_frame_mode.addItem("标准数据帧", (False, False))
        self.cb_frame_mode.addItem("扩展数据帧", (True, False))
        self.cb_frame_mode.addItem("标准远程帧（RTR）", (False, True))
        self.cb_frame_mode.addItem("扩展远程帧（RTR）", (True, True))
        self.sp_dlc = QSpinBox()
        self.sp_dlc.setRange(0, 8)
        self.sp_dlc.setValue(8)
        self.sp_dlc.setSuffix(" 字节")
        self.sp_auto_hz = QDoubleSpinBox()
        self.sp_auto_hz.setRange(0.1, 2000.0)
        self.sp_auto_hz.setDecimals(1)
        self.sp_auto_hz.setSingleStep(1.0)
        self.sp_auto_hz.setValue(10.0)
        self.sp_auto_hz.setSuffix(" Hz")
        self.sp_auto_hz.setToolTip("原始帧自动发送频率；周期发送不经过电机安全限制")
        self.btn_auto = QPushButton("开始自动发送")
        self._auto_frame: tuple[int, bytes, bool, bool, int] | None = None
        sr = QHBoxLayout()
        for w in (QLabel("手动发送  ID(hex)"), self.ed_id, QLabel("数据(hex)")):
            sr.addWidget(w)
        sr.addWidget(self.ed_data, 1)
        sr.addWidget(self.btn_send)
        mr = QHBoxLayout()
        mr.addWidget(QLabel("帧类型"))
        mr.addWidget(self.cb_frame_mode)
        mr.addWidget(QLabel("RTR DLC"))
        mr.addWidget(self.sp_dlc)
        mr.addStretch(1)
        ar = QHBoxLayout()
        ar.addWidget(QLabel("自动发送频率"))
        ar.addWidget(self.sp_auto_hz)
        ar.addWidget(self.btn_auto)
        ar.addStretch(1)
        ll = QVBoxLayout(lg)
        ll.addLayout(lr)
        ll.addWidget(self.log, 1)
        ll.addLayout(sr)
        ll.addLayout(mr)
        ll.addLayout(ar)

        hg = QGroupBox("往返延迟（发送 → 收到应答，仅问答式协议）")
        self.hist = pg.PlotWidget(background="w")
        self.hist.setLabel("bottom", "延迟", units="s")
        self.hist.setLabel("left", "次数")
        self.hist.showGrid(x=True, y=True, alpha=0.3)
        self.bars = pg.BarGraphItem(x=[], height=[], width=1e-4, brush="#1565c0")
        self.hist.addItem(self.bars)
        self.lbl_lat = QLabel("—")
        self.lbl_lat.setProperty("themeRole", "muted")
        self.btn_lat_clear = QPushButton("清空统计")
        hl = QVBoxLayout(hg)
        r = QHBoxLayout()
        r.addWidget(self.lbl_lat, 1)
        r.addWidget(self.btn_lat_clear)
        hl.addLayout(r)
        hl.addWidget(self.hist)

        sp = QSplitter(Qt.Vertical)
        sp.addWidget(lg)
        sp.addWidget(hg)
        sp.setSizes([600, 250])
        lay = QVBoxLayout(self)
        lay.addWidget(sp)

        self.btn_clear.clicked.connect(self._clear_log)
        self.btn_save.clicked.connect(self._save)
        self.btn_send.clicked.connect(self._send)
        self.btn_auto.clicked.connect(self._toggle_auto)
        self.sp_auto_hz.valueChanged.connect(self._auto_rate_changed)
        self.chk_aggregate.toggled.connect(self._aggregate_changed)
        self.cb_frame_mode.currentIndexChanged.connect(self._frame_mode_changed)
        self.chk_log.toggled.connect(lambda on: self.worker and setattr(self.worker, "log_enabled", on))
        self.btn_lat_clear.clicked.connect(lambda: self.worker and self.worker.latencies.clear())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._hist)
        self.timer.start(500)
        self.apply_theme(True)
        self._frame_mode_changed()
        self.set_bound(None)

    def apply_theme(self, dark: bool):
        """同步延迟直方图的背景、坐标轴和图例颜色。"""
        background = theme.current().plot_bg()
        foreground = theme.current().pal.text
        self.hist.setBackground(background)
        for axis_name in ("left", "bottom", "right", "top"):
            axis = self.hist.getPlotItem().getAxis(axis_name)
            axis.setPen(pg.mkPen(foreground))
            axis.setTextPen(pg.mkPen(foreground))

    def set_bound(self, worker):
        self._stop_auto()
        self.worker = worker
        self.btn_send.setEnabled(worker is not None)
        self.btn_auto.setEnabled(worker is not None)
        if worker:
            worker.log_enabled = self.chk_log.isChecked()
        self.driver = None
        self._feedback_id = None
        self._feedback_times.clear()

    def set_driver(self, d):
        self.driver = d
        self._feedback_id = self._driver_feedback_id(d)
        self._feedback_times.clear()
        self.lbl_fps.setText(self._fps_text("—"))
        if d is None:
            self._stop_auto()
        if d is None:
            self.reset_for_no_driver()

    def reset_for_no_driver(self):
        """清除当前电机对应的报文和延迟统计。"""
        self.t0 = None
        self._clear_log()
        self._bus_bits.clear()
        self._feedback_times.clear()
        self.lbl_load.setText("总线负载率：—")
        self.lbl_fps.setText(self._fps_text("—"))
        self.bars.setOpts(x=[], height=[])
        self.lbl_lat.setText("—")
        if self.worker is not None:
            self.worker.latencies.clear()

    def _hist(self):
        w = self.worker
        if w is None or not self.isVisible():
            return
        now = time.perf_counter()
        cutoff = now - 1.0
        while self._bus_bits and self._bus_bits[0][0] < cutoff:
            self._bus_bits.popleft()
        while self._feedback_times and self._feedback_times[0] < cutoff:
            self._feedback_times.popleft()
        bitrate = self._bus_bitrate()
        if bitrate:
            used_bits = sum(bits for _, bits in self._bus_bits)
            load = min(100.0, used_bits / bitrate * 100.0)
            self.lbl_load.setText(f"总线负载率：{load:.1f}%（估算）")
        else:
            self.lbl_load.setText("总线负载率：—")
        self.lbl_fps.setText(self._fps_text(f"{len(self._feedback_times):.0f} FPS"))
        xs = np.array(list(w.latencies))
        if len(xs) < 2:
            self.lbl_lat.setText("暂无数据（大疆为主动广播，无往返延迟）" if self.driver and
                                 not self.driver.caps.enable_cmd else "暂无数据")
            return
        h, e = np.histogram(xs, bins=40)
        self.bars.setOpts(x=(e[:-1] + e[1:]) / 2, height=h, width=(e[1] - e[0]) * 0.9)
        p = np.percentile(xs, [50, 95, 99])
        self.lbl_lat.setText(f"n={len(xs)}  p50 {p[0] * 1e3:.2f} ms  p95 {p[1] * 1e3:.2f} ms  "
                             f"p99 {p[2] * 1e3:.2f} ms  max {xs.max() * 1e3:.2f} ms")

    def _bus_bitrate(self) -> int | None:
        bus = getattr(self.worker, "bus", None)
        if bus is None:
            return None
        bitrate = getattr(bus, "bitrate", None)
        if bitrate:
            return int(bitrate)
        pcan_bitrates = {0x0014: 1_000_000, 0x0016: 800_000,
                         0x001C: 500_000, 0x011C: 250_000, 0x031C: 125_000}
        return pcan_bitrates.get(getattr(bus, "baud", None))

    @staticmethod
    def _driver_feedback_id(driver) -> int | None:
        """返回驱动声明的反馈 CAN ID；不同协议字段名称不同。"""
        if driver is None:
            return None
        if hasattr(driver, "fb_id"):
            return int(driver.fb_id)
        if hasattr(driver, "master_id"):
            return int(driver.master_id)
        return None

    def _fps_text(self, value: str) -> str:
        if self._feedback_id is None:
            return f"反馈帧率：{value}"
        return f"反馈帧率 0x{self._feedback_id:03X}：{value}"

    def _is_selected_feedback(self, frame) -> bool:
        """判断一帧是否是当前选中电机的反馈帧。"""
        if frame.tx or self.driver is None:
            return False
        driver = self.driver
        if frame.extended != driver.extended or frame.rtr:
            return False
        if self._feedback_id is not None and frame.can_id != self._feedback_id:
            return False
        try:
            if not driver.accepts(frame.can_id, frame.data):
                return False
            feedback = driver.parse(frame.can_id, frame.data)
        except (IndexError, TypeError, ValueError):
            return False
        if feedback is None:
            return False
        # 达妙/HM 的多个电机可能共用同一个反馈 CAN ID，继续用状态帧里的
        # motor_id4 区分当前选中的电机；DJI 的反馈 ID 已经包含电机 ID。
        return getattr(feedback, "motor_id", driver.motor_id) == driver.motor_id

    @staticmethod
    def _frame_bits(frame) -> int:
        """估算经典 CAN 一帧在线上占用的 bit 数（含位填充和帧间隔）。"""
        base = 67 if frame.extended else 47
        raw = base + 8 * frame.wire_dlc
        return raw + (raw + 4) // 5 + 3

    def _filter_ids(self):
        t = self.ed_filter.text().strip()
        if not t:
            return None
        out = set()
        for x in t.replace(" ", "").split(","):
            try:
                out.add(int(x, 16))
            except ValueError:
                pass
        return out

    def _clear_log(self):
        self.log.setRowCount(0)
        self._row_by_key.clear()
        self._last_log_scroll_t = 0.0

    def _aggregate_changed(self, _checked: bool):
        # 切换显示模式时清空旧行，避免一部分历史行已聚合、一部分未聚合。
        self._clear_log()

    def _frame_mode_changed(self, _index: int = -1):
        _extended, rtr = self.cb_frame_mode.currentData()
        self.ed_data.setEnabled(not rtr)
        self.sp_dlc.setEnabled(rtr)

    @staticmethod
    def _frame_id_text(frame) -> str:
        width = 8 if frame.extended else 3
        text = f"0x{frame.can_id:0{width}X}"
        return text + " (Ext)" if frame.extended else text

    def _frame_values(self, frame, note: str) -> list[str]:
        if self.t0 is None:
            self.t0 = frame.t
        channel = getattr(getattr(self.worker, "bus", None), "name", "")
        direction = "TX" if frame.tx else "RX"
        if frame.rtr:
            direction += " / RTR"
        sender = "本机" if frame.tx else "总线"
        name = self.driver.model if self.driver else ""
        data = "—" if frame.rtr else P.hexdata(frame.data)
        return [
            f"{frame.t - self.t0:.4f}", channel, direction,
            self._frame_id_text(frame), sender, name, str(frame.wire_dlc), data, note,
        ]

    def _append_frame(self, frame, note: str):
        values = self._frame_values(frame, note)
        key = (frame.can_id, frame.tx, frame.extended, frame.rtr)
        aggregate = self.chk_aggregate.isChecked()
        row = self._row_by_key.get(key) if aggregate else None
        if row is None:
            row = self.log.rowCount()
            self.log.insertRow(row)
            if aggregate:
                self._row_by_key[key] = row
        for column, value in enumerate(values):
            item = self.log.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                if column == 3:
                    item.setData(Qt.UserRole, key)
                self.log.setItem(row, column, item)
            item.setText(value)

    def _trim_raw_rows(self):
        """批量删除最旧的原始日志行，避免逐帧 removeRow(0) 导致卡顿。"""
        if self.chk_aggregate.isChecked():
            return
        count = self.log.rowCount()
        if count <= self._RAW_ROW_LIMIT + self._RAW_TRIM_BATCH:
            return
        remove_count = count - self._RAW_ROW_LIMIT
        for _ in range(remove_count):
            self.log.removeRow(0)

    def _append_message_row(self, message: str):
        row = self.log.rowCount()
        self.log.insertRow(row)
        self.log.setItem(row, 8, QTableWidgetItem(message))
        self.log.scrollToBottom()

    def on_frames(self, frames):
        if self.chk_pause.isChecked() or not frames:
            return
        if self.t0 is None:
            self.t0 = frames[0].t
        flt = self._filter_ids()
        hide = self.chk_hide.isChecked()
        mine = self.chk_mine.isChecked()
        d = self.driver
        aggregate = self.chk_aggregate.isChecked()
        display_start = max(0, len(frames) - 300) if not aggregate else 0
        appended = False
        self.log.setUpdatesEnabled(False)
        self.log.blockSignals(True)
        try:
            for index, f in enumerate(frames):
                self._bus_bits.append((f.t, self._frame_bits(f)))
                if self._is_selected_feedback(f):
                    if self._feedback_id is None:
                        self._feedback_id = f.can_id
                    self._feedback_times.append(f.t)
                if index < display_start:
                    continue
                if flt is not None and f.can_id not in flt:
                    continue
                note = d.describe(f.can_id, f.data, f.tx) if d else ""
                if mine and not note:
                    continue
                if hide and (note in ("MIT控制", "查询状态", "反馈") or note.startswith("状态 ")
                             or note.startswith("下发电流") or note.startswith("位置:")):
                    continue
                self._append_frame(f, note)
                appended = True
            self._trim_raw_rows()
        finally:
            self.log.blockSignals(False)
            self.log.setUpdatesEnabled(True)
        # 高频报文下滚动条本身也会触发布局计算，最多每 100 ms 自动滚动一次。
        if appended and time.perf_counter() - self._last_log_scroll_t >= 0.1:
            self.log.scrollToBottom()
            self._last_log_scroll_t = time.perf_counter()

    def append_text(self, s: str):
        self._append_message_row(s)

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", "can_log.txt", "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                headers = [self.log.horizontalHeaderItem(i).text() for i in range(self.log.columnCount())]
                f.write("\t".join(headers) + "\n")
                for row in range(self.log.rowCount()):
                    values = []
                    for column in range(self.log.columnCount()):
                        item = self.log.item(row, column)
                        values.append(item.text() if item else "")
                    f.write("\t".join(values) + "\n")

    def _parse_frame(self) -> tuple[int, bytes, bool, bool, int]:
        extended, rtr = self.cb_frame_mode.currentData()
        cid = int(self.ed_id.text().strip(), 16)
        max_id = 0x1FFFFFFF if extended else 0x7FF
        if not 0 <= cid <= max_id:
            raise ValueError(f"CAN ID 需在 0~{max_id:X}")
        if rtr:
            return cid, b"", extended, True, self.sp_dlc.value()
        data = bytes.fromhex(self.ed_data.text().replace(",", " "))
        if len(data) > 8:
            raise ValueError("经典 CAN 数据最多 8 字节")
        return cid, data, extended, False, len(data)

    def _toggle_auto(self):
        if self._auto_frame is not None:
            self._stop_auto()
            return
        if self.worker is None:
            return
        try:
            frame = self._parse_frame()
        except ValueError as e:
            QMessageBox.warning(self, "自动发送", str(e))
            return
        self._auto_frame = frame
        self.worker.start_raw_periodic(frame[0], frame[1], self.sp_auto_hz.value(),
                                       frame[2], frame[3], frame[4])
        self.btn_auto.setText("停止自动发送")
        self.ed_id.setEnabled(False)
        self.ed_data.setEnabled(False)
        self.cb_frame_mode.setEnabled(False)
        self.sp_dlc.setEnabled(False)

    def _auto_rate_changed(self, value: float):
        if self._auto_frame is not None and self.worker is not None:
            cid, data, extended, rtr, dlc = self._auto_frame
            self.worker.start_raw_periodic(cid, data, value, extended, rtr, dlc)

    def _stop_auto(self):
        if self.worker is not None:
            self.worker.stop_raw_periodic()
        self._auto_frame = None
        if hasattr(self, "btn_auto"):
            self.btn_auto.setText("开始自动发送")
            self.btn_auto.setEnabled(self.worker is not None)
        if hasattr(self, "ed_id"):
            self.ed_id.setEnabled(True)
            self.ed_data.setEnabled(True)
            self.cb_frame_mode.setEnabled(True)
            self._frame_mode_changed()

    @staticmethod
    def _send_summary(frame) -> str:
        cid, data, extended, rtr, dlc = frame
        prefix = f"0x{cid:08X}" if extended else f"0x{cid:03X}"
        mode = "扩展" if extended else "标准"
        if rtr:
            return f"{mode} RTR {prefix} DLC={dlc}"
        return f"{mode} {prefix} [{P.hexdata(data)}]"

    def _send(self):
        try:
            cid, data, extended, rtr, dlc = self._parse_frame()
        except ValueError as e:
            QMessageBox.warning(self, "发送", str(e))
            return
        if QMessageBox.question(self, "手动发送", f"发送 {self._send_summary((cid, data, extended, rtr, dlc))}？\n"
                                "手动帧不经过安全限制。") == QMessageBox.Yes:
            self.worker.send_raw(cid, data, extended, rtr, dlc)
