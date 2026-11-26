"""实时曲线：位置（累计）/ 速度 / 输出（指令 vs 反馈）/ 温度；环形缓冲 + 暂停 + CSV。

pyqtgraph 的 SI 前缀自动缩放（mrad、mNm 等）保持开启。
"""
from __future__ import annotations

import csv
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from . import theme
from .widgets import (CheckBox, SelectedFirstComboBox, equalize_graphics_rows,
                      set_legend_text_color)

COLS = ["t", "pos_raw", "pos_mt", "vel", "out_cmd", "out_fb", "ref", "temp", "temp2", "code", "active"]
IDX = {c: i for i, c in enumerate(COLS)}


class Ring:
    def __init__(self, n: int, cols: int):
        self.buf = np.full((n, cols), np.nan)
        self.n, self.i, self.count = n, 0, 0

    def extend(self, rows: np.ndarray):
        rows = rows[-self.n:]
        k = len(rows)
        end = self.i + k
        if end <= self.n:
            self.buf[self.i:end] = rows
        else:
            a = self.n - self.i
            self.buf[self.i:] = rows[:a]
            self.buf[:k - a] = rows[a:]
        self.i = end % self.n
        self.count = min(self.count + k, self.n)

    def data(self) -> np.ndarray:
        if self.count < self.n:
            return self.buf[:self.count]
        return np.vstack((self.buf[self.i:], self.buf[:self.i]))

    def clear(self):
        self.buf[:] = np.nan
        self.i = self.count = 0


class PlotWidget(QWidget):
    def __init__(self, parent=None, capacity: int = 200_000, compact: bool = False):
        super().__init__(parent)
        self.ring = Ring(capacity, len(COLS))
        self.t0: float | None = None
        self._dirty = False
        self.out_unit = "Nm"

        self.sp_win = QDoubleSpinBox()
        self.sp_win.setRange(0.5, 600)
        self.sp_win.setValue(10)
        self.sp_win.setSuffix(" s")
        self.cb_pos = SelectedFirstComboBox()
        self.cb_pos.addItem("位置：多圈累计", "multi")
        self.cb_pos.addItem("位置：电机原始值", "raw")
        self.chk_pause = CheckBox("暂停")
        self.chk_ref = CheckBox("显示测试参考")
        self.chk_ref.setChecked(True)
        self.btn_clear = QPushButton("清空")
        self.btn_csv = QPushButton("导出 CSV")
        self.lbl_info = QLabel()
        self.lbl_info.setProperty("themeRole", "muted")
        bar = QHBoxLayout()
        for w in (QLabel("窗口"), self.sp_win, self.cb_pos, self.chk_pause, self.chk_ref,
                  self.btn_clear, self.btn_csv):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.lbl_info)

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground("w")
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.plots = []
        self.plot_series = []
        specs = [("位置", "rad", [("pos", "反馈", "#1565c0", False), ("ref_pos", "参考", "#c62828", False)]),
                 ("速度", "rad/s", [("vel", "反馈", "#1565c0", False), ("ref_vel", "参考", "#c62828", False)]),
                 ("输出", "Nm", [("out_cmd", "指令", "#c62828", False), ("out_fb", "反馈", "#1565c0", False)])]
        if not compact:
            specs.append(("温度", "℃", [("temp", "温度 1", "#e65100", False), ("temp2", "温度 2", "#6a1b9a", False)]))
        for i, (title, unit, cs) in enumerate(specs):
            p = self.glw.addPlot(row=i, col=0)
            p.showGrid(x=True, y=True, alpha=0.3)
            p.setLabel("left", title, units=unit)
            p.addLegend(offset=(5, 5))
            p.setDownsampling(auto=True, mode="peak")
            p.setClipToView(True)
            p.setLimits(xMin=0)
            p.getViewBox().sigXRangeChanged.connect(
                lambda _view, _range, plot=p: self._fit_plot_y_from_data(plot)
            )
            for key, name, color, dash in cs:
                # 反馈、参考和输出曲线统一使用实线，避免深色背景下
                # 虚线断续后可读性明显下降。
                pen = pg.mkPen(color, width=2, style=Qt.SolidLine)
                self.curves[key] = p.plot(name=name, pen=pen)
            self.plots.append(p)
            self.plot_series.append(tuple(key for key, *_ in cs))
        self.plots[-1].setLabel("bottom", "时间", units="s")
        self.ref_target = ""          # "pos" / "vel" / "out"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.glw, 1)
        self.btn_clear.clicked.connect(self.clear)
        self.btn_csv.clicked.connect(self.export_csv)
        for w in (self.chk_ref, self.cb_pos, self.sp_win):
            sig = w.toggled if isinstance(w, CheckBox) else (
                w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged)
            sig.connect(lambda *_: self._mark())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(33)
        self.apply_theme(True)
        self._equalize_plot_rows()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._equalize_plot_rows)

    def _equalize_plot_rows(self):
        equalize_graphics_rows(self.glw)

    def apply_theme(self, dark: bool):
        """同步曲线绘图区、坐标轴和图例的前景色。"""
        background = theme.current().plot_bg()
        foreground = theme.current().pal.text
        grid = theme.current().pal.grid
        self.glw.setBackground(background)
        for plot in self.plots:
            for axis_name in ("left", "bottom", "right", "top"):
                axis = plot.getAxis(axis_name)
                axis.setPen(pg.mkPen(foreground))
                axis.setTextPen(pg.mkPen(foreground))
            plot.showGrid(x=True, y=True, alpha=0.3)
            for grid_item in getattr(plot, "items", []):
                if hasattr(grid_item, "setPen") and grid_item.__class__.__name__ == "InfiniteLine":
                    grid_item.setPen(pg.mkPen(grid, width=1))
            legend = getattr(plot, "legend", None)
            set_legend_text_color(legend, foreground)

    def set_unit(self, unit: str, name: str = "输出"):
        self.out_unit = unit
        self.plots[2].setLabel("left", name, units=unit)

    def set_ref_target(self, target: str):
        self.ref_target = target
        self._mark()

    def _mark(self):
        self._dirty = True

    @staticmethod
    def _set_plot_y_range(plot, values, adaptive_position: bool = False,
                          include_zero: bool = False):
        """按当前可见曲线范围设置 Y 轴，避免大信号跑出视口。"""
        values = np.asarray(values, dtype=float)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return
        low, high = float(np.min(finite)), float(np.max(finite))
        if include_zero:
            # 速度曲线保留零速基准。否则启动阶段离开可见窗口后，恒速
            # 段的反馈噪声会被放大成整幅图，整体趋势反而不可读。
            low, high = min(low, 0.0), max(high, 0.0)
        if adaptive_position:
            # 累计位置可能已经走到数百/数千 rad，但用户仍需要看清当前
            # 窗口内很小的变化。绝对位置只作为最小观察范围，不再决定主要跨度。
            baseline = max(abs(low), abs(high))
            minimum_span = max(0.01, baseline * 0.001)
            span = max(high - low, minimum_span)
        else:
            span = max(high - low, abs(low) * 0.1, abs(high) * 0.1, 1e-6)
        pad = max(span * 0.12, 1e-6)
        if high == low:
            pad = max(abs(high) * 0.2, 1.0) if not adaptive_position else span * 0.12
        plot.setYRange(low - pad, high + pad, padding=0)

    def _fit_plot_y_from_data(self, plot):
        if self.ring.count == 0 or plot not in self.plots:
            return
        data = self.ring.data()
        t = data[:, IDX["t"]]
        left, right = plot.getViewBox().viewRange()[0]
        lo, hi = min(left, right), max(left, right)
        visible = (t >= lo) & (t <= hi)
        if not np.any(visible):
            return
        values = []
        for key in self.plot_series[self.plots.index(plot)]:
            if key in ("ref_pos", "ref_vel") and self.ref_target != key[4:]:
                continue
            if key.startswith("ref_"):
                index = IDX["ref"]
            elif key == "pos":
                index = IDX["pos_mt"] if self.cb_pos.currentData() == "multi" else IDX["pos_raw"]
            else:
                index = IDX[key]
            values.append(data[visible, index])
        if values:
            self._set_plot_y_range(
                plot,
                np.concatenate(values),
                adaptive_position=plot is self.plots[0],
                include_zero=plot is self.plots[1],
            )

    def _fit_visible_y_ranges(self):
        for plot in self.plots:
            self._fit_plot_y_from_data(plot)

    def add_samples(self, samples):
        if not samples:
            return
        if self.t0 is None:
            self.t0 = samples[0].t
        nan = math.nan
        rows = np.array([(max(0.0, s.t - self.t0), s.fb.pos, s.pos_mt, s.fb.vel, s.out, s.fb.out, s.ref,
                          s.fb.temp, s.fb.temp2, s.fb.code, 1.0 if s.active else 0.0)
                         for s in samples], dtype=float)
        self.ring.extend(rows)
        self._dirty = True
        _ = nan

    def clear(self):
        self.ring.clear()
        self.t0 = None
        for c in self.curves.values():
            c.setData([], [])
        for p in self.plots:
            p.setXRange(0.0, self.sp_win.value(), padding=0)

    def _refresh(self):
        if not self._dirty or self.ring.count == 0 or not self.isVisible():
            return
        self._dirty = False
        d = self.ring.data()
        t = d[:, 0]
        tmax = t[-1]
        win = self.sp_win.value()
        # 保留完整环形缓存给 PlotDataItem；实时窗口只控制视口，暂停后才能回看历史。
        pos = d[:, IDX["pos_mt"] if self.cb_pos.currentData() == "multi" else IDX["pos_raw"]]
        self.curves["pos"].setData(t, pos, connect="finite")
        self.curves["vel"].setData(t, d[:, IDX["vel"]], connect="finite")
        self.curves["out_cmd"].setData(t, d[:, IDX["out_cmd"]], connect="finite")
        self.curves["out_fb"].setData(t, d[:, IDX["out_fb"]], connect="finite")
        if "temp" in self.curves:
            self.curves["temp"].setData(t, d[:, IDX["temp"]], connect="finite")
            self.curves["temp2"].setData(t, d[:, IDX["temp2"]], connect="finite")
        ref = d[:, IDX["ref"]] if self.chk_ref.isChecked() else np.full(len(t), np.nan)
        self.curves["ref_pos"].setData(t, ref if self.ref_target == "pos" else t * np.nan, connect="finite")
        self.curves["ref_vel"].setData(t, ref if self.ref_target == "vel" else t * np.nan, connect="finite")
        if not self.chk_pause.isChecked():
            left = max(0.0, tmax - win)
            right = max(win, tmax)
            for plot in self.plots:
                plot.setXRange(left, right, padding=0)
        self._fit_visible_y_ranges()
        rate = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0
        self.lbl_info.setText(f"反馈 {rate:.0f} Hz｜缓存 {self.ring.count} 点")

    def export_csv(self):
        if self.ring.count == 0:
            QMessageBox.information(self, "导出", "没有数据")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出曲线数据", "curve.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([c if c not in ("out_cmd", "out_fb") else f"{c}[{self.out_unit}]" for c in COLS])
            for r in self.ring.data():
                w.writerow(["" if math.isnan(x) else f"{x:.7g}" for x in r])
