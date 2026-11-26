"""程序图标：运行时绘制（电机轴 + CAN 波形），不依赖外部图片文件。"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _draw(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e0709a"))
    p.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 14 * s, 14 * s)
    # 电机外圈与转子
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#ffffff"), 4 * s))
    p.drawEllipse(QPointF(32 * s, 28 * s), 15 * s, 15 * s)
    p.setBrush(QColor("#ffffff"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(32 * s, 28 * s), 4.5 * s, 4.5 * s)
    for k in range(3):
        a = math.radians(-90 + 120 * k)
        p.drawEllipse(QPointF(32 * s + 9 * s * math.cos(a), 28 * s + 9 * s * math.sin(a)),
                      2.2 * s, 2.2 * s)
    # CAN 差分波形
    path = QPainterPath(QPointF(10 * s, 54 * s))
    for x, y in ((18, 54), (18, 48), (28, 48), (28, 54), (36, 54), (36, 48), (46, 48),
                 (46, 54), (54, 54)):
        path.lineTo(x * s, y * s)
    p.setPen(QPen(QColor("#ffffff"), 3 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for n in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw(n))
    return icon
