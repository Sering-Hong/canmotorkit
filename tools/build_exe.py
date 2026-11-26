"""打包 Windows 单文件 exe（PyInstaller）。

用法（项目根目录）:
    .venv\\Scripts\\python.exe -m pip install pyinstaller
    .venv\\Scripts\\python.exe tools/build_exe.py

产物: dist/CanMotorKit-<版本>.exe。PCANBasic.dll 不打包，由 PEAK 驱动提供。
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def make_icon(path: str):
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    from canmotorkit.ui.icon import _draw
    if not _draw(256).save(path, "ICO"):
        raise RuntimeError("生成图标失败")


def main():
    from canmotorkit import APP_NAME, __version__
    build = os.path.join(ROOT, "build")
    os.makedirs(build, exist_ok=True)
    ico = os.path.join(build, "app.ico")
    make_icon(ico)
    name = f"{APP_NAME}-{__version__}"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", name, "--icon", ico,
           "--distpath", os.path.join(ROOT, "dist"), "--workpath", build, "--specpath", build,
           # pyqtgraph 可选后端，不需要
           "--exclude-module", "PyQt5", "--exclude-module", "PyQt6", "--exclude-module", "matplotlib",
           "--exclude-module", "scipy", "--exclude-module", "OpenGL", "--exclude-module", "tkinter",
           "--exclude-module", "pytest",
           "--add-data", os.path.join(ROOT, "使用说明.md") + os.pathsep + ".",
           os.path.join(ROOT, "main.py")]
    print(" ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)
    print("输出:", os.path.join(ROOT, "dist", name + ".exe"))


if __name__ == "__main__":
    main()
