# CanMotorKit

基于 PEAK PCAN / Windows SLCAN / Linux SocketCAN 的 CAN 电机调试与测试工具。约定见 `AGENTS.md`，待办见 `TODO.md`，厂家资料见 `reference/`。
当前支持达妙/HM（MIT + 0x7FF，HM9816G9 实机验证通信）、大疆 RoboMaster、高擎 GD；PCAN / SLCAN / SocketCAN / 演示台。

## 下载
Windows 可直接下载 Release 中的 `CanMotorKit-<版本>.exe`（单文件，无需 Python）。连接 PCAN 需另装 PEAK 驱动（提供 PCANBasic.dll）；演示台无需硬件。

## 打包 exe
```
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe tools/build_exe.py      # 产物 dist/CanMotorKit-<版本>.exe
```

## 安装 / 运行
```
python -m venv .venv
.venv\Scripts\activate  # 可选：独立环境（Linux: source .venv/bin/activate）
pip install -r requirements.txt
python main.py          # 真实硬件（需安装 PEAK 驱动，PCANBasic.dll）
python main.py --sim    # 演示台（虚拟电机），也可在通道下拉选「演示台」
python -m pytest tests  # 单元/集成测试
python tools/hw_diag.py info                    # 实机只读诊断
python tools/hw_diag.py torque --t 0.3 --dur 1  # 实机纯扭矩测试（|t|<=1Nm，<=5s）
```

Linux 使用 SocketCAN 时，先由系统配置并启用接口，例如：
```
sudo ip link set can0 down 2>/dev/null || true
sudo ip link set can0 up type can bitrate 1000000
python main.py
```
程序会在连接列表中显示 `SocketCAN can0`。SocketCAN 的波特率由 Linux 配置，程序只负责收发经典 CAN 帧。

Windows 使用 Canable2 时，程序按 USB VID/PID 自动发现设备并显示当前系统分配的 COM 端口，
不固定 COM 编号。串口默认 115200，界面中的波特率设置 CAN 总线速率。

## 操作模型
- **使能 / 失能**：只发 FC / FD，按钮不锁定。电机是否使能以状态帧状态码为准。
- **通信频率**（控制面板底部，一个参数）：电机已使能时按此频率发送已应用的 MIT 参数，
  否则按此频率发送 0xCC 查询帧。发出失能/急停后立即改发查询帧。
  电机已使能但量程未读取时只发零扭矩帧。
- **目标参数**：默认全 0，修改后（黄色标记）必须点“应用”或在数值框按回车才生效。
- **量程**：连接后自动读取 p_max/v_max/t_max（0x15~0x17）。未读到时禁止使能，可在参数页“重读量程”。KP 0~500、KD 0~5 固定。
- **配置帧保护**：电机使能中不发送 0x7FF 配置帧（读请求排队，写/保存/改 ID/设零/扫描拒绝）。

## 安全
- 左栏「安全限制 / 急停」：速度上限、电流/扭矩限幅、位置窗口（多圈累计）、温度、反馈超时。
- 速度上限与输出限幅**默认启用**（扭矩 ≤1 Nm，电流取协议上限 10%，速度 20 rad/s），
  按「协议/型号」记忆上次的设置。
- **MIT 等效扭矩限幅**：电机实际扭矩为 τ = kp·(p−θ) + kd·(v−ω) + t。上位机用最近一帧反馈估算 τ，
  超出扭矩限幅时自动把位置目标 p 拉近当前位置（kp=0 时缩小前馈或 kd），速度目标 v 受速度上限约束；
  估算有一个通信周期的滞后。只要 kp/kd/t 任一非零，反馈超时保护即生效。
- 急停（按钮 / Esc / 空格）与超限均为**零扭矩并锁存**，不失能；状态栏红色显示原因，
  点「解除锁存」后恢复发送已应用参数。失能只由用户点「失能」。
- 关闭「发送」前会先下发一帧零输出（达妙/HM 未设 can_timeout 时会一直保持最后一帧指令）。
- 大疆/GD 等软件使能的电机，点「开始输出」即启动反馈看门狗，一帧反馈都收不到也会触发零输出。

## 界面与设置
- 右上角「设置」→「外观设置…」：选择背景图，调节背景不透明度、遮罩强度、面板不透明度和强调色，
  实时预览；不透明度/遮罩/强调色按深色、浅色模式**分别记忆**，背景图路径共用。
- 自动记忆：窗口位置与大小、左右栏宽度、连接通道与波特率、发送频率、各型号的安全限制。
  「设置」→「重置窗口布局」可恢复默认布局。
- 日志：`%LOCALAPPDATA%\CanMotorKit\logs\canmotorkit.log`（「设置」→「打开日志目录」）。
  未处理的异常会写入日志并弹窗提示，不会静默崩溃；`python main.py --debug` 记录调试信息。

## 状态码（状态帧 d0 高 4 位）
| 码 | 含义 |
|---|---|
| 0 | 失能 |
| 1 | 已使能（实测：使能期间恒为 1，绿灯常亮，扭矩正常输出） |
| ≥2 | 故障（8~E 含义参考达妙协议，未经厂家确认） |

## 功能
- **MIT 控制**：使能/失能/清错/设零/急停，p/v/kp/kd/t 滑条，通信频率 10–1000 Hz。
- **实时曲线**：位置/速度/扭矩（指令 vs 反馈）与温度，暂停、时间窗口、CSV 导出。
- **参数寄存器**：全部读取、编辑后写入、保存 Flash、修改 ID、重读量程、JSON 导入导出。
- **测试信号**：阶跃/方波/正弦/扫频，需先使能；输出上升时间/超调/调节时间/稳态误差。
- **扫描/报文**：0xCC 逐个查询发现电机；原始帧日志；手动发送任意帧。

## 实机记录（HM9816G9，24 V，空载）
- p_max=π（状态帧位置为单圈值，在 ±π 内翻转），v_max=45，t_max=54。
- 纯扭矩：0.3/0.6 Nm 不转（静摩擦），1.0 Nm 约 0.25 s 加速到 16 rad/s。
- 寄存器 0x51 output_angle_multiturn 与状态帧位置符号相反，待确认。

## 待确认
- 状态帧 d6/d7：实测两者相同（约 26），按温度显示。
- 修改 ID 后固件从哪个 ID 应答未知，写入应答可能超时；重启后用扫描确认。
