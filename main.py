"""CanMotorKit 入口：CAN 电机调试与测试上位机。

用法:
    python main.py          # 连接真实硬件（PCAN / SLCAN / SocketCAN）
    python main.py --sim    # 演示台（虚拟电机，无需硬件），启动后自动连接
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from canmotorkit import APP_NAME, __version__

    ap = argparse.ArgumentParser(description=f"{APP_NAME} {__version__} — CAN 电机调试与测试上位机")
    ap.add_argument("--sim", action="store_true", help="使用演示台（虚拟电机）并自动连接")
    ap.add_argument("--debug", action="store_true", help="日志记录调试级别信息")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = ap.parse_args()

    import logging

    from canmotorkit.applog import install_excepthook, log, setup_logging
    log_path = setup_logging(logging.DEBUG if args.debug else logging.INFO)
    install_excepthook()
    log.info("启动 %s %s，Python %s，日志 %s", APP_NAME, __version__, sys.version.split()[0], log_path)

    import pyqtgraph as pg
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    pg.setConfigOptions(antialias=False, foreground="k")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    if sys.platform == "win32":
        font = QFont("Microsoft YaHei UI", 9)
        font.setStyleStrategy(QFont.PreferAntialias)
        app.setFont(font)

    from canmotorkit.ui.icon import app_icon
    from canmotorkit.ui.main_window import MainWindow

    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.show()
    if args.sim:
        win.conn.select_sim()
        win.connect_bus(win.conn.params())
    code = app.exec()
    log.info("退出，代码 %s", code)
    sys.exit(code)


if __name__ == "__main__":
    main()
