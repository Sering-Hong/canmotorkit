"""达妙 USB2CAN（USB 虚拟串口）总线。

协议依据（两处互相印证，未取得达妙官方协议文档，实机待核对）：
- 达妙官方 Python 例程 DM_Control_Python/DM_CAN.py：30 字节发送模板、16 字节接收帧、921600 串口。
- 兼容达妙协议的开源固件 Deadline039/DM-USB2CAN（dm_protocol.c）：各字节含义与波特率命令。

PC → 适配器，30 字节：
  [0]=0x55 [1]=0xAA(发送) [2]=0x1E(长度) [3]=0x03 [4..7]=发送次数 u32 LE
  [8..11]=发送间隔 u32 LE（0.1 ms） [12]=ID 类型 0 标准/1 扩展 [13..16]=CAN ID u32 LE
  [17]=0 数据帧/1 远程帧 [18]=DLC [19]=ID 自增 [20]=数据自增 [21..28]=数据 [29]=0x00
适配器 → PC，16 字节：
  [0]=0xAA [1]=命令(0x11 接收成功, 0x12 发送成功, 0x01/0x02 失败, 0x00 心跳)
  [2]=bit0~5 DLC, bit6 扩展帧, bit7 远程帧 [3..6]=CAN ID u32 LE [7..14]=数据 [15]=0x55
设置 CAN 波特率：55 05 <序号> AA 55（序号 0=1M, 1=800K, 3=500K, 5=250K, 7=125K）。

注意：上述开源固件每毫秒最多发出 1 帧，且未发出的命令会被下一条覆盖；通信频率建议 ≤500 Hz。
"""
from __future__ import annotations

import struct
import time

from .bus import BusError, CanBus, Frame

SERIAL_BAUDRATE = 921600
FRAME_TX_LEN = 30
FRAME_RX_LEN = 16
RX_HEAD, RX_TAIL = 0xAA, 0x55
CMD_RX_OK = 0x11
RX_CMDS = (0x00, 0x01, 0x02, 0x11, 0x12)
BITRATE_INDEX = {1_000_000: 0, 800_000: 1, 500_000: 3, 250_000: 5, 125_000: 7}
ST_VCP = (0x0483, 0x5740)       # 常见实现使用 ST 通用虚拟串口 VID/PID


def encode_tx(can_id: int, data: bytes, extended: bool = False, rtr: bool = False,
              dlc: int | None = None) -> bytes:
    limit = 0x1FFFFFFF if extended else 0x7FF
    if not 0 <= can_id <= limit:
        raise ValueError(f"CAN ID 超出范围: 0x{can_id:X}")
    if len(data) > 8:
        raise ValueError("经典 CAN 数据长度不能超过 8 字节")
    wire_dlc = len(data) if dlc is None else dlc
    if not 0 <= wire_dlc <= 8:
        raise ValueError("DLC 必须在 0~8 之间")
    payload = (b"" if rtr else bytes(data)).ljust(8, b"\x00")
    # 次数=1、间隔=0x0A 与达妙官方例程模板一致
    return (bytes((0x55, 0xAA, 0x1E, 0x03)) + struct.pack("<II", 1, 0x0A)
            + bytes((1 if extended else 0,)) + struct.pack("<I", can_id)
            + bytes((1 if rtr else 0, wire_dlc, 0, 0)) + payload + b"\x00")


def bitrate_command(bitrate: int) -> bytes:
    if bitrate not in BITRATE_INDEX:
        raise ValueError(f"达妙 USB2CAN 不支持 {bitrate} bit/s")
    return bytes((0x55, 0x05, BITRATE_INDEX[bitrate], 0xAA, 0x55))


def parse_stream(buf: bytearray) -> list[tuple[int, int, bytes, bool, bool, int]]:
    """从串口字节流中取出完整的 16 字节帧，返回 [(cmd, id, data, ext, rtr, dlc)]，并就地删除已消费字节。

    以 帧头 0xAA + 帧尾 0x55 + 合法命令 + DLC≤8 共同判定，失步时逐字节重新同步。
    """
    out = []
    i = 0
    n = len(buf)
    while n - i >= FRAME_RX_LEN:
        if buf[i] != RX_HEAD:
            i += 1
            continue
        f = buf[i:i + FRAME_RX_LEN]
        flags = f[2]
        dlc = flags & 0x3F
        if f[15] != RX_TAIL or f[1] not in RX_CMDS or dlc > 8:
            i += 1
            continue
        can_id = struct.unpack_from("<I", f, 3)[0]
        ext, rtr = bool(flags & 0x40), bool(flags & 0x80)
        out.append((f[1], can_id, b"" if rtr else bytes(f[7:7 + dlc]), ext, rtr, dlc))
        i += FRAME_RX_LEN
    del buf[:i]
    return out


def candidate_ports() -> list[dict]:
    """列出可能是达妙 USB2CAN 的串口。达妙未使用专用 VID/PID，因此列出全部 USB 串口（排除 CANable），
    ST 虚拟串口（0483:5740）排在最前。"""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    from .slcan import CANABLE_PID, CANABLE_VID
    out = []
    for p in list_ports.comports():
        if p.vid is None or (p.vid, p.pid) == (CANABLE_VID, CANABLE_PID):
            continue        # 蓝牙等非 USB 串口没有 VID，不可能是 USB2CAN
        out.append({"port": p.device, "description": p.description or p.device,
                    "likely": (p.vid, p.pid) == ST_VCP})
    out.sort(key=lambda d: (not d["likely"], d["port"]))
    return out


class DmUsb2CanBus(CanBus):
    def __init__(self, port: str, bitrate: int = 1_000_000):
        self.port = str(port)
        self.bitrate = int(bitrate)
        self.name = f"达妙 USB2CAN {self.port}"
        self._serial = None
        self._rx = bytearray()
        self.tx_fail = 0
        self.rx_fail = 0

    def open(self) -> None:
        if self.bitrate not in BITRATE_INDEX:
            raise BusError(f"达妙 USB2CAN 不支持 CAN 波特率 {self.bitrate:g} bit/s")
        if self._serial is not None:
            return
        try:
            import serial
            ser = serial.Serial(self.port, baudrate=SERIAL_BAUDRATE, timeout=0, write_timeout=0.2)
        except Exception as e:  # noqa: BLE001
            raise BusError(f"打开 {self.port} 失败: {e}（是否被达妙上位机等软件占用？）") from e
        self._serial = ser
        self._rx.clear()
        try:
            ser.reset_input_buffer()
            ser.write(bitrate_command(self.bitrate))
            ser.flush()
            time.sleep(0.02)
            ser.reset_input_buffer()
        except Exception as e:  # noqa: BLE001
            self.close()
            raise BusError(f"配置达妙 USB2CAN 失败: {e}") from e

    def close(self) -> None:
        ser, self._serial = self._serial, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass

    def send(self, can_id: int, data: bytes, extended: bool = False,
             rtr: bool = False, dlc: int | None = None) -> None:
        if self._serial is None:
            raise BusError("达妙 USB2CAN 尚未打开")
        try:
            self._serial.write(encode_tx(can_id, data, extended, rtr, dlc))
        except ValueError as e:
            raise BusError(str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise BusError(f"达妙 USB2CAN 发送失败: {e}") from e

    def recv_all(self) -> list[Frame]:
        if self._serial is None:
            return []
        try:
            chunk = self._serial.read(8192)
        except Exception as e:  # noqa: BLE001
            raise BusError(f"达妙 USB2CAN 接收失败: {e}") from e
        if chunk:
            self._rx.extend(chunk)
        if len(self._rx) > 65536:          # 长时间失步时防止无限增长
            del self._rx[:-4096]
        t = time.perf_counter()
        out = []
        for cmd, can_id, data, ext, rtr, dlc in parse_stream(self._rx):
            if cmd == CMD_RX_OK:
                out.append(Frame(t, can_id, data, extended=ext, rtr=rtr, dlc=dlc))
            elif cmd == 0x02:
                self.tx_fail += 1
            elif cmd == 0x01:
                self.rx_fail += 1
        return out

    def status_text(self) -> str:
        if self._serial is None:
            return "未打开"
        if self.tx_fail:
            return f"OK（发送失败 {self.tx_fail} 次：检查接线/终端电阻/波特率）"
        return "OK"
