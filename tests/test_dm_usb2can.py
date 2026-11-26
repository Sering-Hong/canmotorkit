"""达妙 USB2CAN 串口协议编解码，以及经假串口桥接演示台的端到端检查。"""
import struct
import time

import pytest
from PySide6.QtCore import QCoreApplication

from canmotorkit.control.safety import SafetyLimits
from canmotorkit.core import dm_usb2can as U
from canmotorkit.core.worker import Worker
from canmotorkit.drivers.catalog import make_driver
from canmotorkit.sim.demo import DemoBus, DmSimMotor

# 达妙官方 DM_CAN.py 的发送模板（第 13/14 字节为 ID，第 21~28 字节为数据）
OFFICIAL = [0x55, 0xAA, 0x1e, 0x03, 0x01, 0x00, 0x00, 0x00, 0x0a, 0x00, 0x00, 0x00, 0x00, 0, 0, 0, 0,
            0x00, 0x08, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0x00]


def rx_frame(can_id, data, cmd=0x11, ext=False, rtr=False):
    flags = len(data) | (0x40 if ext else 0) | (0x80 if rtr else 0)
    return (bytes((0xAA, cmd, flags)) + struct.pack("<I", can_id)
            + bytes(data).ljust(8, b"\x00") + b"\x55")


def test_encode_matches_official_template():
    data = bytes(range(1, 9))
    frame = list(OFFICIAL)
    frame[13], frame[14] = 0x01, 0x00
    frame[21:29] = data
    assert U.encode_tx(0x001, data) == bytes(frame)


def test_encode_extended_remote_and_short_dlc():
    raw = U.encode_tx(0x18001, b"", extended=True, rtr=True, dlc=8)
    assert len(raw) == 30 and raw[12] == 1 and raw[17] == 1 and raw[18] == 8
    assert struct.unpack_from("<I", raw, 13)[0] == 0x18001
    raw = U.encode_tx(0x7FF, b"\x00\xCC")
    assert raw[18] == 2 and raw[21:23] == b"\x00\xCC" and raw[23:29] == b"\x00" * 6
    with pytest.raises(ValueError):
        U.encode_tx(0x800, b"")


def test_bitrate_command():
    assert U.bitrate_command(1_000_000) == bytes((0x55, 0x05, 0x00, 0xAA, 0x55))
    assert U.bitrate_command(500_000)[2] == 3


def test_parse_stream_resync_split_and_flags():
    a = rx_frame(0x011, b"\x10\x20\x30\x40\x50\x60\x70\x80")
    b = rx_frame(0x10100, b"\x0E\x01", ext=True)
    tx_report = rx_frame(0x001, b"\x00" * 8, cmd=0x12)
    stream = b"\x00\xAA\x55junk" + a + tx_report + b
    buf = bytearray(stream[:26])        # 7 字节杂波 + 完整帧 a + 下一帧前 3 字节
    out = U.parse_stream(buf)
    assert [x[1] for x in out] == [0x011]
    buf.extend(stream[26:])
    out = U.parse_stream(buf)
    assert [(c, i) for c, i, *_ in out] == [(0x12, 0x001), (0x11, 0x10100)]
    assert out[1][2:] == (b"\x0E\x01", True, False, 2)
    assert buf == bytearray()


class _FakeSerial:
    """把 USB2CAN 字节协议桥接到演示台：写入的 30 字节帧发给虚拟电机，电机应答编码为 16 字节帧。"""

    def __init__(self):
        self.bus = DemoBus()
        self.bus.open()
        self.written = []

    def write(self, raw):
        self.written.append(bytes(raw))
        if len(raw) == 30 and raw[0] == 0x55 and raw[1] == 0xAA:
            can_id = struct.unpack_from("<I", raw, 13)[0]
            dlc = raw[18]
            self.bus.send(can_id, bytes(raw[21:21 + dlc]), extended=bool(raw[12]),
                          rtr=bool(raw[17]), dlc=dlc)

    def read(self, n):
        out = b"".join(rx_frame(f.can_id, f.data, ext=f.extended) for f in self.bus.recv_all())
        return out[:n]

    def close(self):
        self.bus.close()


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def test_worker_through_usb2can_protocol(app):
    bus = U.DmUsb2CanBus("COM_FAKE")
    fake = _FakeSerial()
    bus._serial = fake
    w = Worker(bus, make_driver("dm", "HM9816G9", 1, 0))
    w.start()
    try:
        w.set_sending(True)
        end = time.perf_counter() + 2.0
        while time.perf_counter() < end and not (w.limits_ok and w.last_fb is not None):
            app.processEvents()
            time.sleep(0.005)
        assert w.limits_ok and w.last_fb is not None        # 量程读写走 0x7FF，经编解码往返
        w.set_safety_limits(SafetyLimits(vel_max=50, out_max=1.0, temp_max=0))
        w.set_target_out(0.9)
        w.enable()
        end = time.perf_counter() + 2.0
        while time.perf_counter() < end and not (w.active and abs(fb_m := fake.bus.motor(DmSimMotor, 1).cmd.t) > 0.8):
            app.processEvents()
            time.sleep(0.005)
        assert w.active and abs(fb_m - 0.9) < 0.05
        assert all(len(x) == 30 for x in fake.written)
        w.disable()
    finally:
        w.stop()
        w.wait(2000)
        fake.close()
