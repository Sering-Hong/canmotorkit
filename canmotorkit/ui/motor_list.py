"""② 总线电机：扫描、确认型号并选中唯一电机进行调试。"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox,
                               QTableWidget, QVBoxLayout)

from canmotorkit.drivers import dji
from canmotorkit.drivers.catalog import PROTOCOLS, make_driver

from . import theme
from .widgets import ArrowOnlyComboBox

ALL_MODELS = [(p, m) for p, (_, ms) in PROTOCOLS.items() for m in ms]


class _FillHeader(QHeaderView):
    """三列始终填满表格，拖动一个分界线时只调整相邻两列。"""

    # 左栏最窄时也能完整放下三列；拖动时仍会锁住每列的可用下限。
    MIN_WIDTHS = (140, 55, 85)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._ready = False
        self._adjusting = False
        self._last_width = 0
        self.setSectionsMovable(False)
        self.setStretchLastSection(False)
        self.setMinimumSectionSize(min(self.MIN_WIDTHS))
        self.sectionResized.connect(self._on_section_resized)

    def set_default_sizes(self):
        width = self.width()
        if width <= 0:
            return False
        total_min = sum(self.MIN_WIDTHS)
        if width >= total_min:
            first = max(self.MIN_WIDTHS[0], int(width / 3))
            second = max(self.MIN_WIDTHS[1], int(width * 11 / 30))
            third = max(self.MIN_WIDTHS[2], width - first - second)
            if first + second + third > width:
                first = width - second - third
        else:
            # 极窄窗口下优先保证三列仍然存在，不让表格产生横向滚动条。
            first = max(45, int(width / 3))
            second = max(35, int(width * 11 / 30))
            third = max(45, width - first - second)
            first = max(45, width - second - third)
        self._adjusting = True
        for index, size in enumerate((first, second, third)):
            self.resizeSection(index, size)
        self._adjusting = False
        self._ready = True
        self._last_width = width
        self._fill_to_width()
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ready and not self._adjusting:
            width = self.width()
            if width != self._last_width:
                self._resize_proportionally(width)
                self._last_width = width

    def _fill_to_width(self):
        target = self.width()
        current = sum(self.sectionSize(i) for i in range(self.count()))
        delta = target - current
        if delta == 0:
            return
        self._adjusting = True
        if delta > 0:
            self.resizeSection(self.count() - 1, self.sectionSize(self.count() - 1) + delta)
        else:
            for index in reversed(range(self.count())):
                size = self.sectionSize(index)
                minimum = self.MIN_WIDTHS[index]
                reduced = max(minimum, size + delta)
                self.resizeSection(index, reduced)
                delta -= reduced - size
                if delta == 0:
                    break
        self._adjusting = False

    def _resize_proportionally(self, target: int):
        """表格整体变化时让三列一起缩放，避免只有末列变化。"""
        sizes = [self.sectionSize(i) for i in range(self.count())]
        total = sum(sizes)
        if total <= 0 or target <= 0:
            return
        minimums = list(self.MIN_WIDTHS)
        if target < sum(minimums):
            # 极窄情况下仍交给原有逻辑处理，优先保证表格不溢出。
            self._fill_to_width()
            return

        # 先按当前比例分配，再把低于最小宽度的列固定住，将剩余空间
        # 继续按比例分给其它列。
        result = [0] * self.count()
        remaining_indices = set(range(self.count()))
        remaining_target = target
        remaining_weight = float(total)
        while remaining_indices:
            fixed = False
            for index in tuple(remaining_indices):
                proposed = round(remaining_target * sizes[index] / remaining_weight)
                if proposed < minimums[index]:
                    result[index] = minimums[index]
                    remaining_target -= result[index]
                    remaining_weight -= sizes[index]
                    remaining_indices.remove(index)
                    fixed = True
            if not fixed:
                for index in remaining_indices:
                    result[index] = round(remaining_target * sizes[index] / remaining_weight)
                break

        # 舍入可能产生 1~2 px 误差，补到最后一个可调整列。
        delta = target - sum(result)
        if delta:
            for index in reversed(range(self.count())):
                if result[index] + delta >= minimums[index]:
                    result[index] += delta
                    break
        self._adjusting = True
        for index, size in enumerate(result):
            self.resizeSection(index, size)
        self._adjusting = False

    def _on_section_resized(self, index, old_size, new_size):
        if not self._ready or self._adjusting or old_size == new_size:
            return
        minimum = self.MIN_WIDTHS[index]
        if new_size < minimum:
            self._adjusting = True
            self.resizeSection(index, minimum)
            new_size = minimum
            self._adjusting = False
        neighbor = index + 1 if index + 1 < self.count() else index - 1
        if neighbor < 0:
            return
        delta = new_size - old_size
        neighbor_size = self.sectionSize(neighbor)
        neighbor_min = self.MIN_WIDTHS[neighbor]
        target = neighbor_size - delta
        if target < neighbor_min:
            available = neighbor_size - neighbor_min
            corrected = old_size + available
            self._adjusting = True
            self.resizeSection(index, corrected)
            self.resizeSection(neighbor, neighbor_min)
            self._adjusting = False
            return
        self._adjusting = True
        self.resizeSection(neighbor, target)
        self._adjusting = False


class MotorListPanel(QGroupBox):
    motor_selected = Signal(object)     # Driver

    def __init__(self, parent=None):
        super().__init__("② 总线电机", parent)
        self.worker = None
        self.entries: list[dict] = []
        self._debug_row: int | None = None
        self._temporary_row: int | None = None
        self.btn_scan = QPushButton("扫描总线")
        self.btn_scan.setToolTip("监听大疆广播，查询达妙/HM 和高擎 GD 状态。需在停止输出时进行。")
        self.sp_to = QSpinBox()
        self.sp_to.setRange(1, 127)
        self.sp_to.setValue(40)
        self.sp_to.setPrefix("查询 ID 1~")
        self.sp_to.setToolTip("达妙/HM、高擎 GD 共用查询 ID 范围（GD 最大 126）；大疆自动监听反馈广播")
        self.prog = QProgressBar()
        self.prog.setTextVisible(True)
        self.prog.setFormat("")
        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeader(_FillHeader(self.tbl))
        self.tbl.setHorizontalHeaderLabels(["协议 / 型号", "电机 ID", "反馈帧 ID"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tbl.itemSelectionChanged.connect(self._selection_changed)
        self.tbl.cellDoubleClicked.connect(self._double_click)
        hh = self.tbl.horizontalHeader()
        for column in range(3):
            hh.setSectionResizeMode(column, QHeaderView.Interactive)
        self.apply_theme(True)
        self._columns_initialized = False
        QTimer.singleShot(0, self._set_default_column_widths)
        self.tbl.setMinimumHeight(130)
        self.btn_use = QPushButton("双击选择电机")
        self.btn_use.setStyleSheet("QPushButton:enabled{font-weight:bold}")

        top = QHBoxLayout()
        top.addWidget(self.btn_scan)
        top.addWidget(self.sp_to)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.prog)
        lay.addWidget(self.tbl)
        b = QHBoxLayout()
        b.addWidget(self.btn_use, 1)
        lay.addLayout(b)

        self.btn_scan.clicked.connect(self.scan_bus)
        self.btn_use.clicked.connect(self._action_clicked)
        self.set_bound(None)

    # ---------- 外部 ----------
    def set_bound(self, worker):
        self.worker = worker
        self._debug_row = None
        self._temporary_row = None
        for w in (self.btn_scan, self.btn_use):
            w.setEnabled(worker is not None)
        if worker is None:
            self.entries.clear()
            self.tbl.setRowCount(0)
            self.prog.setValue(0)
            self.prog.setFormat("")
        self._update_action_button()
        self._update_selection_style()

    def apply_theme(self, dark: bool):
        # 表头/网格颜色由全局主题样式表提供，这里只刷新选中行高亮。
        self.tbl.horizontalHeader().setStyleSheet("")
        self.tbl.setStyleSheet("")
        self._update_selection_style()

    def on_state(self, s: dict):
        busy = s["active"] or s["scanning"] or s.get("gd_busy", False)
        self.btn_scan.setEnabled(not busy)
        self.btn_use.setEnabled(self.worker is not None and not busy)
        self.btn_scan.setToolTip("请先失能" if s["active"] else self.btn_scan.toolTip())
        self._update_action_button()

    def on_scan_progress(self, phase: str, i: int, n: int):
        self.prog.setRange(0, max(1, n))
        self.prog.setValue(i)
        self.prog.setFormat(f"{phase} {i}/{n}" if phase != "完成" else "扫描完成")

    def on_scan_done(self, found: list):
        self.btn_scan.setEnabled(True)
        self.entries = list(found)
        self._refresh()
        self.prog.setFormat(f"扫描完成，发现 {len(found)} 台")
        if len(self.entries) == 1:
            self.tbl.selectRow(0)

    # ---------- 内部 ----------
    def scan_bus(self):
        if self.worker:
            self.btn_scan.setEnabled(False)
            self.worker.start_scan(range(1, self.sp_to.value() + 1))

    def _refresh(self):
        self.tbl.setRowCount(0)
        for e in self.entries:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            cb = ArrowOnlyComboBox()
            cb.set_center_text(True)
            opts = []
            for m, mid in e["candidates"]:
                p = "dji" if m in dji.MODELS else e["protocol"]
                opts.append((p, m, mid))
            for p, m in ALL_MODELS:          # 允许用户改选任意协议
                valid_id = not (p == "dji" and e["motor_id"] not in dji.MODELS[m].ids)
                if p == e["protocol"] and valid_id and not any(o[1] == m for o in opts):
                    opts.append((p, m, e["motor_id"]))
            selected = (e["protocol"], e["model"], e["motor_id"])
            if selected in opts:
                opts.remove(selected)
                opts.insert(0, selected)
            for p, m, mid in opts:
                short_model = {
                    "M3508 (C620)": "M3508/C620",
                    "M2006 (C610)": "M2006/C610",
                    "GM6020 (电流模式)": "GM6020",
                    "RM 6623": "RM 6623",
                }.get(m, m)
                cb.addItem(short_model, (p, m, mid))
                cb.setItemData(cb.count() - 1, f"{PROTOCOLS[p][0]} · {m}", Qt.ToolTipRole)
            cb.setToolTip(f"{PROTOCOLS[selected[0]][0]} · {selected[1]}")
            cb.currentIndexChanged.connect(lambda _=0, row=r, c=cb: self._model_changed(row, c))
            cb.clicked.connect(lambda row=r: self.tbl.selectRow(row))
            cb.setProperty("motorRow", r)
            cb.installEventFilter(self)
            self.tbl.setCellWidget(r, 0, cb)
            fb = e.get("fb_id")
            for c, txt in ((1, str(e["motor_id"])), (2, f"0x{fb:03X}" if fb is not None else "")):
                field = QLineEdit(txt)
                field.setReadOnly(True)
                field.setFocusPolicy(Qt.NoFocus)
                field.setAlignment(Qt.AlignCenter)
                field.setToolTip(txt or "暂无反馈帧 ID")
                field.setProperty("motorRow", r)
                field.installEventFilter(self)
                self.tbl.setCellWidget(r, c, field)
        self._update_selection_style()

    def eventFilter(self, watched, event):
        row = watched.property("motorRow")
        if row is not None:
            row = int(row)
            if event.type() == QEvent.MouseButtonDblClick:
                self._double_click(row, 0)
                return True
            if event.type() == QEvent.MouseButtonPress:
                self.tbl.selectRow(row)
                self._temporary_row = row
                self._update_selection_style()
        return super().eventFilter(watched, event)

    def _set_default_column_widths(self):
        if self._columns_initialized:
            return
        header = self.tbl.horizontalHeader()
        if header.width() <= 0:
            QTimer.singleShot(50, self._set_default_column_widths)
            return
        self._columns_initialized = header.set_default_sizes()

    def _model_changed(self, row: int, cb: QComboBox):
        self.tbl.selectRow(row)
        self._model_pick(row, cb)

    def _double_click(self, row: int, _column: int = 0):
        self.tbl.selectRow(row)
        self._temporary_row = row
        self._update_selection_style()
        self._use()

    def _selection_changed(self):
        row = self.tbl.currentRow()
        self._temporary_row = row if row >= 0 else None
        self._update_selection_style()

    def set_debug_driver(self, driver):
        """设置持久的调试行；普通表格选中状态与它分离。"""
        self._debug_row = None
        if driver is not None:
            for row, entry in enumerate(self.entries):
                if (entry.get("protocol") == driver.protocol
                        and entry.get("model") == driver.model
                        and entry.get("motor_id") == driver.motor_id):
                    self._debug_row = row
                    break
            if self._debug_row is None and 0 <= self.tbl.currentRow() < len(self.entries):
                self._debug_row = self.tbl.currentRow()
        self.clear_temporary_selection()
        self._update_action_button()

    def clear_temporary_selection(self):
        self.tbl.clearSelection()
        self.tbl.setCurrentCell(-1, -1)
        self._temporary_row = None
        self._update_selection_style()

    def _update_action_button(self):
        self.btn_use.setText("取消调试" if self._debug_row is not None else "双击选择电机")

    def _action_clicked(self):
        if self._debug_row is not None:
            self.motor_selected.emit(None)
        else:
            self._use()

    def _update_selection_style(self):
        for row in range(self.tbl.rowCount()):
            cb = self.tbl.cellWidget(row, 0)
            if cb is None:
                continue
            highlighted = row in (self._debug_row, self._temporary_row)
            t = theme.current()
            acc, on = t.accent, t.accent_on
            if highlighted:
                cb.setStyleSheet(
                    f"QComboBox{{background:{acc};color:{on};font-weight:600;"
                    f"selection-background-color:{acc};selection-color:{on};}}"
                )
            else:
                cb.setStyleSheet("")
            for column in (1, 2):
                field = self.tbl.cellWidget(row, column)
                if field is None:
                    continue
                field.setStyleSheet(
                    f"QLineEdit{{background:{acc};color:{on};border:1px solid {acc};"
                    "border-radius:4px;padding:2px 4px;font-weight:600;}"
                    if highlighted else ""
                )

    def _model_pick(self, row: int, cb: QComboBox):
        p, m, mid = cb.currentData()
        cb.setToolTip(f"{PROTOCOLS[p][0]} · {m}")
        e = self.entries[row]
        e.update(protocol=p, model=m, motor_id=mid)
        if p == "dji":
            e["fb_id"] = dji.MODELS[m].fb_base + mid
        elif e.get("master_id") is not None:
            e["fb_id"] = e["master_id"]
        elif p == "gd":
            e["fb_id"] = 0x10000 | (mid << 8)
        candidates = [(m0, mid0) for m0, mid0 in e.get("candidates", [])
                      if not (m0 == m and mid0 == mid)]
        e["candidates"] = [(m, mid), *candidates]
        self.tbl.cellWidget(row, 1).setText(str(mid))
        fb = e.get("fb_id")
        fb_text = f"0x{fb:03X}" if fb is not None else ""
        self.tbl.cellWidget(row, 2).setText(fb_text)
        self.tbl.cellWidget(row, 2).setToolTip(fb_text or "暂无反馈帧 ID")
        if cb.currentIndex() > 0:
            items = [(cb.itemText(i), cb.itemData(i)) for i in range(cb.count())]
            selected_item = items.pop(cb.currentIndex())
            cb.blockSignals(True)
            cb.clear()
            cb.addItem(*selected_item)
            for item in items:
                cb.addItem(*item)
            cb.setCurrentIndex(0)
            cb.blockSignals(False)

    def _use_row(self, row: int):
        self.tbl.selectRow(row)
        self._use()

    def _use(self):
        r = self.tbl.currentRow()
        if not (0 <= r < len(self.entries)) or self.worker is None:
            QMessageBox.information(self, "选择电机", "请先在列表中选中一台电机")
            return
        if r == self._debug_row:
            self.motor_selected.emit(None)
            return
        e = self.entries[r]
        try:
            drv = make_driver(e["protocol"], e["model"], e["motor_id"], e.get("master_id") or 0)
        except ValueError as ex:
            QMessageBox.warning(self, "选择电机", str(ex))
            return
        self.motor_selected.emit(drv)
